from __future__ import annotations

import json
import unittest

import httpx

from app.feeds import FeedError
from app.github_advisories import (
    GitHubAdvisoriesFetcher,
    github_advisories_web_url,
    parse_github_advisories,
    validate_advisory_settings,
)


ADVISORIES = [
    {
        "ghsa_id": "GHSA-AAAA-BBBB-CCCC",
        "cve_id": "CVE-2026-1000",
        "severity": "high",
        "summary": "Example package remote code execution",
        "description": "A crafted request can execute code.",
        "published_at": "2026-08-10T01:00:00Z",
        "updated_at": "2026-08-11T02:00:00Z",
        "withdrawn_at": None,
        "html_url": "https://github.com/advisories/GHSA-AAAA-BBBB-CCCC",
        "vulnerabilities": [
            {
                "package": {"ecosystem": "npm", "name": "example-package"},
                "vulnerable_version_range": "< 2.4.1",
                "first_patched_version": {"identifier": "2.4.1"},
            }
        ],
    },
    {
        "ghsa_id": "GHSA-DDDD-EEEE-FFFF",
        "severity": "medium",
        "summary": "Low impact issue",
        "description": "Not selected.",
        "updated_at": "2026-08-11T03:00:00Z",
        "withdrawn_at": None,
        "vulnerabilities": [],
    },
]


class GitHubAdvisoryTests(unittest.IsolatedAsyncioTestCase):
    def test_settings_and_high_critical_product_filter(self) -> None:
        settings = validate_advisory_settings(
            {"ecosystem": "npm", "minimum_severity": "high", "keywords": ["example-package"]}
        )
        self.assertEqual(settings["ecosystem"], "npm")
        self.assertIn("severity%3Ahigh", github_advisories_web_url(settings))
        parsed = parse_github_advisories(json.dumps(ADVISORIES).encode(), settings=settings)
        self.assertEqual(len(parsed.entries), 1)
        entry = parsed.entries[0]
        self.assertEqual(
            entry.external_id,
            "github-advisory:GHSA-AAAA-BBBB-CCCC:2026-08-11T02:00:00Z",
        )
        self.assertIn("CVE-2026-1000", entry.body)
        self.assertIn("修复 2.4.1", entry.body)

        no_match = parse_github_advisories(
            json.dumps(ADVISORIES).encode(),
            settings={"ecosystem": "pip", "minimum_severity": "high", "keywords": []},
        )
        self.assertEqual(no_match.entries, ())
        for invalid in (
            {"ecosystem": "unknown", "minimum_severity": "high"},
            {"ecosystem": "npm", "minimum_severity": "medium"},
        ):
            with self.assertRaises(ValueError):
                validate_advisory_settings(invalid)

    def test_updated_timestamp_produces_a_new_id_for_substantive_updates(self) -> None:
        first = parse_github_advisories(
            json.dumps(ADVISORIES[:1]).encode(), settings={"minimum_severity": "high"}
        ).entries[0]
        updated = [{**ADVISORIES[0], "updated_at": "2026-08-11T04:00:00Z"}]
        second = parse_github_advisories(
            json.dumps(updated).encode(), settings={"minimum_severity": "high"}
        ).entries[0]
        self.assertNotEqual(first.external_id, second.external_id)

    def test_first_patched_version_accepts_the_official_string_shape(self) -> None:
        advisory = json.loads(json.dumps(ADVISORIES[0]))
        advisory["vulnerabilities"][0]["first_patched_version"] = "2.4.2"
        entry = parse_github_advisories(
            json.dumps([advisory]).encode(),
            settings={"minimum_severity": "high"},
        ).entries[0]
        self.assertIn("修复 2.4.2", entry.body)

    async def test_fetch_uses_official_global_advisories_api_and_optional_token(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=ADVISORIES, headers={"ETag": '"advisories-v1"'})

        fetcher = GitHubAdvisoriesFetcher(transport=httpx.MockTransport(handler))
        try:
            result = await fetcher.fetch(
                {"settings": {"minimum_severity": "high", "ecosystem": "npm", "keywords": []}},
                token="test-only-token",
            )
            self.assertEqual(len(result.feed.entries), 1)
            self.assertEqual(requests[0].url.host, "api.github.com")
            self.assertEqual(requests[0].url.path, "/advisories")
            self.assertNotIn("test-only-token", str(requests[0].url))
            self.assertEqual(requests[0].headers.get("authorization"), "Bearer test-only-token")
        finally:
            await fetcher.close()

    async def test_invalid_payload_and_rate_limit_are_stable_failures(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429)

        fetcher = GitHubAdvisoriesFetcher(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(FeedError) as raised:
                await fetcher.fetch({"settings": {"minimum_severity": "high"}})
            self.assertEqual(raised.exception.category, "rate_limited")
        finally:
            await fetcher.close()
        with self.assertRaises(FeedError) as invalid:
            parse_github_advisories(b"{}", settings={"minimum_severity": "high"})
        self.assertEqual(invalid.exception.category, "invalid_response")


if __name__ == "__main__":
    unittest.main()
