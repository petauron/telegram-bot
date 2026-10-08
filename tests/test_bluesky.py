from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone

import httpx

from app.bluesky import (
    BLUESKY_COLLECTION,
    BlueskyJetstreamFetcher,
    BlueskyJetstreamPoller,
    parse_jetstream_event,
    validate_bluesky_handle,
)
from app.database import Database, utc_now
from app.feeds import FeedFetchResult, ParsedFeed


DID = "did:plc:example123"
EVENT = {
    "did": DID,
    "time_us": 1_786_426_800_123_456,
    "kind": "commit",
    "commit": {
        "operation": "create",
        "collection": BLUESKY_COLLECTION,
        "rkey": "3examplepost",
        "record": {
            "$type": BLUESKY_COLLECTION,
            "text": "Open source AI tool published with a security update.",
            "createdAt": "2026-08-11T09:00:00Z",
            "embed": {
                "external": {
                    "uri": "https://example.com/release",
                    "title": "Release notes",
                    "description": "Details of the security update.",
                }
            },
        },
    },
}


class FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        raise TimeoutError


class FakeConnection:
    def __init__(self, socket: FakeWebSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> FakeWebSocket:
        return self.socket

    async def __aexit__(self, *_: object) -> None:
        return None


class BlueskyTests(unittest.IsolatedAsyncioTestCase):
    def test_handle_and_event_are_strictly_scoped(self) -> None:
        self.assertEqual(validate_bluesky_handle("@Example.Bsky.Social"), "example.bsky.social")
        for value in ("alice", "https://bsky.app/profile/alice", "localhost", "bad handle.example"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_bluesky_handle(value)
        entry, cursor = parse_jetstream_event(
            EVENT, expected_did=DID, handle="example.bsky.social"
        )
        self.assertEqual(cursor, EVENT["time_us"])
        self.assertEqual(entry.external_id, "bluesky-post:3examplepost")
        self.assertIn("Release notes", entry.body)
        other, _ = parse_jetstream_event(
            EVENT, expected_did="did:plc:another", handle="example.bsky.social"
        )
        self.assertIsNone(other)

    async def test_first_fetch_sets_now_cursor_without_replaying_or_connecting(self) -> None:
        connected: list[str] = []

        def connector(url: str, **_: object) -> FakeConnection:
            connected.append(url)
            return FakeConnection(FakeWebSocket([]))

        fetcher = BlueskyJetstreamFetcher(
            connector=connector,
            now_provider=lambda: datetime(2026, 8, 11, tzinfo=timezone.utc),
        )
        try:
            result = await fetcher.fetch(
                {"initialized": False, "settings": {"handle": "example.bsky.social"}}
            )
            self.assertEqual(result.feed.entries, ())
            self.assertEqual(result.cursor_value, "1786406400000000")
            self.assertEqual(connected, [])
        finally:
            await fetcher.close()

    async def test_initialized_fetch_resolves_did_and_uses_scoped_cursor(self) -> None:
        connections: list[tuple[str, dict]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params.get("handle"), "example.bsky.social")
            return httpx.Response(200, json={"did": DID})

        def connector(url: str, **kwargs: object) -> FakeConnection:
            connections.append((url, kwargs))
            return FakeConnection(FakeWebSocket([json.dumps(EVENT)]))

        fetcher = BlueskyJetstreamFetcher(
            transport=httpx.MockTransport(handler), connector=connector
        )
        try:
            result = await fetcher.fetch(
                {
                    "initialized": True,
                    "cursor_value": "1786426800000000",
                    "settings": {"handle": "example.bsky.social"},
                }
            )
            self.assertEqual(len(result.feed.entries), 1)
            self.assertEqual(result.cursor_value, str(EVENT["time_us"]))
            self.assertIn("wantedCollections=app.bsky.feed.post", connections[0][0])
            self.assertIn("wantedDids=did%3Aplc%3Aexample123", connections[0][0])
            self.assertIn("cursor=1786426800000000", connections[0][0])
            self.assertEqual(connections[0][1]["max_size"], 256 * 1024)
        finally:
            await fetcher.close()

    async def test_first_poll_persists_stream_cursor_without_history_messages(self) -> None:
        class Fetcher:
            async def fetch(self, _: dict, *, token: str = "") -> FeedFetchResult:
                self.assert_no_token = token
                return FeedFetchResult(False, ParsedFeed(()), None, None, 101, "1786426800000000")

            async def close(self) -> None:
                return None

        class Queue:
            def wake(self) -> None:
                raise AssertionError("baseline must not enqueue analysis")

        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            database = Database(handle.name)
            source = database.create_information_source(
                kind="bluesky",
                name="Trusted account",
                url="https://bsky.app/profile/example.bsky.social",
                settings={"handle": "example.bsky.social"},
                enabled=True,
                poll_interval_minutes=5,
                now=utc_now(),
            )
            database.close()
            poller = BlueskyJetstreamPoller(handle.name, Queue(), fetcher=Fetcher())
            await poller._poll(source)
            database = Database(handle.name, initialize=False)
            current = database.get_information_source(source["id"])
            self.assertTrue(current["initialized"])
            self.assertEqual(current["cursor_value"], "1786426800000000")
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            database.close()
            await poller.close()


if __name__ == "__main__":
    unittest.main()
