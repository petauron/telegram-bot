from __future__ import annotations

import json
import unittest

import httpx

from app.feeds import FeedError
from app.vendor_status import (
    VendorStatusFetcher,
    parse_status_incidents,
    status_incidents_api_url,
    validate_status_page_url,
)


INCIDENTS = {
    "incidents": [
        {
            "id": "incident-001",
            "name": "API 请求失败",
            "status": "monitoring",
            "impact": "major",
            "updated_at": "2026-08-11T09:30:00Z",
            "shortlink": "https://status.example.com/incidents/incident-001",
            "incident_updates": [
                {
                    "status": "monitoring",
                    "body": "A fix has been deployed and recovery is being monitored.",
                    "created_at": "2026-08-11T09:30:00Z",
                }
            ],
        }
    ]
}


class VendorStatusTests(unittest.IsolatedAsyncioTestCase):
    def test_url_validation_and_api_path(self) -> None:
        self.assertEqual(
            validate_status_page_url("https://status.example.com/"),
            "https://status.example.com/",
        )
        self.assertEqual(
            status_incidents_api_url("https://status.example.com"),
            "https://status.example.com/api/v2/incidents.json",
        )
        for value in (
            "http://status.example.com",
            "https://127.0.0.1",
            "https://user:pass@status.example.com",
            "https://status.example.com/api/v2/incidents.json",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_status_page_url(value)

    def test_incident_updates_are_versioned_and_readable(self) -> None:
        first = parse_status_incidents(
            json.dumps(INCIDENTS).encode(), source_url="https://status.example.com/"
        ).entries[0]
        self.assertIn("监控中", first.title)
        self.assertIn("影响：重大", first.body)
        changed = json.loads(json.dumps(INCIDENTS))
        changed["incidents"][0]["status"] = "resolved"
        changed["incidents"][0]["updated_at"] = "2026-08-11T09:40:00Z"
        second = parse_status_incidents(
            json.dumps(changed).encode(), source_url="https://status.example.com/"
        ).entries[0]
        self.assertNotEqual(first.external_id, second.external_id)
        self.assertIn("已恢复", second.title)

    async def test_fetch_uses_public_statuspage_json_without_redirects(self) -> None:
        requests: list[httpx.Request] = []

        async def resolver(_: str) -> frozenset[str]:
            return frozenset({"203.0.113.10"})

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=INCIDENTS, headers={"ETag": '"status-v1"'})

        fetcher = VendorStatusFetcher(
            transport=httpx.MockTransport(handler), resolver=resolver
        )
        try:
            result = await fetcher.fetch({"url": "https://status.example.com"})
            self.assertEqual(
                str(requests[0].url),
                "https://status.example.com/api/v2/incidents.json",
            )
            self.assertEqual(result.etag, '"status-v1"')
            self.assertEqual(len(result.feed.entries), 1)
        finally:
            await fetcher.close()

    def test_invalid_response_is_stable_error(self) -> None:
        with self.assertRaises(FeedError) as raised:
            parse_status_incidents(b"[]", source_url="https://status.example.com/")
        self.assertEqual(raised.exception.category, "invalid_response")


if __name__ == "__main__":
    unittest.main()
