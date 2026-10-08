from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.database import Database, utc_now
from app.push_config import validate_ntfy_base_url, validate_ntfy_topic


LOGGER = logging.getLogger("telegram_priority.ntfy_feedback")

FEEDBACK_TOKEN_VERSION = "v1"
FEEDBACK_VOTES = frozenset({"up", "down"})
FEEDBACK_POLL_INTERVAL_SECONDS = 300
FEEDBACK_RESPONSE_MAX_BYTES = 256 * 1024
FEEDBACK_MAX_EVENTS_PER_POLL = 200
FEEDBACK_HTTP_TIMEOUT = httpx.Timeout(
    25.0,
    connect=5.0,
    read=20.0,
    write=5.0,
    pool=5.0,
)
_FEEDBACK_TOKEN_RE = re.compile(
    r"^v1\.([A-Za-z0-9_-]{16,64})\.(up|down)\.([0-9]{10})\.([A-Za-z0-9_-]{43})$"
)
_NTFY_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True, slots=True)
class FeedbackVote:
    public_id: str
    vote: str
    expires_at: int


class FeedbackCollectorError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _signing_key_bytes(signing_key: str) -> bytes:
    try:
        value = bytes.fromhex(signing_key)
    except ValueError as exc:
        raise ValueError("ntfy 反馈签名配置无效") from exc
    if len(value) != 32:
        raise ValueError("ntfy 反馈签名配置无效")
    return value


