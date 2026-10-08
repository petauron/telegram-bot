from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

import httpx

from app.feeds import FeedError
from app.nvd_cve import NVD_API_URL, NvdCveFetcher, parse_nvd_cves


VULNERABILITIES = {
    "resultsPerPage": 3,
    "totalResults": 3,
    "vulnerabilities": [
        {
            "cve": {
                "id": "CVE-2026-20001",
                "vulnStatus": "Analyzed",
                "published": "2026-08-10T00:00:00.000",
                "lastModified": "2026-08-11T01:00:00.000",
                "descriptions": [{"lang": "en", "value": "Critical remote code execution in Example Gateway."}],
                "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"}}]},
                "configurations": [],
            }
        },
        {
            "cve": {
                "id": "CVE-2026-20002",
                "vulnStatus": "Analyzed",
                "lastModified": "2026-08-11T02:00:00.000",
                "descriptions": [{"lang": "en", "value": "Medium issue in watched-product agent."}],
                "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 5.0, "baseSeverity": "MEDIUM"}}]},
                "configurations": [],
            }
        },
        {
            "cve": {
                "id": "CVE-2026-20003",
                "vulnStatus": "Analyzed",
                "lastModified": "2026-08-11T03:00:00.000",
                "descriptions": [{"lang": "en", "value": "Medium issue with active exploitation."}],
                "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 6.0, "baseSeverity": "MEDIUM"}}]},
                "cisaExploitAdd": "2026-08-11",
                "cisaActionDue": "2026-08-20",
                "cisaRequiredAction": "Apply mitigations.",
                "configurations": [],
            }
        },
    ],
}


class NvdCveTests(unittest.IsolatedAsyncioTestCase):
    def test_high_critical_keyword_or_kev_filter(self) -> None:
        feed = parse_nvd_cves(
            json.dumps(VULNERABILITIES).encode(), settings={"keywords": ["watched-product"]}
        )
        self.assertEqual(len(feed.entries), 3)
        self.assertIn("9.8 CRITICAL", feed.entries[0].body)
        self.assertIn("CISA KEV 加入日期", feed.entries[2].body)
        without_keyword = parse_nvd_cves(
            json.dumps(VULNERABILITIES).encode(), settings={"keywords": []}
        )
        self.assertEqual(
            [entry.external_id.split(":", 2)[1] for entry in without_keyword.entries],
            ["CVE-2026-20001", "CVE-2026-20003"],
        )

    def test_last_modified_is_part_of_id(self) -> None:
        first = parse_nvd_cves(json.dumps(VULNERABILITIES).encode(), settings={}).entries[0]
        changed = json.loads(json.dumps(VULNERABILITIES))
        changed["vulnerabilities"][0]["cve"]["lastModified"] = "2026-08-11T05:00:00.000"
        second = parse_nvd_cves(json.dumps(changed).encode(), settings={}).entries[0]
        self.assertNotEqual(first.external_id, second.external_id)

    async def test_fetch_is_incremental_and_key_is_header_only(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=VULNERABILITIES)

        now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        fetcher = NvdCveFetcher(
            transport=httpx.MockTransport(handler), now_provider=lambda: now
        )
        try:
            await fetcher.fetch({}, token="test-only-nvd-key")
            first = requests[0]
            self.assertEqual(f"{first.url.scheme}://{first.url.host}{first.url.path}", NVD_API_URL)
            self.assertIn("lastModStartDate=2026-08-10", str(first.url))
            self.assertNotIn("test-only-nvd-key", str(first.url))
            self.assertEqual(first.headers.get("apikey"), "test-only-nvd-key")
            await fetcher.fetch({"last_success_at": "2026-08-11T10:00:00+00:00"})
            self.assertIn("lastModStartDate=2026-08-11T09%3A55", str(requests[1].url))
        finally:
            await fetcher.close()

    def test_invalid_payload_is_stable_error(self) -> None:
        with self.assertRaises(FeedError) as raised:
            parse_nvd_cves(b"[]", settings={})
        self.assertEqual(raised.exception.category, "invalid_response")


if __name__ == "__main__":
    unittest.main()
