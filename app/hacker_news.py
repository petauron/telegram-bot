from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.feeds import FeedEntry, FeedError, FeedFetchResult, ParsedFeed, clean_feed_html
from app.github_releases import GitHubSourcePoller


HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
HN_WEB_URL = "https://news.ycombinator.com/"
HN_RESPONSE_MAX_BYTES = 512 * 1024
HN_ID_SCAN_LIMIT = 60
HN_ITEM_CONCURRENCY = 8
HN_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=15.0, write=5.0, pool=5.0)
HN_DEFAULT_KEYWORDS = (
    "ai", "artificial intelligence", "llm", "machine learning", "developer",
    "programming", "open source", "github", "linux", "cloud", "security",
    "vulnerability", "network", "database", "kubernetes", "docker",
)


def _matches_keyword(text: str, keyword: str) -> bool:
    if len(keyword) <= 3 and keyword.isascii() and keyword.isalnum():
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
    return keyword in text


def validate_hacker_news_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    raw = settings or {}
    story_list = str(raw.get("story_list") or "best").strip().casefold()
    if story_list not in {"top", "best", "both"}:
        raise ValueError("Hacker News 榜单必须是 top、best 或 both")
    try:
        minimum_score = int(raw.get("minimum_score", 150))
    except (TypeError, ValueError) as exc:
        raise ValueError("Hacker News 最低热度必须是整数") from exc
    if not 20 <= minimum_score <= 5_000:
        raise ValueError("Hacker News 最低热度必须在 20 到 5000 之间")
    keywords = tuple(
        dict.fromkeys(
            str(item).strip().casefold()[:80]
            for item in (raw.get("keywords") or ())
            if str(item).strip()
        )
    )
    if len(keywords) > 40:
        raise ValueError("Hacker News 主题关键词最多 40 个")
    return {
        "story_list": story_list,
        "minimum_score": minimum_score,
        "keywords": list(keywords),
    }


def _safe_story_url(value: object, story_id: int) -> str:
    fallback = f"https://news.ycombinator.com/item?id={story_id}"
    raw = str(value or "").strip()
    if not raw or any(ord(character) < 32 for character in raw):
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
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))[:2048]


def parse_hacker_news_stories(
    rows: list[object], *, settings: dict[str, Any] | None
) -> ParsedFeed:
    config = validate_hacker_news_settings(settings)
    keywords = tuple(config["keywords"]) or HN_DEFAULT_KEYWORDS
    entries: list[FeedEntry] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "story" or row.get("deleted") or row.get("dead"):
            continue
        story_id = row.get("id")
        title = clean_feed_html(str(row.get("title") or ""), limit=300)
        try:
            score = int(row.get("score") or 0)
            comments = int(row.get("descendants") or 0)
            published = datetime.fromtimestamp(int(row.get("time") or 0), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            continue
        if not isinstance(story_id, int) or not title or score < int(config["minimum_score"]):
            continue
        text = clean_feed_html(str(row.get("text") or ""), limit=8_000)
        searchable = f"{title}\n{text}".casefold()
        if not any(_matches_keyword(searchable, keyword) for keyword in keywords):
            continue
        body = "\n\n".join(
            part for part in (text, f"Hacker News 热度：{score} 分 · {comments} 条讨论") if part
        )
        entries.append(
            FeedEntry(
                external_id=f"hn-story:{story_id}",
                title=title,
                body=body[:12_000],
                url=_safe_story_url(row.get("url"), story_id),
                published_at=published,
            )
        )
    return ParsedFeed(tuple(entries))


class HackerNewsFetcher:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=HN_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=HN_ITEM_CONCURRENCY, max_keepalive_connections=4),
            headers={"Accept": "application/json", "User-Agent": "TelegramPriorityHNReader/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _json(self, url: str) -> object:
        try:
            response = await self._client.get(url)
            if 300 <= response.status_code < 400:
                raise FeedError("redirect_blocked")
            if response.status_code == 429:
                raise FeedError("rate_limited")
            if 400 <= response.status_code < 500:
                raise FeedError("http_4xx")
            if response.status_code >= 500:
                raise FeedError("http_5xx")
            if len(response.content) > HN_RESPONSE_MAX_BYTES:
                raise FeedError("response_too_large")
            return response.json()
        except FeedError:
            raise
        except (json.JSONDecodeError, ValueError) as exc:
            raise FeedError("invalid_response") from exc
        except httpx.TimeoutException as exc:
            raise FeedError("timeout") from exc
        except httpx.HTTPError as exc:
            raise FeedError("network_error") from exc

    async def fetch(self, source: dict[str, Any], *, token: str = "") -> FeedFetchResult:
        del token
        settings = validate_hacker_news_settings(source.get("settings"))
        endpoints = (
            ("topstories", "beststories")
            if settings["story_list"] == "both"
            else (f"{settings['story_list']}stories",)
        )
        id_lists = await asyncio.gather(
            *(self._json(f"{HN_API_BASE}/{endpoint}.json") for endpoint in endpoints)
        )
        ids: list[int] = []
        for values in id_lists:
            if not isinstance(values, list):
                raise FeedError("invalid_response")
            for value in values:
                if isinstance(value, int) and value not in ids:
                    ids.append(value)
                if len(ids) >= HN_ID_SCAN_LIMIT:
                    break
            if len(ids) >= HN_ID_SCAN_LIMIT:
                break
        semaphore = asyncio.Semaphore(HN_ITEM_CONCURRENCY)

        async def fetch_item(story_id: int) -> object:
            async with semaphore:
                return await self._json(f"{HN_API_BASE}/item/{story_id}.json")

        rows = await asyncio.gather(*(fetch_item(story_id) for story_id in ids))
        return FeedFetchResult(
            False,
            parse_hacker_news_stories(rows, settings=settings),
            None,
            None,
            200,
        )


class HackerNewsPoller(GitHubSourcePoller):
    SOURCE_KIND = "hacker_news"
    SENDER_NAME = "Hacker News"
    LOG_LABEL = "Hacker News"
    FETCHER_TYPE = HackerNewsFetcher
    CREDENTIAL_PROVIDER = None
