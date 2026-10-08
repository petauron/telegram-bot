from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta

import httpx

from app.database import Database, information_source_chat_id, utc_now
from app.feeds import (
    FEED_RESPONSE_MAX_BYTES,
    FeedError,
    FeedFetchResult,
    ParsedFeed,
    RSSFetcher,
    RSSPoller,
    clean_feed_html,
    parse_feed,
    validate_feed_url,
)


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Official</title>
<item><guid>release-1</guid><title>Product 2.0 released</title>
<link>https://vendor.example/releases/2?utm_source=feed</link>
<description><![CDATA[<p>Faster builds.</p><script>bad()</script>]]></description>
<pubDate>Mon, 10 Aug 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Security</title>
<entry><id>notice-2</id><title>Security notice</title>
<link rel="alternate" href="https://vendor.example/security/2"/>
<summary type="html">Affected versions 1.0 through 1.4.</summary>
<updated>2026-08-10T09:00:00Z</updated></entry>
</feed>"""


class FeedParsingTests(unittest.TestCase):
    def test_parses_rss_and_atom_into_bounded_plain_text(self) -> None:
        rss = parse_feed(RSS).entries[0]
        atom = parse_feed(ATOM).entries[0]
        self.assertRegex(rss.external_id, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(rss.body, "Faster builds.")
        self.assertEqual(rss.url, "https://vendor.example/releases/2")
        self.assertRegex(atom.external_id, r"^sha256:[0-9a-f]{64}$")
        self.assertIn("Affected versions", atom.body)

    def test_tolerates_only_the_known_malformed_trailing_cache_comment(self) -> None:
        malformed_suffix = b"\n            <!--Cached 1786486485--->\n"
        parsed = parse_feed(RSS + malformed_suffix)
        self.assertEqual(parsed.entries[0].title, "Product 2.0 released")

        for payload in (
            RSS + b"\n<!--Cached not-a-timestamp--->\n",
            RSS.replace(
                b"</channel>",
                b"<!--Cached 1786486485---></channel>",
            ),
        ):
            with self.subTest(payload=payload), self.assertRaises(FeedError) as raised:
                parse_feed(payload)
            self.assertEqual(raised.exception.category, "invalid_feed")

    def test_html_cleanup_preserves_limited_paragraphs(self) -> None:
        value = clean_feed_html("<p>First</p><p>Second</p><script>secret</script>")
        self.assertEqual(value, "First\n\nSecond")

    def test_rejects_entities_and_oversized_payload(self) -> None:
        entity = b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&e;</title></item></channel></rss>'
        with self.assertRaises(FeedError) as raised:
            parse_feed(entity)
        self.assertEqual(raised.exception.category, "invalid_feed")
        with self.assertRaises(FeedError) as oversized:
            parse_feed(b"x" * (FEED_RESPONSE_MAX_BYTES + 1))
        self.assertEqual(oversized.exception.category, "response_too_large")

    def test_feed_url_requires_public_https_shape(self) -> None:
        self.assertEqual(
            validate_feed_url(" https://feeds.example.com/news.xml "),
            "https://feeds.example.com/news.xml",
        )
        for value in (
            "http://feeds.example.com/rss",
            "https://user:pass@feeds.example.com/rss",
            "https://127.0.0.1/rss",
            "https://feeds.example.com:8443/rss",
            "https://feeds.example.com/rss#item",
            "https://feeds.example.com/rss?access_token=hidden",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_feed_url(value)


class FeedFetcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_conditional_fetch_and_redirect_blocking(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/redirect":
                return httpx.Response(302, headers={"Location": "https://other.example/rss"})
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304)
            return httpx.Response(200, content=RSS, headers={"ETag": '"v1"'})

        async def resolver(_: str) -> frozenset[str]:
            return frozenset({"203.0.113.10"})

        fetcher = RSSFetcher(transport=httpx.MockTransport(handler), resolver=resolver)
        try:
            first = await fetcher.fetch({"url": "https://feeds.example.com/rss"})
            self.assertFalse(first.not_modified)
            self.assertEqual(first.etag, '"v1"')
            second = await fetcher.fetch(
                {"url": "https://feeds.example.com/rss", "etag": '"v1"'}
            )
            self.assertTrue(second.not_modified)
            with self.assertRaises(FeedError) as redirect:
                await fetcher.fetch({"url": "https://feeds.example.com/redirect"})
            self.assertEqual(redirect.exception.category, "redirect_blocked")
            self.assertEqual(len(requests), 3)
        finally:
            await fetcher.close()

    async def test_private_dns_resolution_is_rejected_before_request(self) -> None:
        called = False

        async def handler(_: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, content=RSS)

        async def resolver(_: str) -> frozenset[str]:
            raise FeedError("unsafe_address")

        fetcher = RSSFetcher(transport=httpx.MockTransport(handler), resolver=resolver)
        try:
            with self.assertRaises(FeedError) as raised:
                await fetcher.fetch({"url": "https://feeds.example.com/rss"})
            self.assertEqual(raised.exception.category, "unsafe_address")
            self.assertFalse(called)
        finally:
            await fetcher.close()


class _SequenceFetcher:
    def __init__(self, feeds: list[ParsedFeed]) -> None:
        self.feeds = feeds
        self.calls = 0

    async def fetch(self, _: dict) -> FeedFetchResult:
        feed = self.feeds[self.calls]
        self.calls += 1
        return FeedFetchResult(False, feed, f'"v{self.calls}"', None, 200)

    async def close(self) -> None:
        pass


class _Queue:
    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


class RSSPollerTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_fetch_baselines_then_only_new_item_enters_pipeline_once(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            database = Database(handle.name)
            now = utc_now()
            database.initialize_runtime_config(
                important_keywords=("发布",),
                trusted_sender_ids=frozenset(),
                watch_chat_ids=frozenset(),
                immediate_score=80,
                now=now,
            )
            source = database.create_information_source(
                kind="rss",
                name="Official feed",
                url="https://feeds.example.com/rss",
                enabled=True,
                poll_interval_minutes=15,
                now=now,
            )
            database.close()

            first = parse_feed(RSS)
            second = ParsedFeed((*first.entries, parse_feed(ATOM).entries[0]))
            fetcher = _SequenceFetcher([first, second, second])
            queue = _Queue()
            poller = RSSPoller(handle.name, queue, fetcher=fetcher, worker_count=1)
            await poller._poll(source)

            database = Database(handle.name, initialize=False)
            after_baseline = database.get_information_source(source["id"])
            self.assertTrue(after_baseline["initialized"])
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            database.close()

            await poller._poll(after_baseline)
            database = Database(handle.name, initialize=False)
            message = database.connection.execute("SELECT * FROM messages").fetchone()
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(message["source_type"], "rss")
            self.assertEqual(message["chat_id"], information_source_chat_id(source["id"]))
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0], 1)
            current = database.get_information_source(source["id"])
            database.close()

            await poller._poll(current)
            database = Database(handle.name, initialize=False)
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0], 1)
            self.assertEqual(queue.wakes, 1)
            database.close()
            await poller.close()

    async def test_url_change_starts_new_baseline_generation_and_recovery_is_bounded(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            database = Database(handle.name)
            now = utc_now()
            source = database.create_information_source(
                kind="rss",
                name="Official feed",
                url="https://feeds.example.com/rss",
                enabled=True,
                poll_interval_minutes=15,
                now=now,
            )
            claimed = database.claim_due_information_source(now=now, lease_seconds=30)
            self.assertEqual(claimed["poll_state"], "processing")
            self.assertEqual(
                database.recover_information_source_polls(now=now + timedelta(seconds=31)),
                1,
            )
            reclaimed = database.claim_due_information_source(
                now=now + timedelta(seconds=31), lease_seconds=90
            )
            self.assertEqual(reclaimed["poll_state"], "processing")
            self.assertEqual(
                database.release_information_source_polls(
                    now=now + timedelta(seconds=32)
                ),
                1,
            )
            updated = database.update_information_source(
                source["id"],
                name="Official feed",
                url="https://feeds.example.com/atom.xml",
                enabled=True,
                poll_interval_minutes=30,
                now=now + timedelta(minutes=1),
            )
            self.assertEqual(updated["generation"], 2)
            self.assertFalse(updated["initialized"])
            self.assertIsNone(updated["etag"])
            self.assertEqual(updated["poll_state"], "idle")
            database.close()


if __name__ == "__main__":
    unittest.main()