def _feedback_signature(
    signing_key: str,
    public_id: str,
    vote: str,
    expires_at: int,
) -> str:
    material = f"{FEEDBACK_TOKEN_VERSION}:{public_id}:{vote}:{expires_at}".encode("ascii")
    digest = hmac.new(_signing_key_bytes(signing_key), material, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_feedback_token(
    *,
    signing_key: str,
    public_id: str,
    vote: str,
    expires_at: int,
) -> str:
    if vote not in FEEDBACK_VOTES or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", public_id):
        raise ValueError("ntfy 反馈目标无效")
    if not 1_000_000_000 <= int(expires_at) <= 9_999_999_999:
        raise ValueError("ntfy 反馈有效期无效")
    signature = _feedback_signature(signing_key, public_id, vote, int(expires_at))
    return f"{FEEDBACK_TOKEN_VERSION}.{public_id}.{vote}.{int(expires_at)}.{signature}"


def parse_feedback_token(
    value: object,
    *,
    signing_key: str,
    now: datetime,
) -> FeedbackVote | None:
    raw = str(value or "").strip()
    if len(raw) > 256:
        return None
    match = _FEEDBACK_TOKEN_RE.fullmatch(raw)
    if match is None:
        return None
    public_id, vote, expires_raw, supplied_signature = match.groups()
    expires_at = int(expires_raw)
    if expires_at < int(now.timestamp()):
        return None
    try:
        expected_signature = _feedback_signature(
            signing_key,
            public_id,
            vote,
            expires_at,
        )
    except ValueError:
        return None
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None
    return FeedbackVote(public_id=public_id, vote=vote, expires_at=expires_at)


def build_feedback_actions(
    *,
    base_url: str,
    feedback_topic: str,
    signing_key: str,
    public_id: str,
    expires_at: int,
) -> tuple[dict[str, Any], ...]:
    normalized_base = validate_ntfy_base_url(base_url)
    normalized_topic = validate_ntfy_topic(feedback_topic, required=True)
    endpoint = f"{normalized_base}/{quote(normalized_topic, safe='')}"
    return tuple(
        {
            "action": "http",
            "label": label,
            "url": endpoint,
            "method": "POST",
            "headers": {"Content-Type": "text/plain; charset=utf-8"},
            "body": build_feedback_token(
                signing_key=signing_key,
                public_id=public_id,
                vote=vote,
                expires_at=expires_at,
            ),
            "clear": False,
        }
        for vote, label in (("up", "👍 有用"), ("down", "👎 无用"))
    )


def _database_call(path: str, method: str, *args: Any, **kwargs: Any) -> Any:
    database = Database(path, initialize=False)
    try:
        return getattr(database, method)(*args, **kwargs)
    finally:
        database.close()


class NtfyFeedbackCollector:
    """Poll an opaque ntfy feedback topic without opening an inbound port."""

    def __init__(
        self,
        database_path: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_seconds: int = FEEDBACK_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._database_path = database_path
        self._poll_interval_seconds = max(1, int(poll_interval_seconds))
        self._client = httpx.AsyncClient(
            timeout=FEEDBACK_HTTP_TIMEOUT,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "telegram-priority-feedback/1.0"},
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ntfy-feedback-collector")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._client.aclose()

    async def poll_once(self) -> bool:
        config = await asyncio.to_thread(
            _database_call,
            self._database_path,
            "get_push_config",
            include_secrets=True,
        )
        ntfy = config["ntfy"]
        if not ntfy.get("enabled"):
            return False
        feedback_topic = ntfy.get("feedback_topic")
        signing_key = ntfy.get("feedback_signing_key")
        if not feedback_topic or not signing_key:
            raise FeedbackCollectorError("configuration")
        base_url = validate_ntfy_base_url(str(ntfy["base_url"]))
        topic = validate_ntfy_topic(str(feedback_topic), required=True)
        runtime = await asyncio.to_thread(
            _database_call,
            self._database_path,
            "get_ntfy_feedback_runtime",
        )
        endpoint = f"{base_url}/{quote(topic, safe='')}/json"
        headers: dict[str, str] = {}
        if ntfy.get("access_token"):
            headers["Authorization"] = f"Bearer {ntfy['access_token']}"
        try:
            async with self._client.stream(
                "GET",
                endpoint,
                params={"poll": "1", "since": runtime.get("cursor_value") or "all"},
                headers=headers,
            ) as response:
                if response.status_code in {401, 403}:
                    raise FeedbackCollectorError("authentication_error")
                if response.status_code == 429:
                    raise FeedbackCollectorError("rate_limited")
                if 500 <= response.status_code < 600:
                    raise FeedbackCollectorError("upstream_error")
                if not 200 <= response.status_code < 300:
                    raise FeedbackCollectorError("request_rejected")
                chunks: list[bytes] = []
                response_size = 0
                async for chunk in response.aiter_bytes():
                    response_size += len(chunk)
                    if response_size > FEEDBACK_RESPONSE_MAX_BYTES:
                        raise FeedbackCollectorError("response_too_large")
                    chunks.append(chunk)
                try:
                    response_text = b"".join(chunks).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise FeedbackCollectorError("invalid_response") from exc
        except httpx.TimeoutException as exc:
            raise FeedbackCollectorError("timeout") from exc
        except httpx.HTTPError as exc:
            raise FeedbackCollectorError("network_error") from exc

        cursor_value = runtime.get("cursor_value")
        received = False
        lines = response_text.splitlines()
        if len(lines) > FEEDBACK_MAX_EVENTS_PER_POLL:
            raise FeedbackCollectorError("response_too_large")
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FeedbackCollectorError("invalid_response") from exc
            if not isinstance(event, dict):
                raise FeedbackCollectorError("invalid_response")
            event_id = str(event.get("id") or "")
            if not _NTFY_EVENT_ID_RE.fullmatch(event_id):
                continue
            cursor_value = event_id
            if event.get("event") != "message":
                continue
            vote = parse_feedback_token(
                event.get("message"),
                signing_key=str(signing_key),
                now=utc_now(),
            )
            if vote is None:
                continue
            try:
                event_time = int(event.get("time") or 0)
            except (TypeError, ValueError):
                continue
            if event_time <= 0 or event_time > int(utc_now().timestamp()) + 300:
                continue
            accepted = await asyncio.to_thread(
                _database_call,
                self._database_path,
                "record_ntfy_feedback",
                event_id=event_id,
                public_id=vote.public_id,
                vote=vote.vote,
                expires_at=vote.expires_at,
                event_time=event_time,
                now=utc_now(),
            )
            received = received or bool(accepted)
        await asyncio.to_thread(
            _database_call,
            self._database_path,
            "complete_ntfy_feedback_poll",
            cursor_value=cursor_value,
            received=received,
            now=utc_now(),
        )
        return received

    async def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                await self.poll_once()
                failures = 0
                delay = self._poll_interval_seconds
            except asyncio.CancelledError:
                raise
            except FeedbackCollectorError as exc:
                failures += 1
                await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "fail_ntfy_feedback_poll",
                    error_category=exc.category,
                    now=utc_now(),
                )
                LOGGER.warning("ntfy 反馈收取异常：category=%s", exc.category)
                delay = min(300, self._poll_interval_seconds * (2 ** min(failures - 1, 5)))
            except Exception as exc:
                failures += 1
                await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "fail_ntfy_feedback_poll",
                    error_category="internal_error",
                    now=utc_now(),
                )
                LOGGER.error("ntfy 反馈收取异常：error=%s", type(exc).__name__)
                delay = min(300, self._poll_interval_seconds * (2 ** min(failures - 1, 5)))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
