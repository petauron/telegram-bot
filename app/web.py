from __future__ import annotations

import hashlib
import os
import secrets
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, TypeVar
from urllib.parse import SplitResult, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from app.secrets import read_setting

from app.auth import (
    MIN_WEB_PASSWORD_LENGTH,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    LoginRateLimiter,
    SessionStore,
    is_valid_web_password,
)
from app.database import (
    PUSH_SCORE_THRESHOLD,
    QUEUE_METRIC_RETENTION_DAYS,
    QUEUE_METRIC_SAMPLE_INTERVAL_SECONDS,
    Database,
    utc_now,
)
from app.cisa_kev import CISA_KEV_WEB_URL
from app.feeds import validate_feed_url
from app.github_releases import github_releases_web_url, validate_github_repository
from app.github_advisories import github_advisories_web_url, validate_advisory_settings
from app.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_CLASSIFICATION_MODEL,
    DEFAULT_CLASSIFICATION_REASONING_EFFORT,
    DEFAULT_NOTIFICATION_MODEL,
    DEFAULT_NOTIFICATION_REASONING_EFFORT,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_SEMANTIC_DEDUPE_MODEL,
    DEFAULT_SEMANTIC_DEDUPE_REASONING_EFFORT,
    ModelClientError,
    OpenAICompatibleClient,
    validate_base_url,
    validate_model_id,
    validate_reasoning_effort,
)
from app.nvd_cve import NVD_SOURCE_WEB_URL, validate_nvd_settings
from app.vendor_status import validate_status_page_url
from app.hacker_news import HN_WEB_URL, validate_hacker_news_settings
from app.bluesky import bluesky_profile_url, validate_bluesky_handle
from app.mastodon import mastodon_source_url, validate_mastodon_settings
from app.newsletter_imap import imap_source_url, validate_imap_settings
from app.push import send_push_test
from app.push_config import DEFAULT_NTFY_BASE_URL


STATIC_DIR = Path(__file__).with_name("web_static")
WEB_USERNAME = os.getenv("WEB_USERNAME", "admin").strip() or "admin"
WEB_PASSWORD = read_setting("WEB_PASSWORD")
DATABASE_PATH = os.getenv("DATABASE_PATH", "/database/messages.db").strip() or "/database/messages.db"


def _environment_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise RuntimeError(f"{name} 必须大于等于 {minimum}")
    return value


def _environment_ints(name: str) -> frozenset[int]:
    values: set[int] = set()
    for item in os.getenv(name, "").split(","):
        if not item.strip():
            continue
        try:
            values.add(int(item.strip()))
        except ValueError as exc:
            raise RuntimeError(f"{name} 必须是逗号分隔的整数") from exc
    return frozenset(values)


def _environment_strings(name: str, default: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.strip() for item in os.getenv(name, default).split(",") if item.strip()
        )
    )


def _environment_boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} 必须是 true 或 false")


WEB_COOKIE_SECURE = _environment_boolean("WEB_COOKIE_SECURE", False)

if not is_valid_web_password(WEB_PASSWORD):
    raise RuntimeError(
        f"WEB_PASSWORD 必须设置为至少 {MIN_WEB_PASSWORD_LENGTH} 位的非示例密码"
    )

