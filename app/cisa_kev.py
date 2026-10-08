from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from app.feeds import FeedEntry, FeedError, FeedFetchResult, ParsedFeed, clean_feed_html
from app.github_releases import GitHubSourcePoller


CISA_KEV_API_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
CISA_KEV_WEB_URL = "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"
CISA_KEV_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
CISA_KEV_ENTRY_LIMIT = 4_000
CISA_KEV_TIMEOUT = httpx.Timeout(25.0, connect=5.0, read=20.0, write=5.0, pool=5.0)


def parse_cisa_kev(payload: bytes) -> ParsedFeed:
    if not payload or len(payload) > CISA_KEV_RESPONSE_MAX_BYTES:
        raise FeedError("response_too_large")
    try:
        document = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise FeedError("invalid_response") from exc
    rows = document.get("vulnerabilities") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise FeedError("invalid_response")
    entries: list[FeedEntry] = []
    for row in rows[:CISA_KEV_ENTRY_LIMIT]:
        if not isinstance(row, dict):
            continue
        cve = str(row.get("cveID") or "").strip().upper()
        date_added = str(row.get("dateAdded") or "").strip()
        if not cve.startswith("CVE-") or not date_added:
            continue
        vendor = clean_feed_html(str(row.get("vendorProject") or ""), limit=120)
        product = clean_feed_html(str(row.get("product") or ""), limit=160)
        name = clean_feed_html(str(row.get("vulnerabilityName") or ""), limit=240)
        description = clean_feed_html(str(row.get("shortDescription") or ""), limit=2_000)
        action = clean_feed_html(str(row.get("requiredAction") or ""), limit=1_000)
        due_date = str(row.get("dueDate") or "").strip()
        ransomware = str(row.get("knownRansomwareCampaignUse") or "").strip()
        notes = clean_feed_html(str(row.get("notes") or ""), limit=1_000)
        cwes = ", ".join(str(item)[:40] for item in (row.get("cwes") or [])[:10])
        body_parts = [
            f"厂商/产品：{vendor} {product}".strip(),
            description,
            f"要求措施：{action}" if action else "",
            f"截止日期：{due_date}" if due_date else "",
            f"勒索软件利用：{ransomware}" if ransomware else "",
            f"CWE：{cwes}" if cwes else "",
            notes,
        ]
        fingerprint_input = json.dumps(
            {
                "description": description,
                "action": action,
                "due": due_date,
                "ransomware": ransomware,
                "notes": notes,
                "cwes": cwes,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:20]
        entries.append(
            FeedEntry(
                external_id=f"cisa-kev:{cve}:{fingerprint}",
                title=f"CISA KEV · {cve} · {name or f'{vendor} {product}'}"[:300],
                body="\n\n".join(part for part in body_parts if part)[:12_000],
                url=CISA_KEV_WEB_URL,
                published_at=f"{date_added}T00:00:00Z",
            )
        )
    if not entries:
        raise FeedError("invalid_response")
    return ParsedFeed(tuple(entries))


class CisaKevFetcher:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=CISA_KEV_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            headers={"Accept": "application/json", "User-Agent": "TelegramPriorityCisaKevReader/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, source: dict[str, Any], *, token: str = "") -> FeedFetchResult:
        del token
        headers: dict[str, str] = {}
        if source.get("etag"):
            headers["If-None-Match"] = str(source["etag"])
        if source.get("last_modified"):
            headers["If-Modified-Since"] = str(source["last_modified"])
        try:
            async with self._client.stream("GET", CISA_KEV_API_URL, headers=headers) as response:
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
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > CISA_KEV_RESPONSE_MAX_BYTES:
                        raise FeedError("response_too_large")
                return FeedFetchResult(
                    False,
                    parse_cisa_kev(bytes(content)),
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


class CisaKevPoller(GitHubSourcePoller):
    SOURCE_KIND = "cisa_kev"
    SENDER_NAME = "CISA KEV"
    LOG_LABEL = "CISA KEV"
    FETCHER_TYPE = CisaKevFetcher
    CREDENTIAL_PROVIDER = None
