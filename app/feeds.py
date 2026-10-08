from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from app.database import (
    Database,
    MessageRecord,
    information_source_chat_id,
    to_iso,
    utc_now,
)
from app.scoring import normalize_text, score_message


LOGGER = logging.getLogger("telegram_priority.feeds")

FEED_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
FEED_ENTRY_LIMIT = 100
FEED_TITLE_MAX_CHARS = 300
FEED_BODY_MAX_CHARS = 12_000
FEED_URL_MAX_CHARS = 2_048
FEED_POLL_WORKERS = 2
FEED_POLL_LEASE_SECONDS = 90
FEED_REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=15.0, write=5.0, pool=5.0)
MALFORMED_TRAILING_CACHE_COMMENT_RE = re.compile(
    br"(?:\r?\n)?[ \t]*<!--Cached [0-9]{9,13}--->[ \t\r\n]*\Z"
)
TRACKING_PARAMETERS = frozenset(
    {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "source"}
)
SECRET_QUERY_PARAMETERS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "auth",
        "authorization",
        "key",
        "password",
        "secret",
        "signature",
        "sig",
        "token",
        "x-amz-credential",
        "x-amz-signature",
    }
)


class FeedError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True)
class FeedEntry:
    external_id: str
    title: str
    body: str
    url: str | None
    published_at: str | None

    @property
    def text(self) -> str:
        if self.title and self.body and self.body != self.title:
            return f"{self.title}\n\n{self.body}"
        return self.title or self.body


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    entries: tuple[FeedEntry, ...]


@dataclass(frozen=True, slots=True)
class FeedFetchResult:
    not_modified: bool
    feed: ParsedFeed | None
    etag: str | None
    last_modified: str | None
    http_status: int
    cursor_value: str | None = None


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_global
    except ValueError:
        return False


def validate_feed_url(value: str) -> str:
    raw = value.strip()
    if not raw or len(raw) > FEED_URL_MAX_CHARS:
        raise ValueError("Feed URL 长度无效")
    if any(character.isspace() or ord(character) < 32 for character in raw):
        raise ValueError("Feed URL 不能包含空白或控制字符")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Feed URL 格式无效") from exc
    if parsed.scheme.casefold() != "https":
        raise ValueError("Feed URL 仅允许公开 HTTPS 地址")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Feed URL 不能包含凭据且必须包含主机名")
    if parsed.fragment:
        raise ValueError("Feed URL 不能包含 fragment")
    if port not in {None, 443}:
        raise ValueError("Feed URL 仅允许标准 HTTPS 端口")
    hostname = parsed.hostname.rstrip(".").casefold()
    if not hostname or hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("Feed URL 不能指向本地主机")
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("Feed URL 不能指向私网或保留地址")
    if any(key.casefold() in SECRET_QUERY_PARAMETERS for key, _ in parse_qsl(parsed.query)):
        raise ValueError("暂不支持在 Feed URL 中携带凭据")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


async def resolve_public_feed_host(url: str) -> frozenset[str]:
    parsed = urlsplit(validate_feed_url(url))
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            parsed.hostname,
            443,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise FeedError("dns_error") from exc
    addresses = frozenset(str(record[4][0]).split("%", 1)[0] for record in records)
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise FeedError("unsafe_address")
    return addresses