RETENTION_DAYS = _environment_int("RETENTION_DAYS", 3, minimum=1)
INITIAL_IMMEDIATE_SCORE = PUSH_SCORE_THRESHOLD
INITIAL_IMPORTANT_KEYWORDS = _environment_strings(
    "IMPORTANT_KEYWORDS",
    "宕机,服务中断,停服,被墙,故障,维护,服务异常,服务恢复,漏洞,CVE,零日,0day,"
    "数据泄露,供应链攻击,安全事件,重大更新,版本发布,正式发布,开源,涨价,"
    "价格调整,退款政策,政策变更,封禁政策,新规,制裁",
)
INITIAL_TRUSTED_SENDER_IDS = _environment_ints("TRUSTED_SENDER_IDS")
INITIAL_WATCH_CHAT_IDS = _environment_ints("WATCH_CHAT_IDS")
MODEL_CLIENT = OpenAICompatibleClient()
PUSH_TEST_LOCK = asyncio.Lock()
SESSION_STORE = SessionStore()
LOGIN_LIMITER = LoginRateLimiter()
USERNAME_DIGEST = hashlib.sha256(WEB_USERNAME.encode("utf-8")).digest()
PASSWORD_SALT = secrets.token_bytes(16)
PASSWORD_DIGEST = hashlib.scrypt(
    WEB_PASSWORD.encode("utf-8"), salt=PASSWORD_SALT, n=16384, r=8, p=1, dklen=32
)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PUBLIC_API_PATHS = frozenset({"/api/auth/login", "/api/auth/session"})


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    def initialize_database() -> None:
        database = Database(DATABASE_PATH, initialize=True)
        try:
            database.initialize_runtime_config(
                important_keywords=INITIAL_IMPORTANT_KEYWORDS,
                trusted_sender_ids=INITIAL_TRUSTED_SENDER_IDS,
                watch_chat_ids=INITIAL_WATCH_CHAT_IDS,
                immediate_score=INITIAL_IMMEDIATE_SCORE,
                now=utc_now(),
            )
        finally:
            database.close()

    await asyncio.to_thread(initialize_database)
    try:
        yield
    finally:
        SESSION_STORE.clear()
        LOGIN_LIMITER.clear()
        await MODEL_CLIENT.close()

app = FastAPI(
    title="群讯雷达管理界面",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


T = TypeVar("T")


def _database_call(callback: Callable[[Database], T]) -> T:
    database = Database(DATABASE_PATH, initialize=False)
    try:
        return callback(database)
    finally:
        database.close()


async def database_call(callback: Callable[[Database], T]) -> T:
    """Keep SQLite work off the async HTTP event loop."""
    return await asyncio.to_thread(_database_call, callback)


def _response_with_security_headers(response: Response, request: Request) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers["Vary"] = "Origin, Referer"
    if request.url.path.startswith("/api/") or not request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


def _json_error(request: Request, status_code: int, detail: str) -> JSONResponse:
    return _response_with_security_headers(
        JSONResponse({"detail": detail}, status_code=status_code),
        request,
    )


def _origin_tuple(parsed: SplitResult) -> tuple[str, str, int] | None:
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.casefold(), effective_port


def _same_origin(request: Request) -> bool:
    host = request.headers.get("host", "").strip()
    if not host:
        return False
    scheme = (
        request.headers.get("x-forwarded-proto", "").strip().casefold()
        or request.url.scheme
    )
    target = _origin_tuple(urlsplit(f"{scheme}://{host}"))
    if target is None:
        return False
    sources = [
        value.strip()
        for value in (
            request.headers.get("origin"),
            request.headers.get("referer"),
        )
        if value and value.strip()
    ]
    if not sources:
        return False
    source_tuples = [_origin_tuple(urlsplit(source)) for source in sources]
    if any(st is None for st in source_tuples):
        return False
    return all(
        st == target or (st[1] == target[1] and st[0] in {"http", "https"})
        for st in source_tuples
    )


def _is_json_request(request: Request) -> bool:
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    return media_type.strip().casefold() == "application/json"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _delete_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=WEB_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    is_api = path.startswith("/api/")
    token = request.cookies.get(SESSION_COOKIE_NAME)
    session = SESSION_STORE.validate(token)
    request.state.admin_session = session

    if is_api and path not in PUBLIC_API_PATHS and session is None:
        return _json_error(request, 401, "需要登录")

    if is_api and request.method.upper() not in SAFE_METHODS:
        if not _is_json_request(request):
            return _json_error(request, 415, "只接受 application/json 请求")
        if request.headers.get("x-requested-with") != "admin-ui":
            return _json_error(request, 403, "缺少管理界面请求标识")
        if not _same_origin(request):
            return _json_error(request, 403, "请求来源验证失败")

    response = await call_next(request)
    return _response_with_security_headers(response, request)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1_024)


