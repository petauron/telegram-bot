from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.database import Database, MessageRecord, information_source_chat_id, to_iso, utc_now
from app.feeds import FeedEntry, FeedError, FeedFetchResult, ParsedFeed, clean_feed_html
from app.scoring import normalize_text, score_message


LOGGER = logging.getLogger("telegram_priority.github_releases")
GITHUB_RELEASE_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
GITHUB_RELEASE_LIMIT = 100
GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
GITHUB_REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=15.0, write=5.0, pool=5.0)


def validate_github_repository(value: str) -> str:
    raw = value.strip()
    if raw.startswith("https://"):
        parsed = urlsplit(raw)
        if (
            parsed.hostname not in {"github.com", "www.github.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("GitHub 仓库地址无效")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[2] == "releases":
            parts = parts[:2]
        raw = "/".join(parts)
    raw = raw.removesuffix(".git").strip("/")
    if not GITHUB_REPOSITORY_PATTERN.fullmatch(raw) or ".." in raw:
        raise ValueError("GitHub 仓库必须使用 owner/repo 格式")
    return raw


def github_releases_web_url(repository: str) -> str:
    return f"https://github.com/{validate_github_repository(repository)}/releases"


def _repository_from_source(source: dict[str, Any]) -> str:
    settings = source.get("settings") or {}
    value = settings.get("repository") or source.get("url") or ""
    return validate_github_repository(str(value))


def parse_github_releases(
    payload: bytes, *, repository: str, include_prereleases: bool
) -> ParsedFeed:
    if not payload or len(payload) > GITHUB_RELEASE_RESPONSE_MAX_BYTES:
        raise FeedError("response_too_large")
    try:
        rows = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise FeedError("invalid_response") from exc
    if not isinstance(rows, list):
        raise FeedError("invalid_response")
    entries: list[FeedEntry] = []
    for row in rows[:GITHUB_RELEASE_LIMIT]:
        if not isinstance(row, dict) or row.get("draft"):
            continue
        prerelease = bool(row.get("prerelease"))
        if prerelease and not include_prereleases:
            continue
        release_id = row.get("id")
        tag = str(row.get("tag_name") or "").strip()[:200]
        if not isinstance(release_id, int) or not tag:
            continue
        name = clean_feed_html(str(row.get("name") or ""), limit=240)
        kind = "预发布" if prerelease else "正式发布"
        title = f"{repository} {name or tag} · {kind}"[:300]
        body = clean_feed_html(str(row.get("body") or ""), limit=12_000)
        html_url = str(row.get("html_url") or "").strip()
        if not html_url.startswith(f"https://github.com/{repository}/releases/"):
            html_url = github_releases_web_url(repository)
        published = str(row.get("published_at") or row.get("created_at") or "").strip() or None
        entries.append(
            FeedEntry(
                external_id=f"github-release:{release_id}",
                title=title,
                body=body,
                url=html_url,
                published_at=published,
            )
        )
    return ParsedFeed(tuple(entries))


class GitHubReleasesFetcher:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=GITHUB_REQUEST_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "TelegramPriorityGitHubReader/1.0",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, source: dict[str, Any], *, token: str = "") -> FeedFetchResult:
        repository = _repository_from_source(source)
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if source.get("etag"):
            headers["If-None-Match"] = str(source["etag"])
        try:
            async with self._client.stream(
                "GET", f"https://api.github.com/repos/{repository}/releases?per_page=100", headers=headers
            ) as response:
                if response.status_code == 304:
                    return FeedFetchResult(True, None, source.get("etag"), None, 304)
                if 300 <= response.status_code < 400:
                    raise FeedError("redirect_blocked")
                if response.status_code == 429 or (
                    response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"
                ):
                    raise FeedError("rate_limited")
                if 400 <= response.status_code < 500:
                    raise FeedError("http_4xx")
                if response.status_code >= 500:
                    raise FeedError("http_5xx")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > GITHUB_RELEASE_RESPONSE_MAX_BYTES:
                        raise FeedError("response_too_large")
                settings = source.get("settings") or {}
                feed = parse_github_releases(
                    bytes(content),
                    repository=repository,
                    include_prereleases=bool(settings.get("include_prereleases")),
                )
                return FeedFetchResult(
                    False,
                    feed,
                    response.headers.get("etag")[:512] if response.headers.get("etag") else None,
                    None,
                    response.status_code,
                )
        except FeedError:
            raise
        except httpx.TimeoutException as exc:
            raise FeedError("timeout") from exc
        except httpx.HTTPError as exc:
            raise FeedError("network_error") from exc


def _database_call(path: str, method: str, *args: Any, **kwargs: Any) -> Any:
    database = Database(path, initialize=False)
    try:
        return getattr(database, method)(*args, **kwargs)
    finally:
        database.close()


class GitHubSourcePoller:
    SOURCE_KIND = "github_releases"
    SENDER_NAME = "GitHub Releases"
    LOG_LABEL = "GitHub Releases"
    FETCHER_TYPE = GitHubReleasesFetcher
    CREDENTIAL_PROVIDER: str | None = "github"
    SOURCE_CREDENTIAL = False

    def __init__(self, database_path: str, analysis_queue: Any, *, fetcher: Any | None = None) -> None:
        self._database_path = database_path
        self._analysis_queue = analysis_queue
        self._fetcher = fetcher or self.FETCHER_TYPE()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> int:
        recovered = await asyncio.to_thread(
            _database_call, self._database_path, "recover_information_source_polls",
            now=utc_now(), kind=self.SOURCE_KIND,
        )
        self._tasks = [
            asyncio.create_task(
                self._worker(), name=f"{self.SOURCE_KIND.replace('_', '-')}-poller"
            )
        ]
        self._wake.set()
        return int(recovered)

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await asyncio.to_thread(
            _database_call, self._database_path, "release_information_source_polls",
            now=utc_now(), kind=self.SOURCE_KIND,
        )
        await self._fetcher.close()

    async def _worker(self) -> None:
        while not self._stop.is_set():
            source = await asyncio.to_thread(
                _database_call, self._database_path, "claim_due_information_source",
                now=utc_now(), lease_seconds=90, kind=self.SOURCE_KIND,
            )
            if source is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                except TimeoutError:
                    pass
                continue
            await self._poll(source)

    async def _poll(self, source: dict[str, Any]) -> None:
        source_id = int(source["id"])
        generation = int(source["generation"])
        try:
            token = ""
            if self.SOURCE_CREDENTIAL:
                credential = await asyncio.to_thread(
                    _database_call, self._database_path, "get_information_source_credential",
                    source_id, include_secret=True,
                )
                token = str(credential.get("secret_value") or "")
            elif self.CREDENTIAL_PROVIDER:
                credential = await asyncio.to_thread(
                    _database_call, self._database_path, "get_source_provider_credential",
                    self.CREDENTIAL_PROVIDER, include_secret=True,
                )
                token = str(credential.get("secret_value") or "")
            result = await self._fetcher.fetch(source, token=token)
            if result.not_modified:
                await self._complete(source, result, 0, None, None)
                return
            assert result.feed is not None
            entries = result.feed.entries
            if not bool(source.get("initialized")):
                await asyncio.to_thread(
                    _database_call, self._database_path, "baseline_information_source",
                    source_id, generation=generation, entries=entries, now=utc_now(),
                    http_status=result.http_status, etag=result.etag,
                    last_modified=result.last_modified, cursor_value=result.cursor_value,
                )
                return
            new_items = 0
            last_item_at: str | None = None
            cursor_value: str | None = source.get("cursor_value")
            for entry in sorted(entries, key=lambda item: (item.published_at or "", item.external_id)):
                reserved = await asyncio.to_thread(
                    _database_call, self._database_path, "reserve_information_source_item",
                    source_id, generation=generation, external_id=entry.external_id,
                    title=entry.title, url=entry.url, published_at=entry.published_at, now=utc_now(),
                )
                if reserved is None or str(reserved.get("state")) != "pending":
                    continue
                observed_at = utc_now()
                runtime = await asyncio.to_thread(_database_call, self._database_path, "get_runtime_config")
                keywords = tuple(runtime.get("important_keywords") or ()) if runtime else ()
                normalized = normalize_text(entry.text)
                repeated = await asyncio.to_thread(
                    _database_call, self._database_path, "count_recent_normalized",
                    information_source_chat_id(source_id), normalized, observed_at - timedelta(minutes=15),
                )
                scoring = score_message(
                    entry.text, mentioned_me=False, reply_to_me=False, trusted_sender=False,
                    keywords=keywords, repeated_recently=bool(repeated),
                )
                item_id = int(reserved["id"])
                record = MessageRecord(
                    chat_id=information_source_chat_id(source_id), message_id=item_id,
                    chat_name=str(source["name"]), chat_username=None, sender_id=None,
                    sender_name=self.SENDER_NAME, sent_at=entry.published_at or to_iso(observed_at),
                    text=entry.text, reply_to_message_id=None, thread_root_id=item_id,
                    base_score=scoring.score, reasons=scoring.reasons, link=entry.url,
                    normalized_text=scoring.normalized_text,
                    primary_url=entry.url or scoring.primary_url, created_at=to_iso(observed_at),
                    source_type=self.SOURCE_KIND, source_id=source_id,
                    source_external_id=entry.external_id,
                )
                inserted = await asyncio.to_thread(
                    _database_call, self._database_path, "insert_message", record,
                    enqueue_analysis=True, now=observed_at,
                )
                await asyncio.to_thread(
                    _database_call, self._database_path, "link_information_source_item",
                    item_id, chat_id=record.chat_id, message_id=record.message_id, now=observed_at,
                )
                if inserted:
                    new_items += 1
                    self._analysis_queue.wake()
                cursor_value = entry.external_id
                last_item_at = entry.published_at or to_iso(observed_at)
            await self._complete(source, result, new_items, cursor_value, last_item_at)
        except asyncio.CancelledError:
            raise
        except FeedError as exc:
            await asyncio.to_thread(
                _database_call, self._database_path, "fail_information_source_poll",
                source_id, generation=generation, now=utc_now(), error_category=exc.category,
            )
            LOGGER.warning("%s 抓取失败：source_id=%s category=%s", self.LOG_LABEL, source_id, exc.category)
        except Exception as exc:
            await asyncio.to_thread(
                _database_call, self._database_path, "fail_information_source_poll",
                source_id, generation=generation, now=utc_now(), error_category="internal_error",
            )
            LOGGER.error("%s 处理异常：source_id=%s error=%s", self.LOG_LABEL, source_id, type(exc).__name__)

    async def _complete(
        self, source: dict[str, Any], result: FeedFetchResult, new_items: int,
        cursor_value: str | None, last_item_at: str | None,
    ) -> None:
        await asyncio.to_thread(
            _database_call, self._database_path, "complete_information_source_poll",
            int(source["id"]), generation=int(source["generation"]), now=utc_now(),
            http_status=result.http_status, etag=result.etag,
            last_modified=result.last_modified,
            new_items=new_items, cursor_value=result.cursor_value or cursor_value or source.get("cursor_value"),
            last_item_at=last_item_at,
        )


class GitHubReleasesPoller(GitHubSourcePoller):
    pass
