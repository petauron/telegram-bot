from __future__ import annotations

import json
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.feeds import (
    FeedEntry,
    FeedError,
    FeedFetchResult,
    ParsedFeed,
    clean_feed_html,
    resolve_public_feed_host,
    validate_feed_url,
)
from app.github_releases import GitHubSourcePoller


STATUS_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
STATUS_INCIDENT_LIMIT = 100
STATUS_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=15.0, write=5.0, pool=5.0)


def validate_status_page_url(value: str) -> str:
    normalized = validate_feed_url(value)
    parsed = urlsplit(normalized)
    if parsed.query:
        raise ValueError("状态页地址不能包含查询参数")
    path = parsed.path.rstrip("/")
    if path.endswith("/api/v2") or "/api/v2/" in path:
        raise ValueError("请填写状态页首页，而不是 API 地址")
    return urlunsplit(("https", parsed.netloc, path or "/", "", ""))


def status_incidents_api_url(value: str) -> str:
    base = validate_status_page_url(value)
    parsed = urlsplit(base)
    prefix = parsed.path.rstrip("/")
    return urlunsplit(
        ("https", parsed.netloc, f"{prefix}/api/v2/incidents.json", "", "")
    )


def _safe_incident_url(value: object, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return fallback
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        return fallback
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))[:2048]


def parse_status_incidents(payload: bytes, *, source_url: str) -> ParsedFeed:
    if not payload or len(payload) > STATUS_RESPONSE_MAX_BYTES:
        raise FeedError("response_too_large")
    try:
        document = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise FeedError("invalid_response") from exc
    rows = document.get("incidents") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise FeedError("invalid_response")
    entries: list[FeedEntry] = []
    for row in rows[:STATUS_INCIDENT_LIMIT]:
        if not isinstance(row, dict):
            continue
        incident_id = str(row.get("id") or "").strip()[:160]
        updated_at = str(row.get("updated_at") or "").strip()
        name = clean_feed_html(str(row.get("name") or ""), limit=280)
        if not incident_id or not updated_at or not name:
            continue
        status = str(row.get("status") or "").strip().casefold()
        impact = str(row.get("impact") or "").strip().casefold()
        updates = row.get("incident_updates")
        update_lines: list[str] = []
        if isinstance(updates, list):
            for update in updates[:8]:
                if not isinstance(update, dict):
                    continue
                body = clean_feed_html(str(update.get("body") or ""), limit=2_000)
                update_status = clean_feed_html(str(update.get("status") or ""), limit=40)
                update_time = str(update.get("created_at") or "").strip()
                if body:
                    prefix = " · ".join(part for part in (update_time, update_status) if part)
                    update_lines.append(f"{prefix}\n{body}" if prefix else body)
        status_label = {
            "investigating": "调查中",
            "identified": "已定位",
            "monitoring": "监控中",
            "resolved": "已恢复",
            "postmortem": "事后报告",
        }.get(status, status or "状态更新")
        impact_label = {
            "critical": "严重",
            "major": "重大",
            "minor": "轻微",
            "none": "无影响",
        }.get(impact, impact or "未标注")
        body_parts = [
            f"状态：{status_label} · 影响：{impact_label}",
            *update_lines,
        ]
        entries.append(
            FeedEntry(
                external_id=f"statuspage:{incident_id}:{updated_at}",
                title=f"{name} · {status_label}"[:300],
                body="\n\n".join(part for part in body_parts if part)[:12_000],
                url=_safe_incident_url(row.get("shortlink"), source_url),
                published_at=updated_at,
            )
        )
    return ParsedFeed(tuple(entries))


class VendorStatusFetcher:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], Awaitable[frozenset[str]]] = resolve_public_feed_host,
    ) -> None:
        self._resolver = resolver
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=STATUS_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={
                "Accept": "application/json",
                "User-Agent": "TelegramPriorityStatusPageReader/1.0",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, source: dict[str, Any], *, token: str = "") -> FeedFetchResult:
        del token
        source_url = validate_status_page_url(str(source["url"]))
        api_url = status_incidents_api_url(source_url)
        resolved = await self._resolver(api_url)
        headers: dict[str, str] = {}
        if source.get("etag"):
            headers["If-None-Match"] = str(source["etag"])
        if source.get("last_modified"):
            headers["If-Modified-Since"] = str(source["last_modified"])
        try:
            async with self._client.stream("GET", api_url, headers=headers) as response:
                if response.status_code == 304:
                    return FeedFetchResult(
                        True, None, source.get("etag"), source.get("last_modified"), 304
                    )
                if 300 <= response.status_code < 400:
                    raise FeedError("redirect_blocked")
                if response.status_code == 429:
                    raise FeedError("rate_limited")
                if 400 <= response.status_code < 500:
                    raise FeedError("http_4xx")
                if response.status_code >= 500:
                    raise FeedError("http_5xx")
                stream = response.extensions.get("network_stream")
                if stream is not None:
                    peer = stream.get_extra_info("server_addr")
                    if peer and str(peer[0]).split("%", 1)[0] not in resolved:
                        raise FeedError("unsafe_address")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > STATUS_RESPONSE_MAX_BYTES:
                        raise FeedError("response_too_large")
                return FeedFetchResult(
                    False,
                    parse_status_incidents(bytes(content), source_url=source_url),
                    response.headers.get("etag")[:512] if response.headers.get("etag") else None,
                    response.headers.get("last-modified")[:512]
                    if response.headers.get("last-modified")
                    else None,
                    response.status_code,
                )
        except FeedError:
            raise
        except httpx.TimeoutException as exc:
            raise FeedError("timeout") from exc
        except httpx.HTTPError as exc:
            raise FeedError("network_error") from exc


class VendorStatusPoller(GitHubSourcePoller):
    SOURCE_KIND = "vendor_status"
    SENDER_NAME = "厂商状态页"
    LOG_LABEL = "厂商状态页"
    FETCHER_TYPE = VendorStatusFetcher
    CREDENTIAL_PROVIDER = None
