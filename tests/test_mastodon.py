from __future__ import annotations

import unittest
import tempfile
from urllib.parse import urlparse

import httpx

from app.mastodon import (
    MastodonFetcher,
    MastodonPoller,
    mastodon_source_url,
    parse_mastodon_statuses,
    validate_mastodon_settings,
)
from app.database import Database, utc_now
from app.feeds import FeedFetchResult, ParsedFeed


STATUS = {
    "id": "1099001",
    "created_at": "2026-08-11T09:00:00Z",
    "content": "<p>Open source cloud platform published a security release.</p>",
    "spoiler_text": "Release notice",
    "url": "https://social.example/@trusted/1099001",
    "in_reply_to_id": None,
    "reblog": None,
}


class MastodonTests(unittest.IsolatedAsyncioTestCase):
    def test_source_credential_schema_is_idempotent_and_write_only_by_default(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            database = Database(handle.name)
            source = database.create_information_source(
                kind="mastodon",
                name="Trusted account",
                url="https://social.example/@trusted",
                settings={"instance_url": "https://social.example", "timeline_type": "account", "target": "trusted"},
                enabled=True,
                poll_interval_minutes=15,
                now=utc_now(),
                secret_value="test-only-source-token",
            )
            self.assertTrue(source["secret_configured"])
            public = database.get_information_source_credential(source["id"])
            self.assertTrue(public["configured"])
            self.assertNotIn("secret_value", public)
            database.close()

    async def test_poller_loads_only_its_source_scoped_token(self) -> None:
        class Fetcher:
            def __init__(self) -> None:
                self.token = ""

            async def fetch(self, _: dict, *, token: str = "") -> FeedFetchResult:
                self.token = token
                return FeedFetchResult(False, ParsedFeed(()), None, None, 200)

            async def close(self) -> None:
                return None

        class Queue:
            def wake(self) -> None:
                raise AssertionError("baseline must not enqueue")

        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            database = Database(handle.name)
            source = database.create_information_source(
                kind="mastodon",
                name="Trusted account",
                url="https://social.example/@trusted",
                settings={"instance_url": "https://social.example", "timeline_type": "account", "target": "trusted"},
                enabled=True,
                poll_interval_minutes=15,
                now=utc_now(),
                secret_value="test-only-source-token",
            )
            database.close()
            fetcher = Fetcher()
            poller = MastodonPoller(handle.name, Queue(), fetcher=fetcher)
            await poller._poll(source)
            self.assertEqual(fetcher.token, "test-only-source-token")
            await poller.close()
            database = Database(handle.name)
            self.assertTrue(database.get_information_source(source["id"])["secret_configured"])
            database.close()

    def test_settings_are_instance_scoped(self) -> None:
        config = validate_mastodon_settings(
            {
                "instance_url": "https://social.example/",
                "timeline_type": "account",
                "target": "@trusted@social.example",
            }
        )
        self.assertEqual(config["target"], "trusted")
        self.assertEqual(mastodon_source_url(config), "https://social.example/@trusted")
        with self.assertRaises(ValueError):
            validate_mastodon_settings(
                {
                    "instance_url": "https://social.example",
                    "timeline_type": "account",
                    "target": "trusted@other.example",
                }
            )

    def test_status_parser_excludes_replies_and_keeps_public_content(self) -> None:
        reply = {**STATUS, "id": "1099002", "in_reply_to_id": "1098000"}
        feed = parse_mastodon_statuses(
            [STATUS, reply], fallback_url="https://social.example/@trusted"
        )
        self.assertEqual(len(feed.entries), 1)
        self.assertEqual(feed.entries[0].external_id, "mastodon-status:1099001")
        self.assertIn("Release notice", feed.entries[0].body)
        self.assertNotIn("<p>", feed.entries[0].body)

    async def test_account_fetch_uses_official_api_and_token_header_only(self) -> None:
        requests: list[httpx.Request] = []

        async def resolver(_: str) -> frozenset[str]:
            return frozenset({"203.0.113.20"})

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            path = urlparse(str(request.url)).path
            if path.endswith("/api/v1/accounts/lookup"):
                return httpx.Response(200, json={"id": "42"})
            if path.endswith("/api/v1/accounts/42/statuses"):
                return httpx.Response(200, json=[STATUS])
            return httpx.Response(404)

        fetcher = MastodonFetcher(
            transport=httpx.MockTransport(handler), resolver=resolver
        )
        try:
            result = await fetcher.fetch(
                {
                    "cursor_value": "1098000",
                    "settings": {
                        "instance_url": "https://social.example",
                        "timeline_type": "account",
                        "target": "trusted",
                    },
                },
                token="test-only-mastodon-token",
            )
            self.assertEqual(len(result.feed.entries), 1)
            self.assertEqual(result.cursor_value, "1099001")
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(request.headers.get("authorization") == "Bearer test-only-mastodon-token" for request in requests))
            self.assertTrue(all("test-only-mastodon-token" not in str(request.url) for request in requests))
            self.assertEqual(requests[1].url.params.get("since_id"), "1098000")
            self.assertEqual(requests[1].url.params.get("exclude_replies"), "true")
        finally:
            await fetcher.close()

    async def test_tag_fetch_skips_account_lookup(self) -> None:
        requests: list[str] = []

        async def resolver(_: str) -> frozenset[str]:
            return frozenset({"203.0.113.20"})

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(200, json=[STATUS])

        fetcher = MastodonFetcher(transport=httpx.MockTransport(handler), resolver=resolver)
        try:
            await fetcher.fetch(
                {
                    "settings": {
                        "instance_url": "https://social.example",
                        "timeline_type": "tag",
                        "target": "opensource",
                    }
                }
            )
            self.assertEqual(len(requests), 1)
            self.assertIn("/api/v1/timelines/tag/opensource", requests[0])
        finally:
            await fetcher.close()


if __name__ == "__main__":
    unittest.main()
