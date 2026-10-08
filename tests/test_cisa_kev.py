from __future__ import annotations

import json
import unittest

import httpx

from app.cisa_kev import CISA_KEV_API_URL, CisaKevFetcher, parse_cisa_kev
from app.feeds import FeedError


CATALOG = {
    "title": "CISA Known Exploited Vulnerabilities Catalog",
    "catalogVersion": "2026.08.11",
    "dateReleased": "2026-08-11T10:00:00Z",
    "count": 1,
    "vulnerabilities": [
        {
            "cveID": "CVE-2026-12345",
            "vendorProject": "Example Vendor",
            "product": "Example Gateway",
            "vulnerabilityName": "Remote code execution",
            "dateAdded": "2026-08-11",
            "shortDescription": "An unauthenticated attacker can execute code.",
            "requiredAction": "Apply the vendor update.",
            "dueDate": "2026-08-20",
            "knownRansomwareCampaignUse": "Known",
            "notes": "Follow vendor guidance.",
            "cwes": ["CWE-78"],
        }
    ],
}


class CisaKevTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_is_high_signal_and_substantive_updates_get_new_ids(self) -> None:
        first = parse_cisa_kev(json.dumps(CATALOG).encode()).entries[0]
        self.assertIn("CVE-2026-12345", first.title)
        self.assertIn("截止日期：2026-08-20", first.body)
        self.assertIn("要求措施：Apply the vendor update.", first.body)
        same = parse_cisa_kev(json.dumps(CATALOG).encode()).entries[0]
        self.assertEqual(first.external_id, same.external_id)
        changed = json.loads(json.dumps(CATALOG))
        changed["vulnerabilities"][0]["dueDate"] = "2026-08-18"
        second = parse_cisa_kev(json.dumps(changed).encode()).entries[0]
        self.assertNotEqual(first.external_id, second.external_id)

    async def test_fetch_uses_fixed_official_url_and_conditional_headers(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.headers.get("if-none-match") == '"kev-v1"':
                return httpx.Response(304)
            return httpx.Response(
                200,
                json=CATALOG,
                headers={"ETag": '"kev-v1"', "Last-Modified": "Tue, 11 Aug 2026 10:00:00 GMT"},
            )

        fetcher = CisaKevFetcher(transport=httpx.MockTransport(handler))
        try:
            result = await fetcher.fetch({})
            self.assertEqual(str(requests[0].url), CISA_KEV_API_URL)
            self.assertEqual(len(result.feed.entries), 1)
            cached = await fetcher.fetch({"etag": '"kev-v1"'})
            self.assertTrue(cached.not_modified)
        finally:
            await fetcher.close()

    def test_invalid_or_empty_catalog_fails_closed_for_collection(self) -> None:
        for payload in (b"{}", b'{"vulnerabilities": []}', b"not-json"):
            with self.subTest(payload=payload), self.assertRaises(FeedError) as raised:
                parse_cisa_kev(payload)
            self.assertEqual(raised.exception.category, "invalid_response")


if __name__ == "__main__":
    unittest.main()
