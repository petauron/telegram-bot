from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from app.database import utc_now
from app.feeds import FeedEntry, FeedError, FeedFetchResult, ParsedFeed, clean_feed_html
from app.github_releases import GitHubSourcePoller


NVD_API_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
NVD_WEB_URL = "https://nvd.nist.gov/vuln/detail"
NVD_SOURCE_WEB_URL = "https://nvd.nist.gov/vuln/search"
NVD_RESPONSE_MAX_BYTES = 12 * 1024 * 1024
NVD_RESULT_LIMIT = 2_000
NVD_TIMEOUT = httpx.Timeout(35.0, connect=5.0, read=30.0, write=5.0, pool=5.0)


def validate_nvd_settings(settings: dict[str, Any]) -> dict[str, Any]:
    keywords = tuple(
        dict.fromkeys(
            str(value).strip()[:80]
            for value in settings.get("keywords") or ()
            if str(value).strip()
        )
    )[:40]
    return {"keywords": list(keywords)}


def _english_description(cve: dict[str, Any]) -> str:
    rows = cve.get("descriptions") or []
    preferred = next(
        (row for row in rows if isinstance(row, dict) and row.get("lang") == "en"),
        rows[0] if rows else {},
    )
    return clean_feed_html(str(preferred.get("value") or ""), limit=5_000)


def _cvss(cve: dict[str, Any]) -> tuple[str, float | None]:
    metrics = cve.get("metrics") or {}
    candidates: list[tuple[float, str]] = []
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        for metric in metrics.get(key) or []:
            if not isinstance(metric, dict):
                continue
            data = metric.get("cvssData") or {}
            try:
                score = float(data.get("baseScore"))
            except (TypeError, ValueError):
                continue
            severity = str(data.get("baseSeverity") or metric.get("baseSeverity") or "").upper()
            candidates.append((score, severity))
    if not candidates:
        return "UNKNOWN", None
    score, severity = max(candidates, key=lambda item: item[0])
    if not severity:
        severity = "CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "LOW"
    return severity, score


def _cpe_text(cve: dict[str, Any]) -> str:
    values: list[str] = []
    for configuration in cve.get("configurations") or []:
        if not isinstance(configuration, dict):
            continue
        for node in configuration.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            for match in node.get("cpeMatch") or []:
                if isinstance(match, dict) and match.get("criteria"):
                    values.append(str(match["criteria"]))
    return " ".join(values[:100])


def parse_nvd_cves(payload: bytes, *, settings: dict[str, Any]) -> ParsedFeed:
    if not payload or len(payload) > NVD_RESPONSE_MAX_BYTES:
        raise FeedError("response_too_large")
    try:
        document = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise FeedError("invalid_response") from exc
    rows = document.get("vulnerabilities") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise FeedError("invalid_response")
    keywords = validate_nvd_settings(settings)["keywords"]
    entries: list[FeedEntry] = []
    for wrapper in rows[:NVD_RESULT_LIMIT]:
        cve = wrapper.get("cve") if isinstance(wrapper, dict) else None
        if not isinstance(cve, dict) or str(cve.get("vulnStatus") or "").casefold() == "rejected":
            continue
        cve_id = str(cve.get("id") or "").strip().upper()
        modified = str(cve.get("lastModified") or "").strip()
        if not cve_id.startswith("CVE-") or not modified:
            continue
        description = _english_description(cve)
        severity, score = _cvss(cve)
        kev = bool(cve.get("cisaExploitAdd"))
        haystack = f"{cve_id}\n{description}\n{_cpe_text(cve)}".casefold()
        keyword_match = any(keyword.casefold() in haystack for keyword in keywords)
        if severity not in {"HIGH", "CRITICAL"} and not kev and not keyword_match:
            continue
        cisa_parts = [
            f"CISA KEV 加入日期：{cve.get('cisaExploitAdd')}" if kev else "",
            f"CISA 要求措施：{clean_feed_html(str(cve.get('cisaRequiredAction') or ''), limit=800)}"
            if cve.get("cisaRequiredAction") else "",
            f"CISA 截止日期：{cve.get('cisaActionDue')}" if cve.get("cisaActionDue") else "",
        ]
        metric = f"CVSS：{score:.1f} {severity}" if score is not None else f"严重度：{severity}"
        body = "\n\n".join(part for part in (metric, description, *cisa_parts) if part)[:12_000]
        entries.append(
            FeedEntry(
                external_id=f"nvd-cve:{cve_id}:{modified}",
                title=f"NVD · {cve_id} · {severity}"[:300],
                body=body,
                url=f"{NVD_WEB_URL}/{cve_id}",
                published_at=modified,
            )
        )
    return ParsedFeed(tuple(entries))


def _nvd_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class NvdCveFetcher:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        self._now = now_provider
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=NVD_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            headers={"Accept": "application/json", "User-Agent": "TelegramPriorityNvdReader/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, source: dict[str, Any], *, token: str = "") -> FeedFetchResult:
        end = self._now()
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        last_success = _parse_time(source.get("last_success_at"))
        start = (last_success - timedelta(minutes=5)) if last_success else (end - timedelta(hours=24))
        start = max(start, end - timedelta(days=120))
        params = {
            "lastModStartDate": _nvd_timestamp(start),
            "lastModEndDate": _nvd_timestamp(end),
            "resultsPerPage": str(NVD_RESULT_LIMIT),
        }
        headers = {"apiKey": token} if token else {}
        try:
            async with self._client.stream("GET", NVD_API_URL, params=params, headers=headers) as response:
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
                    if len(content) > NVD_RESPONSE_MAX_BYTES:
                        raise FeedError("response_too_large")
                return FeedFetchResult(
                    False,
                    parse_nvd_cves(bytes(content), settings=source.get("settings") or {}),
                    None,
                    None,
                    response.status_code,
                )
        except FeedError:
            raise
        except httpx.TimeoutException as exc:
            raise FeedError("timeout") from exc
        except httpx.HTTPError as exc:
            raise FeedError("network_error") from exc


class NvdCvePoller(GitHubSourcePoller):
    SOURCE_KIND = "nvd_cve"
    SENDER_NAME = "NVD CVE"
    LOG_LABEL = "NVD CVE"
    FETCHER_TYPE = NvdCveFetcher
    CREDENTIAL_PROVIDER = "nvd"
