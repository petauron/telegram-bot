from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote_plus

import httpx

from app.feeds import FeedEntry, FeedError, FeedFetchResult, ParsedFeed, clean_feed_html
from app.github_releases import GitHubSourcePoller


GITHUB_ADVISORY_RESPONSE_MAX_BYTES = 3 * 1024 * 1024
GITHUB_ADVISORY_LIMIT = 100
GITHUB_ADVISORY_ECOSYSTEMS = frozenset(
    {"", "actions", "composer", "erlang", "go", "maven", "npm", "nuget", "pip", "pub", "rubygems", "rust", "swift"}
)
GITHUB_ADVISORY_SEVERITIES = frozenset({"high", "critical"})
GITHUB_ADVISORY_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=15.0, write=5.0, pool=5.0)


def validate_advisory_settings(settings: dict[str, Any]) -> dict[str, Any]:
    ecosystem = str(settings.get("ecosystem") or "").strip().casefold()
    severity = str(settings.get("minimum_severity") or "high").strip().casefold()
    if ecosystem not in GITHUB_ADVISORY_ECOSYSTEMS:
        raise ValueError("不支持的 GitHub Advisory ecosystem")
    if severity not in GITHUB_ADVISORY_SEVERITIES:
        raise ValueError("最低严重度只允许 high 或 critical")
    keywords = tuple(
        dict.fromkeys(
            str(value).strip()[:80]
            for value in settings.get("keywords") or ()
            if str(value).strip()
        )
    )[:30]
    return {
        "ecosystem": ecosystem,
        "minimum_severity": severity,
        "keywords": list(keywords),
    }


def github_advisories_web_url(settings: dict[str, Any]) -> str:
    clean = validate_advisory_settings(settings)
    terms = ["type:reviewed", f"severity:{clean['minimum_severity']}"]
    if clean["ecosystem"]:
        terms.append(f"ecosystem:{clean['ecosystem']}")
    terms.extend(clean["keywords"])
    return f"https://github.com/advisories?query={quote_plus(' '.join(terms))}"


def _matches_filters(row: dict[str, Any], settings: dict[str, Any]) -> bool:
    clean = validate_advisory_settings(settings)
    severity = str(row.get("severity") or "").casefold()
    if clean["minimum_severity"] == "critical":
        if severity != "critical":
            return False
    elif severity not in {"high", "critical"}:
        return False
    vulnerabilities = row.get("vulnerabilities") or []
    if clean["ecosystem"]:
        if not any(
            str((item.get("package") or {}).get("ecosystem") or "").casefold()
            == clean["ecosystem"]
            for item in vulnerabilities
            if isinstance(item, dict)
        ):
            return False
    if clean["keywords"]:
        haystack = "\n".join(
            [
                str(row.get("summary") or ""),
                str(row.get("description") or ""),
                " ".join(
                    str((item.get("package") or {}).get("name") or "")
                    for item in vulnerabilities
                    if isinstance(item, dict)
                ),
            ]
        ).casefold()
        if not any(keyword.casefold() in haystack for keyword in clean["keywords"]):
            return False
    return True


def parse_github_advisories(payload: bytes, *, settings: dict[str, Any]) -> ParsedFeed:
    if not payload or len(payload) > GITHUB_ADVISORY_RESPONSE_MAX_BYTES:
        raise FeedError("response_too_large")
    try:
        rows = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise FeedError("invalid_response") from exc
    if not isinstance(rows, list):
        raise FeedError("invalid_response")
    entries: list[FeedEntry] = []
    for row in rows[:GITHUB_ADVISORY_LIMIT]:
        if not isinstance(row, dict) or row.get("withdrawn_at") or not _matches_filters(row, settings):
            continue
        ghsa_id = str(row.get("ghsa_id") or "").strip().upper()
        updated_at = str(row.get("updated_at") or "").strip()
        if not ghsa_id.startswith("GHSA-") or not updated_at:
            continue
        severity = str(row.get("severity") or "").strip().upper()
        summary = clean_feed_html(str(row.get("summary") or ""), limit=240)
        description = clean_feed_html(str(row.get("description") or ""), limit=8_000)
        cve = str(row.get("cve_id") or "").strip().upper()
        affected: list[str] = []
        for vulnerability in (row.get("vulnerabilities") or [])[:20]:
            if not isinstance(vulnerability, dict):
                continue
            package = vulnerability.get("package") or {}
            package_name = str(package.get("name") or "").strip()
            ecosystem = str(package.get("ecosystem") or "").strip()
            version_range = str(vulnerability.get("vulnerable_version_range") or "").strip()
            patched_value = vulnerability.get("first_patched_version")
            if isinstance(patched_value, dict):
                patched = str(patched_value.get("identifier") or "").strip()
            elif isinstance(patched_value, str):
                patched = patched_value.strip()
            else:
                patched = ""
            if package_name:
                affected.append(
                    f"{ecosystem}/{package_name}: 受影响 {version_range or '未注明'}；修复 {patched or '暂未提供'}"
                )
        body_parts = [part for part in (cve, description, "\n".join(affected)) if part]
        html_url = str(row.get("html_url") or "").strip()
        if not html_url.startswith("https://github.com/advisories/"):
            html_url = f"https://github.com/advisories/{ghsa_id}"
        entries.append(
            FeedEntry(
                external_id=f"github-advisory:{ghsa_id}:{updated_at}",
                title=f"[{severity}] {ghsa_id} {summary}"[:300],
                body="\n\n".join(body_parts)[:12_000],
                url=html_url,
                published_at=updated_at,
            )
        )
    return ParsedFeed(tuple(entries))


class GitHubAdvisoriesFetcher:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=GITHUB_ADVISORY_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "TelegramPriorityAdvisoryReader/1.0",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, source: dict[str, Any], *, token: str = "") -> FeedFetchResult:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if source.get("etag"):
            headers["If-None-Match"] = str(source["etag"])
        try:
            async with self._client.stream(
                "GET",
                "https://api.github.com/advisories?type=reviewed&sort=updated&direction=desc&per_page=100",
                headers=headers,
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
                    if len(content) > GITHUB_ADVISORY_RESPONSE_MAX_BYTES:
                        raise FeedError("response_too_large")
                return FeedFetchResult(
                    False,
                    parse_github_advisories(bytes(content), settings=source.get("settings") or {}),
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


class GitHubAdvisoriesPoller(GitHubSourcePoller):
    SOURCE_KIND = "github_advisories"
    SENDER_NAME = "GitHub Security Advisories"
    LOG_LABEL = "GitHub Security Advisories"
    FETCHER_TYPE = GitHubAdvisoriesFetcher