def _safe_article_url(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw or len(raw) > FEED_URL_MAX_CHARS:
        return None
    if any(ord(character) < 32 for character in raw):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        return None
    hostname = parsed.hostname.rstrip(".").casefold()
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        return None
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.casefold() in SECRET_QUERY_PARAMETERS for key, _ in query_items):
        return None
    query = urlencode(
        [
            (key, item)
            for key, item in query_items
            if key.casefold() not in TRACKING_PARAMETERS
            and not key.casefold().startswith("utm_")
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", query, ""))


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = frozenset({"br", "p", "div", "li", "section", "article", "h1", "h2", "h3", "h4"})
    SKIP_TAGS = frozenset({"script", "style", "svg", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, _: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized in self.SKIP_TAGS:
            self.skip_depth += 1
        elif normalized in self.BLOCK_TAGS and self.skip_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif normalized in self.BLOCK_TAGS and self.skip_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0:
            self.parts.append(data)


def clean_feed_html(value: str, *, limit: int = FEED_BODY_MAX_CHARS) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = value
    lines = [re.sub(r"[\t\r\f\v ]+", " ", line).strip() for line in text.splitlines()]
    compact: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line
        if blank and (previous_blank or not compact):
            continue
        compact.append(line)
        previous_blank = blank
    return "\n".join(compact).strip()[:limit]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1].casefold()


def _first_text(element: Any, names: tuple[str, ...]) -> str:
    wanted = frozenset(name.casefold() for name in names)
    for child in element.iter():
        if child is element or _local_name(str(child.tag)) not in wanted:
            continue
        value = "".join(child.itertext()).strip()
        if value:
            return value
    return ""


def _entry_link(element: Any) -> str | None:
    for child in element.iter():
        if _local_name(str(child.tag)) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        rel = str(child.attrib.get("rel") or "alternate").casefold()
        candidate = href if href and rel in {"alternate", ""} else "".join(child.itertext()).strip()
        safe = _safe_article_url(candidate)
        if safe:
            return safe
    return None


def _parse_date(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return to_iso(parsed)


def _external_id(element: Any, *, url: str | None, title: str, body: str, published: str | None) -> str:
    declared = _first_text(element, ("guid", "id"))
    if declared:
        candidate = f"id:{declared}"
    elif url:
        candidate = f"url:{url}"
    else:
        digest = hashlib.sha256(
            f"{title}\n{published or ''}\n{body}".encode("utf-8")
        ).hexdigest()
        candidate = f"hash:{digest}"
    return f"sha256:{hashlib.sha256(candidate.encode('utf-8')).hexdigest()}"


def parse_feed(payload: bytes) -> ParsedFeed:
    if not payload or len(payload) > FEED_RESPONSE_MAX_BYTES:
        raise FeedError("response_too_large")
    payload = MALFORMED_TRAILING_CACHE_COMMENT_RE.sub(b"", payload, count=1)
    try:
        root = ElementTree.fromstring(
            payload,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (ElementTree.ParseError, DefusedXmlException, ValueError) as exc:
        raise FeedError("invalid_feed") from exc
    root_name = _local_name(str(root.tag))
    if root_name not in {"rss", "rdf", "feed"}:
        raise FeedError("invalid_feed")
    entry_name = "entry" if root_name == "feed" else "item"
    elements = [item for item in root.iter() if _local_name(str(item.tag)) == entry_name]
    if not elements:
        raise FeedError("invalid_feed")
    entries: list[FeedEntry] = []
    seen: set[str] = set()
    for element in elements[:FEED_ENTRY_LIMIT]:
        title = clean_feed_html(_first_text(element, ("title",)), limit=FEED_TITLE_MAX_CHARS)
        body = clean_feed_html(
            _first_text(element, ("encoded", "content", "summary", "description")),
            limit=FEED_BODY_MAX_CHARS,
        )
        url = _entry_link(element)
        published = _parse_date(_first_text(element, ("published", "pubdate", "updated", "date")))
        external_id = _external_id(
            element,
            url=url,
            title=title,
            body=body,
            published=published,
        )
        if external_id in seen or not (title or body):
            continue
        seen.add(external_id)
        entries.append(
            FeedEntry(
                external_id=external_id,
                title=title,
                body=body,
                url=url,
                published_at=published,
            )
        )
    if not entries:
        raise FeedError("invalid_feed")
    return ParsedFeed(tuple(entries))


class RSSFetcher:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], Awaitable[frozenset[str]]] = resolve_public_feed_host,
    ) -> None:
        self._resolver = resolver
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=FEED_REQUEST_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={
                "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9",
                "User-Agent": "TelegramPriorityFeedReader/1.0",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, source: dict[str, Any]) -> FeedFetchResult:
        url = validate_feed_url(str(source["url"]))
        resolved = await self._resolver(url)
        headers: dict[str, str] = {}
        if source.get("etag"):
            headers["If-None-Match"] = str(source["etag"])
        if source.get("last_modified"):
            headers["If-Modified-Since"] = str(source["last_modified"])
        try:
            async with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code == 304:
                    return FeedFetchResult(True, None, source.get("etag"), source.get("last_modified"), 304)
                if 300 <= response.status_code < 400:
                    raise FeedError("redirect_blocked")
                if response.status_code == 429:
                    raise FeedError("rate_limited")
                if 400 <= response.status_code < 500:
                    raise FeedError("http_4xx")
                if response.status_code >= 500:
                    raise FeedError("http_5xx")
                try:
                    declared_length = int(response.headers.get("content-length", "0"))
                except ValueError:
                    declared_length = 0
                if declared_length > FEED_RESPONSE_MAX_BYTES:
                    raise FeedError("response_too_large")
                stream = response.extensions.get("network_stream")
                if stream is not None:
                    peer = stream.get_extra_info("server_addr")
                    if peer and str(peer[0]).split("%", 1)[0] not in resolved:
                        raise FeedError("unsafe_address")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > FEED_RESPONSE_MAX_BYTES:
                        raise FeedError("response_too_large")
                feed = parse_feed(bytes(content))
                return FeedFetchResult(
                    False,
                    feed,
                    response.headers.get("etag")[:512] if response.headers.get("etag") else None,
                    response.headers.get("last-modified")[:512] if response.headers.get("last-modified") else None,
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


class RSSPoller:
    def __init__(
        self,
        database_path: str,
        analysis_queue: Any,
        *,
        fetcher: RSSFetcher | None = None,
        worker_count: int = FEED_POLL_WORKERS,
    ) -> None:
        self._database_path = database_path
        self._analysis_queue = analysis_queue
        self._fetcher = fetcher or RSSFetcher()
        self._worker_count = max(1, worker_count)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> int:
        recovered = await asyncio.to_thread(
            _database_call,
            self._database_path,
            "recover_information_source_polls",
            now=utc_now(),
        )
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"rss-poller-{index}")
            for index in range(self._worker_count)
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
            _database_call,
            self._database_path,
            "release_information_source_polls",
            now=utc_now(),
        )
        await self._fetcher.close()

    async def _worker(self, _: int) -> None:
        while not self._stop.is_set():
            source = await asyncio.to_thread(
                _database_call,
                self._database_path,
                "claim_due_information_source",
                now=utc_now(),
                lease_seconds=FEED_POLL_LEASE_SECONDS,
            )
            if source is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                except TimeoutError:
                    pass
                continue
            await self._poll(source)
            await asyncio.sleep(0)

    async def _poll(self, source: dict[str, Any]) -> None:
        source_id = int(source["id"])
        generation = int(source["generation"])
        now = utc_now()
        try:
            result = await self._fetcher.fetch(source)
            if result.not_modified:
                await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "complete_information_source_poll",
                    source_id,
                    generation=generation,
                    now=utc_now(),
                    http_status=result.http_status,
                    etag=result.etag,
                    last_modified=result.last_modified,
                    new_items=0,
                    cursor_value=source.get("cursor_value"),
                    last_item_at=None,
                )
                return
            assert result.feed is not None
            entries = result.feed.entries
            if not bool(source.get("initialized")):
                await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "baseline_information_source",
                    source_id,
                    generation=generation,
                    entries=entries,
                    now=utc_now(),
                    http_status=result.http_status,
                    etag=result.etag,
                    last_modified=result.last_modified,
                )
                return

            def ordering(entry: FeedEntry) -> tuple[str, str]:
                return entry.published_at or to_iso(now), entry.external_id

            new_items = 0
            last_item_at: str | None = None
            cursor_value: str | None = source.get("cursor_value")
            for entry in sorted(entries, key=ordering):
                reserved = await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "reserve_information_source_item",
                    source_id,
                    generation=generation,
                    external_id=entry.external_id,
                    title=entry.title,
                    url=entry.url,
                    published_at=entry.published_at,
                    now=utc_now(),
                )
                if reserved is None or str(reserved.get("state")) != "pending":
                    continue
                observed_at = utc_now()
                runtime = await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "get_runtime_config",
                )
                keywords = tuple(runtime.get("important_keywords") or ()) if runtime else ()
                normalized = normalize_text(entry.text)
                repeated = await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "count_recent_normalized",
                    information_source_chat_id(source_id),
                    normalized,
                    observed_at - timedelta(minutes=15),
                )
                scoring = score_message(
                    entry.text,
                    mentioned_me=False,
                    reply_to_me=False,
                    trusted_sender=False,
                    keywords=keywords,
                    repeated_recently=bool(repeated),
                )
                item_id = int(reserved["id"])
                sent_at = entry.published_at or to_iso(observed_at)
                record = MessageRecord(
                    chat_id=information_source_chat_id(source_id),
                    message_id=item_id,
                    chat_name=str(source["name"]),
                    chat_username=None,
                    sender_id=None,
                    sender_name="RSS/Atom",
                    sent_at=sent_at,
                    text=entry.text,
                    reply_to_message_id=None,
                    thread_root_id=item_id,
                    base_score=scoring.score,
                    reasons=scoring.reasons,
                    link=entry.url,
                    normalized_text=scoring.normalized_text,
                    primary_url=entry.url or scoring.primary_url,
                    created_at=to_iso(observed_at),
                    source_type="rss",
                    source_id=source_id,
                    source_external_id=entry.external_id,
                )
                inserted = await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "insert_message",
                    record,
                    enqueue_analysis=True,
                    now=observed_at,
                )
                linked = await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "link_information_source_item",
                    item_id,
                    chat_id=record.chat_id,
                    message_id=record.message_id,
                    now=observed_at,
                )
                if inserted:
                    new_items += 1
                    self._analysis_queue.wake()
                if linked:
                    cursor_value = entry.external_id
                    last_item_at = entry.published_at or to_iso(observed_at)
            await asyncio.to_thread(
                _database_call,
                self._database_path,
                "complete_information_source_poll",
                source_id,
                generation=generation,
                now=utc_now(),
                http_status=result.http_status,
                etag=result.etag,
                last_modified=result.last_modified,
                new_items=new_items,
                cursor_value=cursor_value,
                last_item_at=last_item_at,
            )
        except asyncio.CancelledError:
            raise
        except FeedError as exc:
            await asyncio.to_thread(
                _database_call,
                self._database_path,
                "fail_information_source_poll",
                source_id,
                generation=generation,
                now=utc_now(),
                error_category=exc.category,
            )
            LOGGER.warning("RSS/Atom 抓取失败：source_id=%s category=%s", source_id, exc.category)
        except Exception as exc:
            await asyncio.to_thread(
                _database_call,
                self._database_path,
                "fail_information_source_poll",
                source_id,
                generation=generation,
                now=utc_now(),
                error_category="internal_error",
            )
            LOGGER.error("RSS/Atom 处理异常：source_id=%s error=%s", source_id, type(exc).__name__)
