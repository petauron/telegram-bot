from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import WebSocketException

from app.database import utc_now
from app.feeds import FeedEntry, FeedError, FeedFetchResult, ParsedFeed, clean_feed_html
from app.github_releases import GitHubSourcePoller


BLUESKY_RESOLVE_URL = "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle"
BLUESKY_JETSTREAM_URL = "wss://jetstream2.us-east.bsky.network/subscribe"
BLUESKY_COLLECTION = "app.bsky.feed.post"
BLUESKY_EVENT_LIMIT = 100
BLUESKY_EVENT_MAX_BYTES = 256 * 1024
BLUESKY_IDLE_SECONDS = 2.0
BLUESKY_TIMEOUT = httpx.Timeout(15.0, connect=5.0, read=10.0, write=5.0, pool=5.0)
BLUESKY_HANDLE_PATTERN = re.compile(
    r"^(?=.{3,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$"
)
BLUESKY_DID_PATTERN = re.compile(r"^did:(?:plc|web):[A-Za-z0-9._:%-]{3,240}$")


def validate_bluesky_handle(value: str) -> str:
    handle = value.strip().removeprefix("@").casefold()
    if not BLUESKY_HANDLE_PATTERN.fullmatch(handle):
        raise ValueError("Bluesky 账号必须是有效的公开 handle")
    return handle


def bluesky_profile_url(handle: str) -> str:
    return f"https://bsky.app/profile/{validate_bluesky_handle(handle)}"


def _safe_external_url(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048 or any(ord(character) < 32 for character in raw):
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
        or parsed.fragment
        or port not in {None, 80, 443}
    ):
        return None
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def parse_jetstream_event(
    payload: object, *, expected_did: str, handle: str
) -> tuple[FeedEntry | None, int | None]:
    if not isinstance(payload, dict):
        return None, None
    time_us = payload.get("time_us")
    cursor = int(time_us) if isinstance(time_us, int) and time_us > 0 else None
    if payload.get("kind") != "commit" or payload.get("did") != expected_did:
        return None, cursor
    commit = payload.get("commit")
    if not isinstance(commit, dict) or commit.get("operation") != "create":
        return None, cursor
    if commit.get("collection") != BLUESKY_COLLECTION:
        return None, cursor
    rkey = str(commit.get("rkey") or "").strip()
    record = commit.get("record")
    if not rkey or len(rkey) > 160 or not isinstance(record, dict):
        return None, cursor
    text = clean_feed_html(str(record.get("text") or ""), limit=10_000)
    if not text:
        return None, cursor
    created_at = str(record.get("createdAt") or "").strip() or None
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "Bluesky 动态")
    body_parts = [text]
    embed = record.get("embed")
    if isinstance(embed, dict):
        external = embed.get("external")
        if isinstance(external, dict):
            external_title = clean_feed_html(str(external.get("title") or ""), limit=300)
            external_description = clean_feed_html(
                str(external.get("description") or ""), limit=2_000
            )
            external_url = _safe_external_url(external.get("uri"))
            if external_title:
                body_parts.append(external_title)
            if external_description:
                body_parts.append(external_description)
            if external_url:
                body_parts.append(external_url)
    return (
        FeedEntry(
            external_id=f"bluesky-post:{rkey}",
            title=first_line[:300],
            body="\n\n".join(body_parts)[:12_000],
            url=f"https://bsky.app/profile/{handle}/post/{rkey}"[:2048],
            published_at=created_at,
        ),
        cursor,
    )


class BlueskyJetstreamFetcher:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connector: Callable[..., Any] = websocket_connect,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        self._connector = connector
        self._now = now_provider
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=BLUESKY_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            headers={"Accept": "application/json", "User-Agent": "TelegramPriorityBlueskyReader/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _resolve_did(self, handle: str) -> str:
        try:
            response = await self._client.get(BLUESKY_RESOLVE_URL, params={"handle": handle})
            if response.status_code == 429:
                raise FeedError("rate_limited")
            if 300 <= response.status_code < 400:
                raise FeedError("redirect_blocked")
            if 400 <= response.status_code < 500:
                raise FeedError("http_4xx")
            if response.status_code >= 500:
                raise FeedError("http_5xx")
            if len(response.content) > 64 * 1024:
                raise FeedError("response_too_large")
            document = response.json()
        except FeedError:
            raise
        except httpx.TimeoutException as exc:
            raise FeedError("timeout") from exc
        except httpx.HTTPError as exc:
            raise FeedError("network_error") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise FeedError("invalid_response") from exc
        did = str(document.get("did") or "") if isinstance(document, dict) else ""
        if not BLUESKY_DID_PATTERN.fullmatch(did):
            raise FeedError("invalid_response")
        return did

    async def fetch(self, source: dict[str, Any], *, token: str = "") -> FeedFetchResult:
        del token
        settings = source.get("settings") or {}
        handle = validate_bluesky_handle(str(settings.get("handle") or ""))
        if not bool(source.get("initialized")):
            cursor = str(int(self._now().timestamp() * 1_000_000))
            return FeedFetchResult(False, ParsedFeed(()), None, None, 101, cursor)
        cursor_raw = str(source.get("cursor_value") or "").strip()
        if not cursor_raw.isdigit():
            cursor_raw = str(int(self._now().timestamp() * 1_000_000))
        did = await self._resolve_did(handle)
        query = urlencode(
            {
                "wantedCollections": BLUESKY_COLLECTION,
                "wantedDids": did,
                "cursor": cursor_raw,
            }
        )
        url = f"{BLUESKY_JETSTREAM_URL}?{query}"
        entries: list[FeedEntry] = []
        newest_cursor = int(cursor_raw)
        try:
            async with self._connector(
                url,
                open_timeout=5,
                close_timeout=3,
                max_size=BLUESKY_EVENT_MAX_BYTES,
                ping_interval=20,
                ping_timeout=10,
            ) as websocket:
                for _ in range(BLUESKY_EVENT_LIMIT):
                    try:
                        message = await asyncio.wait_for(
                            websocket.recv(), timeout=BLUESKY_IDLE_SECONDS
                        )
                    except TimeoutError:
                        break
                    if not isinstance(message, (str, bytes)) or len(message) > BLUESKY_EVENT_MAX_BYTES:
                        raise FeedError("response_too_large")
                    try:
                        document = json.loads(message)
                    except (TypeError, ValueError) as exc:
                        raise FeedError("invalid_response") from exc
                    entry, event_cursor = parse_jetstream_event(
                        document, expected_did=did, handle=handle
                    )
                    if event_cursor is not None:
                        newest_cursor = max(newest_cursor, event_cursor)
                    if entry is not None:
                        entries.append(entry)
        except FeedError:
            raise
        except TimeoutError as exc:
            raise FeedError("timeout") from exc
        except (OSError, WebSocketException) as exc:
            raise FeedError("network_error") from exc
        return FeedFetchResult(
            False, ParsedFeed(tuple(entries)), None, None, 101, str(newest_cursor)
        )


class BlueskyJetstreamPoller(GitHubSourcePoller):
    SOURCE_KIND = "bluesky"
    SENDER_NAME = "Bluesky"
    LOG_LABEL = "Bluesky Jetstream"
    FETCHER_TYPE = BlueskyJetstreamFetcher
    CREDENTIAL_PROVIDER = None
