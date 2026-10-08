from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta

from app.database import Database, MessageRecord, to_iso, utc_now
from app.feedback import (
    FeedbackGuidance,
    build_feedback_guidance,
    feedback_interest_tags,
    feedback_source_key,
)
from app.llm import (
    ModelRuntimeConfig,
    OpenAICompatibleClient,
    _feedback_preferences_for_message,
)
from app.scoring import normalize_text
from tests.fake_openai import FakeOpenAIServer


def message_record(message_id: int, now, *, chat_id: int = -1001) -> MessageRecord:
    text = f"AI 安全产品更新 {message_id}"
    return MessageRecord(
        chat_id=chat_id,
        message_id=message_id,
        chat_name="去标识来源",
        chat_username=None,
        sender_id=None,
        sender_name="匿名",
        sent_at=to_iso(now),
        text=text,
        reply_to_message_id=None,
        thread_root_id=message_id,
        base_score=0,
        reasons=(),
        link=None,
        normalized_text=normalize_text(text),
        primary_url=None,
        created_at=to_iso(now),
    )


class FeedbackAggregationTests(unittest.TestCase):
    def test_source_keys_are_stable_irreversible_and_tags_are_bounded(self) -> None:
        first = feedback_source_key(
            source_type="telegram", source_id=None, chat_id=-1001234567890
        )
        second = feedback_source_key(
            source_type="telegram", source_id=None, chat_id=-1001234567890
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotIn("1001234567890", first)
        self.assertEqual(
            feedback_interest_tags("AI 平台发布安全更新", ("AI", "安全", "财经")),
            ("AI", "安全"),
        )

    def test_guidance_uses_latest_vote_aggregates_and_minimum_sample_gate(self) -> None:
        records = (
            {"public_id": "a", "vote": "up", "title": "产品甲发布更新", "content_kind": "news", "source_key": "s1", "interest_tags_json": '["AI"]'},
            {"public_id": "b", "vote": "down", "title": "产品乙停止服务", "content_kind": "news", "source_key": "s2", "interest_tags_json": '["安全"]'},
            {"public_id": "c", "vote": "up", "title": "产品丙限时免费", "content_kind": "benefit_deal", "source_key": "s1", "interest_tags_json": '["云"]'},
        )
        guidance = build_feedback_guidance(
            records,
            content_kind="news",
            source_key="s1",
            current_tags=("AI",),
        )
        self.assertEqual(guidance.sample_count, 3)
        self.assertEqual(
            guidance.payload["topic_preferences"],
            (
                [
                    {"id": "p1", "vote": "up", "topic": "产品甲发布更新"},
                    {"id": "p2", "vote": "down", "topic": "产品乙停止服务"},
                    {"id": "p3", "vote": "up", "topic": "产品丙限时免费"},
                ]
            ),
        )
        self.assertNotIn("content_kind", guidance.payload)
        self.assertNotIn("same_source", guidance.payload)
        self.assertNotIn("matched_interests", guidance.payload)

        class StubDatabase:
            def __init__(self, value):
                self.value = value
                self.recorded = False

            def feedback_guidance(self, *_args, **_kwargs):
                return self.value

            def record_feedback_context(self, *_args, **_kwargs):
                self.recorded = True

        immature = StubDatabase(FeedbackGuidance(0, "没有样本", {"secret": "not-used"}))
        self.assertIsNone(
            _feedback_preferences_for_message(
                immature, row_id=1, content_kind="news", now=utc_now()
            )
        )
        self.assertTrue(immature.recorded)
        mature = StubDatabase(
            FeedbackGuidance(
                1,
                "单条具体主题反馈",
                {
                    "window_days": 90,
                    "topic_preferences": [
                        {"id": "p1", "vote": "down", "topic": "产品乙停止服务"}
                    ],
                },
            )
        )
        self.assertEqual(
            _feedback_preferences_for_message(
                mature, row_id=1, content_kind="news", now=utc_now()
            ),
            mature.value.payload,
        )

    def test_database_migration_dashboard_and_feedback_context_are_idempotent(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            now = utc_now()
            database = Database(handle.name)
            database.initialize_runtime_config(
                important_keywords=("AI", "安全"),
                trusted_sender_ids=frozenset(),
                watch_chat_ids=frozenset({-1001}),
                immediate_score=80,
                now=now,
            )
            database.initialize_push_config(
                telegram_bot_token="", telegram_chat_id="", now=now
            )
            for index, vote in enumerate(("up", "down", "up"), start=1):
                recorded_at = now + timedelta(seconds=index)
                self.assertTrue(database.insert_message(message_record(index, recorded_at)))
                row = database.get_message(-1001, index)
                with database.connection:
                    database.connection.execute(
                        """
                        UPDATE messages
                        SET ai_status = 'success', ai_category = 'external_information',
                            content_kind = 'news', ai_score = 70,
                            notification_title = ?, ai_summary = ?
                        WHERE id = ?
                        """,
                        (f"去标识标题 {index}", "AI 安全更新", int(row["id"])),
                    )
                target = database.prepare_ntfy_feedback_target(int(row["id"]), now=recorded_at)
                self.assertTrue(
                    database.record_ntfy_feedback(
                        event_id=f"event-{index}",
                        public_id=target["public_id"],
                        vote=vote,
                        expires_at=target["expires_at"],
                        event_time=int(recorded_at.timestamp()),
                        now=recorded_at,
                    )
                )

            current_time = now + timedelta(minutes=1)
            self.assertTrue(database.insert_message(message_record(99, current_time)))
            current = database.get_message(-1001, 99)
            guidance = database.feedback_guidance(
                int(current["id"]), content_kind="news", now=current_time
            )
            self.assertEqual(guidance.sample_count, 3)
            database.record_feedback_context(int(current["id"]), guidance=guidance, now=current_time)
            audited = database.get_message_by_id(int(current["id"]))
            self.assertEqual(audited["feedback_context_sample_count"], 3)
            self.assertIsNotNone(audited["feedback_context_applied_at"])

            dashboard = database.feedback_dashboard(hours=2160, now=current_time)
            self.assertEqual(dashboard["summary"]["total"], 3)
            self.assertEqual(dashboard["summary"]["up"], 2)
            self.assertEqual(dashboard["summary"]["down"], 1)
            self.assertTrue(dashboard["items"])
            serialized = json.dumps(dashboard, ensure_ascii=False)
            self.assertNotIn("public_id", serialized)
            self.assertNotIn("source_key", serialized)
            old_time = now - timedelta(days=91)
            with database.connection:
                database.connection.execute(
                    """
                    INSERT INTO feedback_records(
                        public_id, vote, voted_at, title, source_name,
                        content_kind, source_key, interest_tags_json,
                        created_at, updated_at
                    ) VALUES('expired-record', 'up', ?, '过期记录', '匿名来源',
                             'news', 'expired', '[]', ?, ?)
                    """,
                    (to_iso(old_time), to_iso(old_time), to_iso(old_time)),
                )
            database.cleanup(3, current_time)
            self.assertIsNone(
                database.connection.execute(
                    "SELECT 1 FROM feedback_records WHERE public_id = 'expired-record'"
                ).fetchone()
            )
            database.close()

            reopened = Database(handle.name)
            columns = {
                row["name"]
                for row in reopened.connection.execute("PRAGMA table_info(messages)")
            }
            self.assertIn("feedback_context_sample_count", columns)
            self.assertIsNotNone(
                reopened.connection.execute(
                    "SELECT 1 FROM feedback_records LIMIT 1"
                ).fetchone()
            )
            reopened.close()


class FeedbackModelPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeOpenAIServer().start()
        self.client = OpenAICompatibleClient()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.server.close()

    async def test_scoring_receives_only_bounded_aggregate_feedback(self) -> None:
        config = ModelRuntimeConfig(
            enabled=True,
            base_url=self.server.base_url,
            api_key=self.server.api_key,
            model="test-model-a",
        )
        self.server.scoring_content = json.dumps(
            {
                "score": 88,
                "summary": "产品乙发布功能更新",
                "reason": "消息包含具体产品变化",
                "feedback_match_id": "p1",
                "feedback_match_vote": "down",
                "feedback_match_confidence": 95,
            },
            ensure_ascii=False,
        )
        result = await self.client.score(
            config=config,
            sent_at="2026-08-12T00:00:00Z",
            text="平台发布安全更新",
            category="external_information",
            session_key="tgchat-v1-" + "a" * 64,
            feedback_preferences={
                "window_days": 90,
                "topic_preferences": [
                    {"id": "private-id", "vote": "down", "topic": "产品乙停止服务"},
                    {"id": "other-id", "vote": "up", "topic": "产品甲发布新版本"},
                ],
                "source_key": "不得发送",
            },
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.score, 59)
        self.assertEqual(result.feedback_match_vote, "down")
        self.assertIn("已限制为不推送", result.reason)
        request = self.server.requests[-1]["payload"]
        self.assertNotIn("tools", request)
        user_payload = json.loads(request["messages"][1]["content"])
        preference = user_payload["feedback_preferences"]
        self.assertEqual(preference["topic_sample_count"], 2)
        self.assertEqual(preference["topic_preferences"][0], {
            "id": "p1", "vote": "down", "topic": "产品乙停止服务"
        })
        serialized = json.dumps(preference, ensure_ascii=False)
        self.assertNotIn("source_key", serialized)
        self.assertNotIn("不得发送", serialized)

        self.server.requests.clear()
        self.server.scoring_content = json.dumps(
            {
                "score": 76,
                "summary": "产品甲发布功能更新",
                "reason": "这是另一个具体产品",
                "feedback_match_id": None,
                "feedback_match_vote": "none",
                "feedback_match_confidence": 0,
            },
            ensure_ascii=False,
        )
        unrelated = await self.client.score(
            config=config,
            sent_at="2026-08-12T00:00:00Z",
            text="同一家公司推出产品甲的重要能力",
            category="external_information",
            session_key="tgchat-v1-" + "a" * 64,
            feedback_preferences={
                "window_days": 90,
                "topic_preferences": [
                    {"id": "hidden", "vote": "down", "topic": "产品乙停止服务"}
                ],
            },
        )
        self.assertEqual(unrelated.score, 76)
        self.assertIsNone(unrelated.feedback_match_vote)

        self.server.requests.clear()
        await self.client.score(
            config=config,
            sent_at="2026-08-12T00:00:00Z",
            text="平台发布安全更新",
            category="external_information",
            session_key="tgchat-v1-" + "a" * 64,
        )
        no_feedback = json.loads(
            self.server.requests[-1]["payload"]["messages"][1]["content"]
        )
        self.assertNotIn("feedback_preferences", no_feedback)


if __name__ == "__main__":
    unittest.main()
