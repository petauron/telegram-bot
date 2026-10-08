from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta

import httpx

from app.database import Database, MessageRecord, to_iso, utc_now
from app.ntfy_feedback import (
    NtfyFeedbackCollector,
    build_feedback_actions,
    build_feedback_token,
    parse_feedback_token,
)
from app.push import NtfyPusher, PushDispatcher, build_ntfy_notification


class NtfyFeedbackProtocolTests(unittest.TestCase):
    def test_signed_tokens_are_bounded_and_tamper_evident(self) -> None:
        now = utc_now()
        key = "ab" * 32
        expires_at = int((now + timedelta(hours=1)).timestamp())
        token = build_feedback_token(
            signing_key=key,
            public_id="opaque_event_target_2026",
            vote="up",
            expires_at=expires_at,
        )
        parsed = parse_feedback_token(token, signing_key=key, now=now)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.vote, "up")
        self.assertIsNone(
            parse_feedback_token(
                token.replace(".up.", ".down."),
                signing_key=key,
                now=now,
            )
        )
        self.assertIsNone(
            parse_feedback_token(
                token,
                signing_key=key,
                now=now + timedelta(hours=2),
            )
        )

    def test_actions_only_contain_opaque_signed_feedback(self) -> None:
        actions = build_feedback_actions(
            base_url="https://push.example.test",
            feedback_topic="feedback-random",
            signing_key="cd" * 32,
            public_id="opaque_event_target_2026",
            expires_at=2_000_000_000,
        )
        self.assertEqual([item["label"] for item in actions], ["👍 有用", "👎 无用"])
        for action in actions:
            self.assertEqual(action["action"], "http")
            self.assertEqual(action["method"], "POST")
            self.assertEqual(action["url"], "https://push.example.test/feedback-random")
            self.assertEqual(
                action["headers"],
                {"Content-Type": "text/plain; charset=utf-8"},
            )
            self.assertFalse(action["clear"])
            serialized = json.dumps(action, ensure_ascii=False)
            for hidden in ("chat_id", "message_id", "Telegram", "t.me", "token="):
                self.assertNotIn(hidden, serialized)


