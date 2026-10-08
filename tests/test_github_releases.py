from __future__ import annotations

import json
import tempfile
import unittest

import httpx

from app.database import Database, utc_now
from app.feeds import FeedError
from app.github_releases import (
    GitHubReleasesFetcher,
    github_releases_web_url,
    parse_github_releases,
    validate_github_repository,
)


RELEASES = [
    {
        "id": 101,
        "tag_name": "v2.0.0",
        "name": "Version 2",
        "body": "Faster builds and a security fix.",
        "draft": False,
        "prerelease": False,
        "html_url": "https://github.com/example/tool/releases/tag/v2.0.0",
        "published_at": "2026-08-11T01:00:00Z",
    },
    {
        "id": 102,
        "tag_name": "v3.0.0-rc1",
        "name": "Version 3 RC",
        "body": "Release candidate.",
        "draft": False,
        "prerelease": True,
        "html_url": "https://github.com/example/tool/releases/tag/v3.0.0-rc1",
        "published_at": "2026-08-11T02:00:00Z",
    },
    {
        "id": 103,
        "tag_name": "hidden",
        "draft": True,
        "prerelease": False,
    },
]


class GitHubReleaseTests(unittest.IsolatedAsyncioTestCase):
    def test_repository_validation_and_release_filtering(self) -> None:
        self.assertEqual(validate_github_repository("example/tool"), "example/tool")
        self.assertEqual(
            validate_github_repository("https://github.com/example/tool/releases"),
            "example/tool",
        )
        self.assertEqual(
            github_releases_web_url("example/tool"),
            "https://github.com/example/tool/releases",
        )
        for invalid in ("http://github.com/example/tool", "../tool", "example/tool/issues", "user:secret@example/tool"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_github_repository(invalid)

        stable = parse_github_releases(
            json.dumps(RELEASES).encode(), repository="example/tool", include_prereleases=False
        )
        self.assertEqual([item.external_id for item in stable.entries], ["github-release:101"])
        all_releases = parse_github_releases(
            json.dumps(RELEASES).encode(), repository="example/tool", include_prereleases=True
        )
        self.assertEqual(len(all_releases.entries), 2)
        self.assertIn("正式发布", all_releases.entries[0].title)
        self.assertIn("预发布", all_releases.entries[1].title)

    async def test_fetch_uses_official_api_optional_token_and_etag(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304)
            return httpx.Response(200, json=RELEASES, headers={"ETag": '"v1"'})

        fetcher = GitHubReleasesFetcher(transport=httpx.MockTransport(handler))
        source = {
            "url": "https://github.com/example/tool/releases",
            "settings": {"repository": "example/tool", "include_prereleases": False},
        }
        try:
            result = await fetcher.fetch(source, token="secret-test-token")
            self.assertEqual(len(result.feed.entries), 1)
            self.assertEqual(requests[0].url.host, "api.github.com")
            self.assertEqual(requests[0].headers.get("authorization"), "Bearer secret-test-token")
            not_modified = await fetcher.fetch({**source, "etag": '"v1"'})
            self.assertTrue(not_modified.not_modified)
            self.assertIsNone(requests[1].headers.get("authorization"))
        finally:
            await fetcher.close()

    async def test_rate_limit_is_stable_error_category(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})

        fetcher = GitHubReleasesFetcher(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(FeedError) as raised:
                await fetcher.fetch({"settings": {"repository": "example/tool"}})
            self.assertEqual(raised.exception.category, "rate_limited")
        finally:
            await fetcher.close()

    def test_settings_migrate_idempotently_and_token_never_round_trips(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            database = Database(handle.name)
            database.close()
            database = Database(handle.name)
            source = database.create_information_source(
                kind="github_releases",
                name="Example releases",
                url="https://github.com/example/tool/releases",
                settings={"repository": "example/tool", "include_prereleases": True},
                enabled=True,
                poll_interval_minutes=15,
                now=utc_now(),
            )
            self.assertEqual(source["settings"]["repository"], "example/tool")
            public = database.update_source_provider_credential(
                "github", secret_value="secret-test-token", clear_secret=False, now=utc_now()
            )
            self.assertTrue(public["configured"])
            self.assertNotIn("secret_value", public)
            self.assertNotIn("secret-test-token", json.dumps(public))
            kept = database.update_source_provider_credential(
                "github", secret_value="", clear_secret=False, now=utc_now()
            )
            self.assertTrue(kept["configured"])
            cleared = database.update_source_provider_credential(
                "github", secret_value="", clear_secret=True, now=utc_now()
            )
            self.assertFalse(cleared["configured"])
            database.close()


if __name__ == "__main__":
    unittest.main()
