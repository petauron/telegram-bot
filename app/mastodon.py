from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit, urlunsplit

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


MASTODON_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
MASTODON_STATUS_LIMIT = 40
MASTODON_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=15.0, write=5.0, pool=5.0)
MASTODON_ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
MASTODON_TAG_PATTERN = re.compile(r"^[\w-]{1,64}$", re.UNICODE)


def validate_mastodon_instance(value: str) -> str:
    normalized = validate_feed_url(value)
    parsed = urlsplit(normalized)
    if parsed.path not in {"", "/"} or parsed.query:
        raise ValueError("Mastodon 实例地址必须是 HTTPS 根地址")
    return urlunsplit(("https", parsed.netloc, "", "", ""))


def validate_mastodon_settings(settings: dict[str, Any] | None) -> dict[str, str]:
    raw = settings or {}
    instance_url = validate_mastodon_instance(str(raw.get("instance_url") or ""))
    timeline_type = str(raw.get("timeline_type") or "account").strip().casefold()
    if timeline_type not in {"account", "tag"}:
        raise ValueError("Mastodon 来源必须是可信账号或精确标签")
    target = str(raw.get("target") or "").strip().removeprefix("@")
    if timeline_type == "account":
        parts = target.split("@")
        account = parts[0]
        if not MASTODON_ACCOUNT_PATTERN.fullmatch(account):
            raise ValueError("Mastodon 账号格式无效")
        if len(parts) > 2:
            raise ValueError("Mastodon 账号格式无效")
        if len(parts) == 2 and parts[1].casefold() != urlsplit(instance_url).hostname:
            raise ValueError("Mastodon 账号必须属于所选实例")
        target = account
    elif not MASTODON_TAG_PATTERN.fullmatch(target):
        raise ValueError("Mastodon 标签格式无效")
    return {
        "instance_url": instance_url,
        "timeline_type": timeline_type,
        "target": target,
    }


def mastodon_source_url(settings: dict[str, Any] | None) -> str:
    config = validate_mastodon_settings(settings)
    suffix = (
        f"/@{quote(config['target'], safe='')}"
        if config["timeline_type"] == "account"
        else f"/tags/{quote(config['target'], safe='')}"
    )
    return f"{config['instance_url']}{suffix}"


def _safe_status_url(value: object, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048 or any(ord(character) < 32 for character in raw):
        return fallback
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return fallback
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 80, 443}
    ):
        return fallback
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def parse_mastodon_statuses(rows: object, *, fallback_url: str) -> ParsedFeed:
    if not isinstance(rows, list):
        raise FeedError("invalid_response")
    entries: list[FeedEntry] = []
    for wrapper in rows[:MASTODON_STATUS_LIMIT]:
        if not isinstance(wrapper, dict) or wrapper.get("in_reply_to_id") is not None:
            continue
        wrapper_id = str(wrapper.get("id") or "").strip()
        status = wrapper.get("reblog") if isinstance(wrapper.get("reblog"), dict) else wrapper
        if not wrapper_id or not isinstance(status, dict):
            continue
        content = clean_feed_html(str(status.get("content") or ""), limit=10_000)
        spoiler = clean_feed_html(str(status.get("spoiler_text") or ""), limit=500)
        if not content:
            continue
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "Mastodon 动态")
        body_parts = [f"内容提示：{spoiler}" if spoiler else "", content]
        if status is not wrapper:
            body_parts.insert(0, "可信账号转发")
        published = str(wrapper.get("created_at") or status.get("created_at") or "").strip() or None
        entries.append(
            FeedEntry(
                external_id=f"mastodon-status:{wrapper_id}",
                title=first_line[:300],
                body="\n\n".join(part for part in body_parts if part)[:12_000],
                url=_safe_status_url(wrapper.get("url") or status.get("url"), fallback_url),
                published_at=published,
            )
        )
    return ParsedFeed(tuple(entries))


class MastodonFetcher:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], Awaitable[frozenset[str]]] = resolve_public_feed_host,
    ) -> None:
        self._resolver = resolver
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=MASTODON_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={"Accept": "application/json", "User-Agent": "TelegramPriorityMastodonReader/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_json(
        self, url: str, *, params: dict[str, str], headers: dict[str, str], resolved: frozenset[str]
    ) -> object:
        try:
            response = await self._client.get(url, params=params, headers=headers)
            if 300 <= response.status_code < 400:
                raise FeedError("redirect_blocked")
            if response.status_code == 429:
                raise FeedError("rate_limited")
            if 400 <= response.status_code < 500:
                raise FeedError("http_4xx")
            if response.status_code >= 500:
                raise FeedError("http_5xx")
            if len(response.content) > MASTODON_RESPONSE_MAX_BYTES:
                raise FeedError("response_too_large")
            stream = response.extensions.get("network_stream")
            if stream is not None:
                peer = stream.get_extra_info("server_addr")
                if peer and str(peer[0]).split("%", 1)[0] not in resolved:
                    raise FeedError("unsafe_address")
            return response.json()
        except FeedError:
            raise
        except httpx.TimeoutException as exc:
            raise FeedError("timeout") from exc
        except httpx.HTTPError as exc:
            raise FeedError("network_error") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise FeedError("invalid_response") from exc

    async def fetch(self, source: dict[str, Any], *, token: str = "") -> FeedFetchResult:
        config = validate_mastodon_settings(source.get("settings"))
        instance = config["instance_url"]
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resolved = await self._resolver(f"{instance}/api/v1/instance")
        params = {"limit": str(MASTODON_STATUS_LIMIT)}
        cursor = str(source.get("cursor_value") or "")
        if cursor.isdigit():
            params["since_id"] = cursor
        if config["timeline_type"] == "account":
            account = await self._get_json(
                f"{instance}/api/v1/accounts/lookup",
                params={"acct": config["target"]},
                headers=headers,
                resolved=resolved,
            )
            account_id = str(account.get("id") or "") if isinstance(account, dict) else ""
            if not account_id.isdigit():
                raise FeedError("invalid_response")
            params.update({"exclude_replies": "true", "exclude_reblogs": "false"})
            endpoint = f"{instance}/api/v1/accounts/{account_id}/statuses"
        else:
            endpoint = f"{instance}/api/v1/timelines/tag/{quote(config['target'], safe='')}"
        rows = await self._get_json(endpoint, params=params, headers=headers, resolved=resolved)
        feed = parse_mastodon_statuses(rows, fallback_url=mastodon_source_url(config))
        newest = max(
            (
                entry.external_id.rsplit(":", 1)[-1]
                for entry in feed.entries
                if entry.external_id.rsplit(":", 1)[-1].isdigit()
            ),
            key=int,
            default=cursor or None,
        )
        return FeedFetchResult(False, feed, None, None, 200, newest)


class MastodonPoller(GitHubSourcePoller):
    SOURCE_KIND = "mastodon"
    SENDER_NAME = "Mastodon"
    LOG_LABEL = "Mastodon"
    FETCHER_TYPE = MastodonFetcher
    CREDENTIAL_PROVIDER = None
    SOURCE_CREDENTIAL = True