class RuntimeConfigUpdate(BaseModel):
    important_keywords: list[str] = Field(min_length=1, max_length=100)
    trusted_sender_ids: list[int] = Field(default_factory=list, max_length=100)
    watch_chat_ids: list[int] = Field(default_factory=list, max_length=500)
    immediate_score: Literal[60] = PUSH_SCORE_THRESHOLD

    @field_validator("important_keywords")
    @classmethod
    def validate_keywords(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not cleaned:
            raise ValueError("至少保留一个重要关键词")
        if any(len(value) > 64 for value in cleaned):
            raise ValueError("单个关键词不能超过 64 个字符")
        return cleaned


class ModelConfigUpdate(BaseModel):
    enabled: bool = False
    community_insights_enabled: bool = True
    benefit_deals_enabled: bool = True
    base_url: str = Field(default=DEFAULT_BASE_URL, min_length=1, max_length=512)
    api_key: str = Field(default="", max_length=2_048)
    clear_api_key: bool = False
    classification_model: str = Field(
        default=DEFAULT_CLASSIFICATION_MODEL, max_length=200
    )
    classification_reasoning_effort: str = Field(
        default=DEFAULT_CLASSIFICATION_REASONING_EFFORT, max_length=16
    )
    model: str = Field(default="", max_length=200)
    reasoning_effort: str = Field(default=DEFAULT_REASONING_EFFORT, max_length=16)
    semantic_dedupe_model: str = Field(
        default=DEFAULT_SEMANTIC_DEDUPE_MODEL, max_length=200
    )
    semantic_dedupe_reasoning_effort: str = Field(
        default=DEFAULT_SEMANTIC_DEDUPE_REASONING_EFFORT, max_length=16
    )
    notification_model: str = Field(default=DEFAULT_NOTIFICATION_MODEL, max_length=200)
    notification_reasoning_effort: str = Field(
        default=DEFAULT_NOTIFICATION_REASONING_EFFORT, max_length=16
    )

    @field_validator(
        "classification_reasoning_effort",
        "reasoning_effort",
        "semantic_dedupe_reasoning_effort",
        "notification_reasoning_effort",
    )
    @classmethod
    def validate_effort(cls, value: str) -> str:
        return validate_reasoning_effort(value)


class ModelRefreshRequest(BaseModel):
    base_url: str = Field(default=DEFAULT_BASE_URL, min_length=1, max_length=512)
    api_key: str = Field(default="", max_length=2_048)


class PushConfigUpdate(BaseModel):
    telegram_enabled: bool = True
    telegram_bot_token: str = Field(default="", max_length=2_048)
    clear_telegram_bot_token: bool = False
    telegram_chat_id: str = Field(default="", max_length=128)
    ntfy_enabled: bool = False
    ntfy_base_url: str = Field(
        default=DEFAULT_NTFY_BASE_URL, min_length=1, max_length=512
    )
    ntfy_topic: str = Field(default="", max_length=128)
    ntfy_community_topic: str | None = Field(default=None, max_length=128)
    ntfy_benefit_topic: str | None = Field(default=None, max_length=128)
    ntfy_access_token: str = Field(default="", max_length=2_048)
    clear_ntfy_access_token: bool = False


class PushTestRequest(BaseModel):
    channel: Literal["telegram", "ntfy"]


class InformationSourceCreate(BaseModel):
    kind: Literal["rss", "github_releases", "github_advisories", "cisa_kev", "nvd_cve", "vendor_status", "hacker_news", "bluesky", "mastodon", "newsletter_imap"] = "rss"
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(default="", max_length=2_048)
    enabled: bool = True
    poll_interval_minutes: int = Field(default=15, ge=5, le=1440)
    include_prereleases: bool = False
    ecosystem: str = Field(default="", max_length=32)
    minimum_severity: Literal["high", "critical"] = "high"
    keywords: list[str] = Field(default_factory=list, max_length=30)
    story_list: Literal["top", "best", "both"] = "best"
    minimum_score: int = Field(default=150, ge=20, le=5_000)
    bluesky_handle: str = Field(default="", max_length=253)
    mastodon_instance_url: str = Field(default="", max_length=2_048)
    mastodon_timeline_type: Literal["account", "tag"] = "account"
    mastodon_target: str = Field(default="", max_length=128)
    source_secret: str = Field(default="", max_length=2_048)
    clear_source_secret: bool = False
    imap_host: str = Field(default="", max_length=253)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_username: str = Field(default="", max_length=254)
    imap_mailbox: str = Field(default="INBOX", max_length=128)
    sender_allowlist: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("信息源名称不能为空")
        return cleaned

    @model_validator(mode="after")
    def clean_source(self) -> "InformationSourceCreate":
        if self.kind == "rss":
            self.url = validate_feed_url(self.url)
        elif self.kind == "github_releases":
            repository = validate_github_repository(self.url)
            self.url = github_releases_web_url(repository)
        elif self.kind == "github_advisories":
            self.url = github_advisories_web_url(
                {
                    "ecosystem": self.ecosystem,
                    "minimum_severity": self.minimum_severity,
                    "keywords": self.keywords,
                }
            )
        elif self.kind == "cisa_kev":
            self.url = CISA_KEV_WEB_URL
        elif self.kind == "vendor_status":
            self.url = validate_status_page_url(self.url)
        elif self.kind == "hacker_news":
            self.url = HN_WEB_URL
        elif self.kind == "bluesky":
            self.url = bluesky_profile_url(self.bluesky_handle)
        elif self.kind == "mastodon":
            self.url = mastodon_source_url(
                {
                    "instance_url": self.mastodon_instance_url,
                    "timeline_type": self.mastodon_timeline_type,
                    "target": self.mastodon_target,
                }
            )
        elif self.kind == "newsletter_imap":
            self.url = imap_source_url(
                {
                    "host": self.imap_host,
                    "port": self.imap_port,
                    "username": self.imap_username,
                    "mailbox": self.imap_mailbox,
                    "sender_allowlist": self.sender_allowlist,
                }
            )
        else:
            self.url = NVD_SOURCE_WEB_URL
        return self

    def settings(self) -> dict[str, object]:
        if self.kind == "github_releases":
            return {
                "repository": validate_github_repository(self.url),
                "include_prereleases": self.include_prereleases,
            }
        if self.kind == "github_advisories":
            return validate_advisory_settings(
                {
                    "ecosystem": self.ecosystem,
                    "minimum_severity": self.minimum_severity,
                    "keywords": self.keywords,
                }
            )
        if self.kind == "nvd_cve":
            return validate_nvd_settings({"keywords": self.keywords})
        if self.kind == "hacker_news":
            return validate_hacker_news_settings(
                {
                    "story_list": self.story_list,
                    "minimum_score": self.minimum_score,
                    "keywords": self.keywords,
                }
            )
        if self.kind == "bluesky":
            return {"handle": validate_bluesky_handle(self.bluesky_handle)}
        if self.kind == "mastodon":
            return validate_mastodon_settings(
                {
                    "instance_url": self.mastodon_instance_url,
                    "timeline_type": self.mastodon_timeline_type,
                    "target": self.mastodon_target,
                }
            )
        if self.kind == "newsletter_imap":
            return validate_imap_settings(
                {
                    "host": self.imap_host,
                    "port": self.imap_port,
                    "username": self.imap_username,
                    "mailbox": self.imap_mailbox,
                    "sender_allowlist": self.sender_allowlist,
                }
            )
        return {}


class InformationSourceUpdate(InformationSourceCreate):
    pass


class SourceProviderConfigUpdate(BaseModel):
    github_token: str = Field(default="", max_length=2_048)
    clear_github_token: bool = False
    nvd_api_key: str = Field(default="", max_length=2_048)
    clear_nvd_api_key: bool = False


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, __: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {"detail": "请求字段格式无效"},
        status_code=422,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/auth/session")
async def auth_session(request: Request) -> Response:
    session = request.state.admin_session
    response = JSONResponse(
        {
            "authenticated": session is not None,
            "username": session.username if session is not None else None,
        }
    )
    if session is None and request.cookies.get(SESSION_COOKIE_NAME):
        _delete_session_cookie(response)
    return response


@app.post("/api/auth/login")
async def auth_login(payload: LoginRequest, request: Request) -> Response:
    client_ip = _client_ip(request)
    if not LOGIN_LIMITER.allowed(client_ip):
        raise HTTPException(status_code=429, detail="登录暂不可用，请稍后再试")

    submitted_username = hashlib.sha256(payload.username.encode("utf-8")).digest()
    submitted_password = hashlib.scrypt(
        payload.password.encode("utf-8"), salt=PASSWORD_SALT, n=16384, r=8, p=1, dklen=32
    )
    username_matches = secrets.compare_digest(submitted_username, USERNAME_DIGEST)
    password_matches = secrets.compare_digest(submitted_password, PASSWORD_DIGEST)
    if not (username_matches & password_matches):
        LOGIN_LIMITER.record_failure(client_ip)
        raise HTTPException(status_code=401, detail="用户名或密码不正确")

    LOGIN_LIMITER.record_success(client_ip)
    SESSION_STORE.revoke(request.cookies.get(SESSION_COOKIE_NAME))
    token, session = SESSION_STORE.create(WEB_USERNAME)
    response = JSONResponse(
        {"authenticated": True, "username": session.username}
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        secure=WEB_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> Response:
    SESSION_STORE.revoke(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({"authenticated": False})
    _delete_session_cookie(response)
    return response


@app.get("/api/stats")
async def stats(hours: int = Query(24, ge=1, le=720)) -> dict:
    return await database_call(
        lambda database: database.dashboard_stats(hours=hours, now=utc_now())
    )


@app.get("/api/status")
async def status(
    hours: int = Query(24, ge=1, le=720),
    history_minutes: int = Query(60, ge=15, le=180),
) -> dict:
    now = utc_now()

    def load_status(database: Database) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return (
            database.dashboard_stats(hours=hours, now=now),
            database.queue_metric_history(minutes=history_minutes, now=now),
        )

    value, queue_history = await database_call(load_status)
    return {
        "heartbeat": value["heartbeat"],
        "analysis_queue": value["analysis_queue"],
        "delivery_queue": value["delivery_queue"],
        "latest_message_at": value["latest_message_at"],
        "queue_history": queue_history,
        "queue_history_meta": {
            "sample_interval_seconds": QUEUE_METRIC_SAMPLE_INTERVAL_SECONDS,
            "retention_days": QUEUE_METRIC_RETENTION_DAYS,
        },
    }


@app.get("/api/chats")
async def chats(
    hours: int = Query(168, ge=1, le=720),
    recorded_only: bool = False,
    include_sources: bool = False,
) -> dict:
    items = await database_call(
        lambda database: database.list_chat_options(
            hours=hours,
            now=utc_now(),
            recorded_only=recorded_only,
            include_sources=include_sources,
        )
    )
    return {"items": items}


@app.get("/api/sources")
async def information_sources() -> dict:
    items = await database_call(lambda database: database.list_information_sources())
    return {"items": items}


@app.post("/api/sources")
async def create_information_source(payload: InformationSourceCreate) -> dict:
    if payload.kind == "newsletter_imap" and not payload.source_secret:
        raise HTTPException(status_code=400, detail="邮件 Newsletter 必须填写邮箱密码")
    try:
        item = await database_call(
            lambda database: database.create_information_source(
                kind=payload.kind,
                name=payload.name,
                url=payload.url,
                settings=payload.settings(),
                enabled=payload.enabled,
                poll_interval_minutes=payload.poll_interval_minutes,
                now=utc_now(),
                secret_value=payload.source_secret if payload.kind in {"mastodon", "newsletter_imap"} else "",
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"item": item}


@app.put("/api/sources/{source_id}")
async def update_information_source(
    source_id: int, payload: InformationSourceUpdate
) -> dict:
    try:
        item = await database_call(
            lambda database: database.update_information_source(
                source_id,
                kind=payload.kind,
                name=payload.name,
                url=payload.url,
                settings=payload.settings(),
                enabled=payload.enabled,
                poll_interval_minutes=payload.poll_interval_minutes,
                now=utc_now(),
                secret_value=payload.source_secret if payload.kind in {"mastodon", "newsletter_imap"} else "",
                clear_secret=payload.clear_source_secret,
            )
        )
    except ValueError as exc:
        status = 404 if str(exc) == "信息源不存在" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from None
    return {"item": item}


@app.get("/api/source-provider-config")
async def source_provider_config() -> dict:
    github = await database_call(
        lambda database: database.get_source_provider_credential("github")
    )
    nvd = await database_call(
        lambda database: database.get_source_provider_credential("nvd")
    )
    return {
        "github_token_configured": github["configured"],
        "nvd_api_key_configured": nvd["configured"],
        "updated_at": max(
            (value for value in (github["updated_at"], nvd["updated_at"]) if value),
            default=None,
        ),
    }


@app.put("/api/source-provider-config")
async def update_source_provider_config(payload: SourceProviderConfigUpdate) -> dict:
    github = await database_call(
        lambda database: database.update_source_provider_credential(
            "github",
            secret_value=payload.github_token,
            clear_secret=payload.clear_github_token,
            now=utc_now(),
        )
    )
    nvd = await database_call(
        lambda database: database.update_source_provider_credential(
            "nvd",
            secret_value=payload.nvd_api_key,
            clear_secret=payload.clear_nvd_api_key,
            now=utc_now(),
        )
    )
    return {
        "github_token_configured": github["configured"],
        "nvd_api_key_configured": nvd["configured"],
        "updated_at": max(
            (value for value in (github["updated_at"], nvd["updated_at"]) if value),
            default=None,
        ),
    }


@app.post("/api/sources/{source_id}/refresh")
async def refresh_information_source(source_id: int) -> dict:
    try:
        item = await database_call(
            lambda database: database.request_information_source_refresh(
                source_id, now=utc_now()
            )
        )
    except ValueError as exc:
        status = 404 if str(exc) == "信息源不存在" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from None
    return {"item": item}


@app.get("/api/messages")
async def messages(
    hours: int = Query(24, ge=1, le=720),
    q: str = Query("", max_length=120),
    chat_id: int | None = None,
    min_score: int = Query(0, ge=-100, le=500),
    push_status: str = Query(
        "all",
        pattern="^(all|immediate|digest|eligible|not_pushed)$",
    ),
    prefilter_status: str = Query("exclude", pattern="^(exclude|all|filtered)$"),
    similar_status: str = Query("exclude", pattern="^(exclude|all|suppressed)$"),
    attention_only: bool = False,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    return await database_call(
        lambda database: database.list_messages(
            hours=hours,
            now=utc_now(),
            query=q.strip(),
            chat_id=chat_id,
            min_score=min_score,
            push_status=push_status,
            prefilter_status=prefilter_status,
            similar_status=similar_status,
            attention_only=attention_only,
            limit=limit,
            offset=offset,
        )
    )


@app.get("/api/messages/{row_id}")
async def message_detail(row_id: int) -> dict:
    row = await database_call(
        lambda database: database.get_message_by_id(row_id, include_related=True)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="消息不存在")
    return row


@app.get("/api/config")
async def get_config() -> dict:
    runtime = await database_call(lambda database: database.get_runtime_config())
    if runtime is None:
        raise HTTPException(status_code=500, detail="运行时配置不可用")
    return {
        **runtime,
        "important_keywords": list(runtime["important_keywords"]),
        "trusted_sender_ids": sorted(runtime["trusted_sender_ids"]),
        "watch_chat_ids": sorted(runtime["watch_chat_ids"]),
        "retention_days": RETENTION_DAYS,
    }


@app.put("/api/config")
async def update_config(
    payload: RuntimeConfigUpdate,
) -> dict:
    def save(database: Database) -> dict:
        available_ids = database.current_available_chat_ids()
        unknown_ids = set(payload.watch_chat_ids) - available_ids
        if unknown_ids:
            raise HTTPException(status_code=422, detail="监听列表包含账号当前不可见的会话")
        return database.update_runtime_config(
            important_keywords=tuple(payload.important_keywords),
            trusted_sender_ids=frozenset(payload.trusted_sender_ids),
            watch_chat_ids=frozenset(payload.watch_chat_ids),
            immediate_score=PUSH_SCORE_THRESHOLD,
            now=utc_now(),
        )
    runtime = await database_call(save)
    return {
        **runtime,
        "important_keywords": list(runtime["important_keywords"]),
        "trusted_sender_ids": sorted(runtime["trusted_sender_ids"]),
        "watch_chat_ids": sorted(runtime["watch_chat_ids"]),
    }


@app.get("/api/model-config")
async def get_model_config() -> dict:
    return await database_call(lambda database: database.get_model_config())


@app.put("/api/model-config")
async def update_model_config(
    payload: ModelConfigUpdate,
) -> dict:
    if payload.clear_api_key and payload.api_key:
        raise HTTPException(status_code=422, detail="清除密钥时不能同时提交新密钥")
    try:
        base_url = validate_base_url(payload.base_url)
        classification_model = validate_model_id(payload.classification_model)
        model = validate_model_id(payload.model) if payload.model.strip() else None
        semantic_dedupe_model = validate_model_id(payload.semantic_dedupe_model)
        notification_model = validate_model_id(payload.notification_model)
        return await database_call(
            lambda database: database.update_model_config(
                enabled=payload.enabled,
                community_insights_enabled=payload.community_insights_enabled,
                benefit_deals_enabled=payload.benefit_deals_enabled,
                base_url=base_url,
                classification_model=classification_model,
                model=model,
                api_key=payload.api_key.strip(),
                clear_api_key=payload.clear_api_key,
                now=utc_now(),
                classification_reasoning_effort=payload.classification_reasoning_effort,
                reasoning_effort=payload.reasoning_effort,
                semantic_dedupe_model=semantic_dedupe_model,
                semantic_dedupe_reasoning_effort=payload.semantic_dedupe_reasoning_effort,
                notification_model=notification_model,
                notification_reasoning_effort=payload.notification_reasoning_effort,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/api/model-config/models")
async def refresh_models(
    payload: ModelRefreshRequest,
) -> dict:
    try:
        base_url = validate_base_url(payload.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    saved = await database_call(
        lambda database: database.get_model_config(include_api_key=True)
    )
    api_key = payload.api_key.strip() or saved.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        raise HTTPException(status_code=422, detail="API Key 尚未配置")
    try:
        models = await MODEL_CLIENT.list_models(base_url=base_url, api_key=api_key)
    except ModelClientError as exc:
        raise HTTPException(status_code=502, detail=exc.public_message) from None
    return {"items": models}


@app.get("/api/push-config")
async def get_push_config() -> dict:
    return await database_call(lambda database: database.get_push_config())


@app.get("/api/feedback")
async def feedback_records(
    hours: int = Query(2160, ge=1, le=2160),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    return await database_call(
        lambda database: database.feedback_dashboard(
            hours=hours,
            now=utc_now(),
            limit=limit,
            offset=offset,
        )
    )


@app.put("/api/push-config")
async def update_push_config(payload: PushConfigUpdate) -> dict:
    try:
        return await database_call(
            lambda database: database.update_push_config(
                telegram_enabled=payload.telegram_enabled,
                telegram_bot_token=payload.telegram_bot_token,
                clear_telegram_bot_token=payload.clear_telegram_bot_token,
                telegram_chat_id=payload.telegram_chat_id,
                ntfy_enabled=payload.ntfy_enabled,
                ntfy_base_url=payload.ntfy_base_url,
                ntfy_topic=payload.ntfy_topic,
                ntfy_community_topic=payload.ntfy_community_topic,
                ntfy_benefit_topic=payload.ntfy_benefit_topic,
                ntfy_access_token=payload.ntfy_access_token,
                clear_ntfy_access_token=payload.clear_ntfy_access_token,
                now=utc_now(),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/api/push-config/test")
async def test_push_config(payload: PushTestRequest) -> dict:
    if PUSH_TEST_LOCK.locked():
        raise HTTPException(status_code=409, detail="已有推送渠道正在测试")
    async with PUSH_TEST_LOCK:
        config = await database_call(
            lambda database: database.get_push_config(include_secrets=True)
        )
        try:
            sent = await send_push_test(config, payload.channel)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if not sent:
            raise HTTPException(status_code=502, detail="测试推送失败，请检查渠道配置")
    return {"channel": payload.channel, "sent": True}


@app.post("/api/messages/{row_id}/reanalyze", status_code=202)
async def reanalyze_message(
    row_id: int,
) -> dict:
    def enqueue(database: Database) -> dict:
        row = database.get_message_by_id(row_id)
        if row is None:
            raise HTTPException(status_code=404, detail="消息不存在")
        job, created = database.enqueue_manual_analysis(row_id, now=utc_now())
        queued = database.get_message_by_id(row_id)
        if queued is None:
            raise HTTPException(status_code=404, detail="消息不存在")
        queued["queue_created"] = created
        queued["analysis_job_id"] = int(job["id"])
        return queued

    return await database_call(enqueue)


if not STATIC_DIR.exists():
    raise RuntimeError("Web 静态文件不存在，请先构建前端")

app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/{path:path}", response_model=None)
async def spa_fallback(path: str):
    if path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})