class NtfyFeedbackDatabaseTests(unittest.TestCase):
    @staticmethod
    def _message(now):
        return MessageRecord(
            chat_id=-1001,
            message_id=7,
            chat_name="匿名来源",
            chat_username=None,
            sender_id=None,
            sender_name="匿名",
            sent_at=to_iso(now),
            text="去标识化资讯内容",
            reply_to_message_id=None,
            thread_root_id=7,
            base_score=80,
            reasons=("测试",),
            link=None,
            normalized_text="去标识化资讯内容",
            primary_url=None,
            created_at=to_iso(now),
        )

    def test_migration_is_idempotent_secrets_are_private_and_vote_is_latest(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            now = utc_now()
            database = Database(handle.name)
            database.initialize_push_config(
                telegram_bot_token="",
                telegram_chat_id="",
                now=now,
            )
            public = database.get_push_config()
            private = database.get_push_config(include_secrets=True)
            self.assertNotIn("feedback_topic", public["ntfy"])
            self.assertNotIn("feedback_signing_key", public["ntfy"])
            self.assertTrue(private["ntfy"]["feedback_topic"])
            self.assertEqual(len(private["ntfy"]["feedback_signing_key"]), 64)
            topic = private["ntfy"]["feedback_topic"]
            signing_key = private["ntfy"]["feedback_signing_key"]

            self.assertTrue(database.insert_message(self._message(now)))
            row = database.get_message(-1001, 7)
            target = database.prepare_ntfy_feedback_target(row["id"], now=now)
            self.assertEqual(
                target,
                database.prepare_ntfy_feedback_target(row["id"], now=now),
            )
            self.assertTrue(
                database.record_ntfy_feedback(
                    event_id="event-up",
                    public_id=target["public_id"],
                    vote="up",
                    expires_at=target["expires_at"],
                    event_time=int(now.timestamp()),
                    now=now,
                )
            )
            self.assertFalse(
                database.record_ntfy_feedback(
                    event_id="event-up",
                    public_id=target["public_id"],
                    vote="up",
                    expires_at=target["expires_at"],
                    event_time=int(now.timestamp()),
                    now=now,
                )
            )
            later = now + timedelta(seconds=1)
            self.assertTrue(
                database.record_ntfy_feedback(
                    event_id="event-down",
                    public_id=target["public_id"],
                    vote="down",
                    expires_at=target["expires_at"],
                    event_time=int(later.timestamp()),
                    now=later,
                )
            )
            self.assertEqual(database.get_ntfy_feedback(row["id"])["vote"], "down")
            database.close()

            reopened = Database(handle.name)
            migrated = reopened.get_push_config(include_secrets=True)
            self.assertEqual(migrated["ntfy"]["feedback_topic"], topic)
            self.assertEqual(migrated["ntfy"]["feedback_signing_key"], signing_key)
            self.assertEqual(reopened.get_ntfy_feedback(row["id"])["vote"], "down")
            reopened.close()

    def test_legacy_push_config_migration_generates_private_feedback_config(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            connection = sqlite3.connect(handle.name)
            connection.executescript(
                """
                CREATE TABLE push_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    telegram_enabled INTEGER NOT NULL DEFAULT 0,
                    telegram_bot_token TEXT,
                    telegram_chat_id TEXT,
                    ntfy_enabled INTEGER NOT NULL DEFAULT 0,
                    ntfy_base_url TEXT NOT NULL DEFAULT 'https://ntfy.example.com',
                    ntfy_topic TEXT,
                    ntfy_access_token TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO push_config(
                    id, telegram_enabled, ntfy_enabled, ntfy_base_url,
                    ntfy_topic, updated_at
                ) VALUES(
                    1, 0, 0, 'https://ntfy.example.com',
                    'legacy-news', '2026-01-01T00:00:00Z'
                );
                """
            )
            connection.close()

            database = Database(handle.name)
            first = database.get_push_config(include_secrets=True)["ntfy"]
            database.close()
            reopened = Database(handle.name)
            second = reopened.get_push_config(include_secrets=True)["ntfy"]
            self.assertEqual(first["feedback_topic"], second["feedback_topic"])
            self.assertEqual(
                first["feedback_signing_key"],
                second["feedback_signing_key"],
            )
            self.assertEqual(second["community_topic"], "legacy-news")
            self.assertEqual(second["benefit_topic"], "legacy-news")
            self.assertNotIn("feedback_topic", reopened.get_push_config()["ntfy"])
            reopened.close()

    def test_dispatcher_reuses_feedback_actions_for_delivery_retry(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            now = utc_now()
            database = Database(handle.name)
            database.initialize_push_config(
                telegram_bot_token="",
                telegram_chat_id="",
                now=now,
            )
            database.update_push_config(
                telegram_enabled=False,
                telegram_bot_token="",
                clear_telegram_bot_token=False,
                telegram_chat_id="",
                ntfy_enabled=True,
                ntfy_base_url="https://push.example.test",
                ntfy_topic="news",
                ntfy_access_token="",
                clear_ntfy_access_token=False,
                now=now,
            )
            self.assertTrue(database.insert_message(self._message(now)))
            row = database.get_message(-1001, 7)
            dispatcher = PushDispatcher(database)
            first = dispatcher._with_ntfy_feedback((row,))[0]
            second = dispatcher._with_ntfy_feedback((row,))[0]
            self.assertEqual(
                first["_ntfy_feedback_actions"],
                second["_ntfy_feedback_actions"],
            )
            self.assertEqual(len(first["_ntfy_feedback_actions"]), 2)
            database.close()


class NtfyFeedbackCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_collector_records_signed_vote_and_replay_is_idempotent(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            now = utc_now()
            database = Database(handle.name)
            database.initialize_push_config(
                telegram_bot_token="",
                telegram_chat_id="",
                now=now,
            )
            database.update_push_config(
                telegram_enabled=False,
                telegram_bot_token="",
                clear_telegram_bot_token=False,
                telegram_chat_id="",
                ntfy_enabled=True,
                ntfy_base_url="https://push.example.test",
                ntfy_topic="news",
                ntfy_access_token="read-token",
                clear_ntfy_access_token=False,
                now=now,
            )
            self.assertTrue(database.insert_message(NtfyFeedbackDatabaseTests._message(now)))
            row = database.get_message(-1001, 7)
            target = database.prepare_ntfy_feedback_target(row["id"], now=now)
            private = database.get_push_config(include_secrets=True)
            feedback_topic = private["ntfy"]["feedback_topic"]
            body = build_feedback_token(
                signing_key=private["ntfy"]["feedback_signing_key"],
                public_id=target["public_id"],
                vote="up",
                expires_at=target["expires_at"],
            )
            database.close()

            async def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, f"/{feedback_topic}/json")
                self.assertEqual(request.headers["authorization"], "Bearer read-token")
                event = {
                    "id": "feedback-event-1",
                    "time": int(now.timestamp()),
                    "event": "message",
                    "topic": feedback_topic,
                    "message": body,
                }
                return httpx.Response(200, text=json.dumps(event) + "\n")

            collector = NtfyFeedbackCollector(
                handle.name,
                transport=httpx.MockTransport(handler),
            )
            try:
                self.assertTrue(await collector.poll_once())
                self.assertFalse(await collector.poll_once())
            finally:
                await collector.close()

            database = Database(handle.name, initialize=False)
            self.assertEqual(database.get_ntfy_feedback(row["id"])["vote"], "up")
            runtime = database.get_ntfy_feedback_runtime()
            self.assertEqual(runtime["cursor_value"], "feedback-event-1")
            self.assertIsNone(runtime["last_error_category"])
            database.close()

    async def test_ntfy_json_notification_contains_actions_without_credentials(self) -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "published"})

        actions = build_feedback_actions(
            base_url="https://push.example.test",
            feedback_topic="feedback-random",
            signing_key="ef" * 32,
            public_id="opaque_event_target_2026",
            expires_at=2_000_000_000,
        )
        row = {
            "chat_name": "匿名来源",
            "text": "一条去标识化资讯",
            "ai_status": "success",
            "ai_score": 82,
            "ai_summary": "资讯摘要",
            "notification_title": "资讯标题",
            "notification_body": "资讯正文",
            "content_kind": "news",
            "_ntfy_feedback_actions": actions,
        }
        pusher = NtfyPusher(
            "https://push.example.test",
            "news",
            "server-push-token",
            transport=httpx.MockTransport(handler),
        )
        try:
            self.assertTrue(await pusher.send_notification(build_ntfy_notification(row)))
        finally:
            await pusher.close()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].url.path, "/")
        payload = json.loads(captured[0].content)
        self.assertEqual(payload["topic"], "news")
        self.assertEqual(payload["priority"], 4)
        self.assertEqual([item["label"] for item in payload["actions"]], ["👍 有用", "👎 无用"])
        self.assertNotIn("click", payload)
        self.assertNotIn("tags", payload)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("server-push-token", serialized)
        self.assertNotIn("chat_id", serialized)
        self.assertNotIn("message_id", serialized)


if __name__ == "__main__":
    unittest.main()
