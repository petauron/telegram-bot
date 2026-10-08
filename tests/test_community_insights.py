from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.database import Database, MessageRecord, to_iso, utc_now
from app.llm import (
    OpenAICompatibleClient,
    analyze_persisted_message,
    parse_community_insight_content,
    should_assess_community,
)
from app.llm_context import (
    COMMUNITY_CONTEXT_CHAR_LIMIT,
    community_has_product_review,
    context_character_count,
    product_subject_keys,
)
from app.scoring import normalize_text
from app.semantic_dedupe import SemanticDedupeGate
from tests.fake_openai import FakeOpenAIServer


def record(
    chat_id: int,
    message_id: int,
    text: str,
    *,
    sent_at: datetime | None = None,
    thread_root_id: int | None = None,
    reply_to_message_id: int | None = None,
    is_service_message: bool = False,
    sender_id: int | None = None,
    sender_name: str = "匿名",
) -> MessageRecord:
    now = sent_at or utc_now()
    return MessageRecord(
        chat_id=chat_id,
        message_id=message_id,
        chat_name="去标识讨论群",
        chat_username=None,
        sender_id=sender_id,
        sender_name=sender_name,
        sent_at=to_iso(now),
        text=text,
        reply_to_message_id=reply_to_message_id,
        thread_root_id=thread_root_id if thread_root_id is not None else message_id,
        base_score=0,
        reasons=(),
        link=None,
        normalized_text=normalize_text(text),
        primary_url=None,
        created_at=to_iso(now),
        is_service_message=is_service_message,
    )


class CommunityInsightPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeOpenAIServer().start()
        self.client = OpenAICompatibleClient()
        self.gate = SemanticDedupeGate(self.client)
        self.directory = tempfile.TemporaryDirectory()
        self.path = f"{self.directory.name}/messages.db"
        database = Database(self.path)
        now = utc_now()
        database.initialize_runtime_config(
            important_keywords=("故障", "云服务", "安全"),
            trusted_sender_ids=frozenset(),
            watch_chat_ids=frozenset({-1001, -2002}),
            immediate_score=80,
            now=now,
        )
        database.update_model_config(
            enabled=True,
            community_insights_enabled=True,
            base_url=self.server.base_url,
            model="community-score-model",
            classification_model="classification-model",
            api_key=self.server.api_key,
            clear_api_key=False,
            now=now,
            reasoning_effort="medium",
            semantic_dedupe_model="dedupe-model",
            semantic_dedupe_reasoning_effort="low",
            notification_model="notification-model",
            notification_reasoning_effort="low",
        )
        database.close()
        self.server.classification_content = json.dumps(
            {
                "category": "discussion",
                "confidence": 92,
                "summary": "群内技术讨论",
                "reason": "消息是用户之间的观察和交流",
            },
            ensure_ascii=False,
        )

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.server.close()
        self.directory.cleanup()

    async def analyze(self, item: MessageRecord, *, manual: bool = False) -> dict:
        database = Database(self.path)
        database.insert_message(item)
        result = await analyze_persisted_message(
            database=database,
            client=self.client,
            row=database.get_message(item.chat_id, item.message_id),
            now=utc_now(),
            manual=manual,
            semantic_gate=self.gate,
        )
        database.close()
        assert result is not None
        return result

    async def test_valuable_discussion_enters_existing_dedupe_and_push_gate(self) -> None:
        self.server.community_content = json.dumps(
            {
                "valuable": True,
                "signal_type": "incident_report",
                "confidence": 90,
                "score": 82,
                "title": "社区反馈某服务连接异常",
                "summary": "讨论中出现具体连接失败现象，具有排障参考价值。",
                "reason": "消息包含明确对象、现象和实际影响",
                "evidence_count": 1,
            },
            ensure_ascii=False,
        )
        before = len(self.server.requests)
        result = await self.analyze(
            record(-1001, 1, "实测云服务连接持续失败，切换网络后仍能复现错误码 502")
        )
        requests = self.server.requests[before:]
        self.assertEqual([item.get("stage") for item in requests], ["classification", "community"])
        community_request = requests[1]["payload"]
        self.assertEqual(community_request["model"], "community-score-model")
        self.assertEqual(community_request["reasoning_effort"], "medium")
        self.assertNotIn("tools", community_request)
        user_payload = json.loads(community_request["messages"][1]["content"])
        self.assertEqual(
            set(user_payload),
            {
                "recent_context",
                "conversation_evidence",
                "current_message",
                "important_keywords",
            },
        )
        self.assertNotIn("chat_id", community_request["messages"][1]["content"])
        self.assertNotIn("sender", community_request["messages"][1]["content"])
        self.assertNotIn("participant_ids", community_request["messages"][1]["content"])
        self.assertEqual(result["content_kind"], "community_signal")
        self.assertEqual(result["community_status"], "valuable")
        self.assertEqual(result["community_signal_type"], "incident_report")
        self.assertTrue(result["push_eligible"])
        self.assertEqual(result["notification_prepare_status"], "success")
        self.assertEqual(result["notification_title"], result["community_title"])
        database = Database(self.path)
        database.initialize_push_config(
            telegram_bot_token="test-token",
            telegram_chat_id="123456",
            now=utc_now(),
        )
        self.assertEqual(
            database.enqueue_immediate_deliveries(
                -1001,
                1,
                now=utc_now(),
            ),
            1,
        )
        database.close()

    async def test_obvious_chat_stops_locally_without_second_request(self) -> None:
        before = len(self.server.requests)
        result = await self.analyze(record(-1001, 1, "大家怎么看呀"))
        self.assertEqual(
            [item.get("stage") for item in self.server.requests[before:]],
            ["classification"],
        )
        self.assertEqual(result["community_status"], "filtered")
        self.assertEqual(result["ai_status"], "filtered_non_information")
        self.assertEqual(result["community_evidence_count"], 0)
        self.assertIsNone(result["community_model"])
        self.assertIsNone(result["community_effort"])
        self.assertIsNone(result["ai_model"])
        self.assertFalse(result["push_eligible"])

    async def test_isolated_subjectless_short_outage_is_kept_but_cannot_push(self) -> None:
        before = len(self.server.requests)
        result = await self.analyze(record(-1001, 1, "炸了", sender_id=101))
        self.assertEqual(
            [item.get("stage") for item in self.server.requests[before:]],
            ["classification"],
        )
        self.assertEqual(result["prefilter_status"], "passed")
        self.assertEqual(result["community_status"], "filtered")
        self.assertFalse(result["push_eligible"])

    async def test_low_confidence_and_model_error_fail_closed(self) -> None:
        self.server.community_content = json.dumps(
            {
                "valuable": True,
                "signal_type": "verified_observation",
                "confidence": 70,
                "score": 82,
                "title": "测试发现行为变化",
                "summary": "文本给出了测试结果，但支持程度不足。",
                "reason": "缺少足够上下文",
                "evidence_count": 1,
            },
            ensure_ascii=False,
        )
        low = await self.analyze(record(-1001, 1, "实测某工具升级后出现兼容问题，当前只有一次结果"))
        self.assertEqual(low["community_status"], "filtered_low_confidence")
        self.assertFalse(low["push_eligible"])

        self.server.community_status = 503
        failed = await self.analyze(record(-1001, 2, "实测另一云服务持续连接失败并返回错误码 504"))
        self.assertEqual(failed["community_status"], "error")
        self.assertEqual(failed["ai_error_stage"], "community")
        self.assertFalse(failed["push_eligible"])

    async def test_context_is_prior_same_chat_bounded_and_manual_never_pushes(self) -> None:
        self.server.community_content = json.dumps(
            {
                "valuable": False,
                "signal_type": "none",
                "confidence": 90,
                "score": 20,
                "title": "普通讨论",
                "summary": "尚未形成明确结论。",
                "reason": "只有单条观察",
                "evidence_count": 1,
            },
            ensure_ascii=False,
        )
        await self.analyze(record(-1001, 1, "测试某服务时遇到连接失败，需要更多结果确认"))
        await self.analyze(record(-2002, 1, "另一个群正在讨论完全不同的版本兼容问题"))
        self.server.community_content = json.dumps(
            {
                "valuable": True,
                "signal_type": "technical_solution",
                "confidence": 90,
                "score": 72,
                "title": "连接异常已有可复现解决方法",
                "summary": "讨论给出了配置调整步骤，并明确说明调整后连接恢复。",
                "reason": "同一话题包含问题、步骤和结果",
                "evidence_count": 2,
            },
            ensure_ascii=False,
        )
        before = len(self.server.requests)
        result = await self.analyze(
            record(-1001, 2, "调整 DNS 配置后连接已经恢复，重复测试三次均正常"),
            manual=True,
        )
        request = next(
            item for item in self.server.requests[before:] if item.get("stage") == "community"
        )
        payload = json.loads(request["payload"]["messages"][1]["content"])
        self.assertEqual(len(payload["recent_context"]), 1)
        self.assertIn("需要更多结果确认", payload["recent_context"][0]["text"])
        self.assertNotIn("另一个群", request["payload"]["messages"][1]["content"])
        self.assertEqual(result["community_status"], "valuable")
        self.assertFalse(result["push_eligible"])
        self.assertEqual(result["push_gate_reason"], "manual_reanalysis")

    async def test_all_thirty_selected_context_messages_reach_model_payload(self) -> None:
        target_time = utc_now()
        database = Database(self.path)
        for index in range(20):
            database.insert_message(
                record(
                    -1001,
                    100 + index,
                    f"量子网络故障线程观察 {index}",
                    sent_at=target_time - timedelta(minutes=100 - index),
                    thread_root_id=9000,
                )
            )
        for index in range(10):
            database.insert_message(
                record(
                    -1001,
                    200 + index,
                    f"量子网络故障相关补充 {index}",
                    sent_at=target_time - timedelta(hours=4, minutes=-index),
                    thread_root_id=200 + index,
                )
            )
        database.connection.execute(
            """
            UPDATE messages
            SET prefilter_status = 'passed', ai_category = 'discussion',
                community_status = 'filtered', ai_status = 'filtered_non_information'
            WHERE chat_id = -1001
            """
        )
        database.connection.commit()
        database.close()
        self.server.community_content = json.dumps(
            {
                "valuable": False,
                "signal_type": "none",
                "confidence": 90,
                "score": 30,
                "title": "证据仍待确认",
                "summary": "讨论尚未形成可推送结论。",
                "reason": "当前消息没有明确状态变化",
                "evidence_count": 1,
            },
            ensure_ascii=False,
        )
        before = len(self.server.requests)
        await self.analyze(
            record(
                -1001,
                500,
                "量子网络故障是否已经恢复",
                sent_at=target_time,
                thread_root_id=9000,
                reply_to_message_id=119,
            )
        )
        request = next(
            item for item in self.server.requests[before:] if item.get("stage") == "community"
        )
        payload = json.loads(request["payload"]["messages"][1]["content"])
        self.assertEqual(len(payload["recent_context"]), 30)

    async def test_multi_participant_short_outage_runs_model_with_aggregate_evidence(self) -> None:
        target_time = utc_now()
        database = Database(self.path)
        for message_id, minutes, text, sender_id in (
            (600, 4, "WorkBuddy 官网无法访问，客户端持续连接超时", 101),
            (601, 2, "我这里也打不开", 202),
        ):
            database.insert_message(
                record(
                    -1001,
                    message_id,
                    text,
                    sent_at=target_time - timedelta(minutes=minutes),
                    sender_id=sender_id,
                )
            )
        database.connection.execute(
            """
            UPDATE messages
            SET prefilter_status = 'passed', ai_category = 'discussion',
                community_status = 'filtered', ai_status = 'filtered_non_information'
            WHERE chat_id = -1001 AND message_id IN (600, 601)
            """
        )
        database.connection.commit()
        database.close()
        self.server.community_content = json.dumps(
            {
                "valuable": True,
                "signal_type": "incident_report",
                "confidence": 90,
                "score": 82,
                "title": "社区多人反馈服务异常",
                "summary": "多条同群消息指向同一服务无法访问。",
                "reason": "群聊上下文给出同一主体和多方佐证",
                "evidence_count": 3,
            },
            ensure_ascii=False,
        )
        before = len(self.server.requests)
        result = await self.analyze(
            record(-1001, 602, "又炸了", sent_at=target_time, sender_id=303)
        )
        requests = self.server.requests[before:]
        self.assertEqual([item.get("stage") for item in requests], ["classification", "community"])
        payload = json.loads(requests[1]["payload"]["messages"][1]["content"])
        evidence = payload["conversation_evidence"]
        self.assertTrue(evidence["multi_participant_incident"])
        self.assertEqual(evidence["distinct_participant_count"], 3)
        self.assertIn("WorkBuddy", json.dumps(payload["recent_context"], ensure_ascii=False))
        self.assertTrue(
            all(set(item) == {"time", "text"} for item in payload["recent_context"])
        )
        self.assertNotIn("sender", requests[1]["payload"]["messages"][1]["content"])
        self.assertTrue(result["push_eligible"])

    async def test_product_reputation_requires_two_participants_and_pushes_at_sixty(self) -> None:
        target_time = utc_now()
        first = await self.analyze(
            record(
                -1001,
                700,
                "我用了 GPT-5.6 一周，编程质量更好，但响应速度偏慢",
                sent_at=target_time - timedelta(minutes=12),
                thread_root_id=700,
                sender_id=101,
            )
        )
        self.assertEqual(first["community_status"], "filtered")
        self.assertEqual(
            first["community_reason"],
            "已记录产品评价，但同一产品尚未形成至少两位独立参与者的具体口碑证据",
        )

        self.server.community_content = json.dumps(
            {
                "valuable": True,
                "signal_type": "product_review",
                "confidence": 91,
                "score": 72,
                "title": "GPT-5.6 编程质量提升但速度评价有分歧",
                "summary": "两位参与者认可代码质量改善，同时提到响应速度存在差异。",
                "reason": "同一模型有两位独立参与者提供具体使用体验",
                "evidence_count": 2,
            },
            ensure_ascii=False,
        )
        before = len(self.server.requests)
        result = await self.analyze(
            record(
                -1001,
                701,
                "我也在用 GPT-5.6，代码效果确实更强，不过高峰期延迟明显",
                sent_at=target_time,
                thread_root_id=700,
                reply_to_message_id=700,
                sender_id=202,
            )
        )
        requests = self.server.requests[before:]
        self.assertEqual(
            [item.get("stage") for item in requests],
            ["classification", "community"],
        )
        model_input = json.loads(requests[1]["payload"]["messages"][1]["content"])
        evidence = model_input["conversation_evidence"]
        self.assertTrue(evidence["multi_participant_product_review"])
        self.assertEqual(evidence["product_review_participant_count"], 2)
        self.assertGreaterEqual(evidence["product_review_dimension_count"], 2)
        self.assertNotIn("sender", requests[1]["payload"]["messages"][1]["content"])
        self.assertEqual(result["content_kind"], "community_signal")
        self.assertEqual(result["community_signal_type"], "product_review")
        self.assertEqual(result["ai_score"], 72)
        self.assertTrue(result["push_eligible"])

        database = Database(self.path)
        database.initialize_push_config(
            telegram_bot_token="test-token",
            telegram_chat_id="123456",
            now=utc_now(),
        )
        self.assertEqual(
            database.enqueue_immediate_deliveries(
                -1001,
                701,
                now=utc_now(),
            ),
            1,
        )
        delivery = database.claim_next_delivery_unit(now=utc_now())
        self.assertEqual(delivery["delivery_type"], "immediate")
        self.assertEqual([row["id"] for row in delivery["rows"]], [result["id"]])
        database.close()

    async def test_single_review_and_bare_recommendation_question_stop_locally(self) -> None:
        before = len(self.server.requests)
        single = await self.analyze(
            record(
                -1001,
                710,
                "我用了 AlphaVPS 一个月，价格偏贵，客服响应慢，不推荐续费",
                sender_id=101,
            )
        )
        self.assertEqual(
            [item.get("stage") for item in self.server.requests[before:]],
            ["classification"],
        )
        self.assertEqual(single["community_status"], "filtered")
        self.assertIn("两位独立参与者", single["community_reason"])
        self.assertFalse(single["push_eligible"])
        self.assertFalse(community_has_product_review("求推荐哪家 VPS 延迟低又稳定？"))

    async def test_product_review_protocol_rejects_single_evidence_and_out_of_band_score(self) -> None:
        valid = json.dumps(
            {
                "valuable": True,
                "signal_type": "product_review",
                "confidence": 90,
                "score": 79,
                "title": "产品体验存在明确优缺点",
                "summary": "两位使用者给出具体且可核对的体验。",
                "reason": "包含两条独立使用证据",
                "evidence_count": 2,
            },
            ensure_ascii=False,
        )
        parsed = parse_community_insight_content(valid, evidence_limit=2)
        self.assertEqual(parsed[1], "product_review")
        for evidence_count, score in ((1, 72), (2, 80)):
            invalid = json.loads(valid)
            invalid["evidence_count"] = evidence_count
            invalid["score"] = score
            with self.subTest(evidence_count=evidence_count, score=score), self.assertRaises(ValueError):
                parse_community_insight_content(
                    json.dumps(invalid, ensure_ascii=False),
                    evidence_limit=2,
                )


class CommunityContextSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = f"{self.directory.name}/messages.db"
        self.database = Database(self.path)
        self.database.initialize_runtime_config(
            important_keywords=("故障", "恢复", "安全"),
            trusted_sender_ids=frozenset(),
            watch_chat_ids=frozenset({-1001, -2002}),
            immediate_score=80,
            now=utc_now(),
        )

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def add_history(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        sent_at: datetime,
        thread_root_id: int,
        category: str = "discussion",
        community_status: str = "filtered",
        service: bool = False,
        prefilter_status: str = "passed",
        sender_id: int | None = None,
        sender_name: str = "匿名",
    ) -> None:
        self.database.insert_message(
            record(
                chat_id,
                message_id,
                text,
                sent_at=sent_at,
                thread_root_id=thread_root_id,
                is_service_message=service,
                sender_id=sender_id,
                sender_name=sender_name,
            )
        )
        self.database.connection.execute(
            """
            UPDATE messages
            SET prefilter_status = ?, ai_category = ?, community_status = ?,
                ai_status = 'filtered_non_information'
            WHERE chat_id = ? AND message_id = ?
            """,
            (prefilter_status, category, community_status, chat_id, message_id),
        )
        self.database.connection.commit()

    def test_thread_recent_and_related_tiers_are_bounded_and_isolated(self) -> None:
        target_time = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
        target_thread = 9000
        for index in range(10):
            self.add_history(
                chat_id=-1001,
                message_id=100 + index,
                text=f"线程内排障步骤 {index}",
                sent_at=target_time - timedelta(minutes=90 - index),
                thread_root_id=target_thread,
            )
        for index in range(10):
            self.add_history(
                chat_id=-1001,
                message_id=200 + index,
                text=f"近期独立讨论 {index}",
                sent_at=target_time - timedelta(minutes=40 - index),
                thread_root_id=200 + index,
            )
        for index in range(10):
            self.add_history(
                chat_id=-1001,
                message_id=300 + index,
                text=f"量子网络故障相关观察 {index}",
                sent_at=target_time - timedelta(hours=5, minutes=-index),
                thread_root_id=300 + index,
            )
        self.add_history(
            chat_id=-1001,
            message_id=401,
            text="线程中过期但不相关的普通寒暄",
            sent_at=target_time - timedelta(hours=2, seconds=1),
            thread_root_id=target_thread,
        )
        self.add_history(
            chat_id=-2002,
            message_id=402,
            text="另一个群的量子网络故障相关观察",
            sent_at=target_time - timedelta(minutes=5),
            thread_root_id=target_thread,
        )
        self.add_history(
            chat_id=-1001,
            message_id=403,
            text="检测到违规消息已删除并封禁",
            sent_at=target_time - timedelta(minutes=4),
            thread_root_id=403,
            service=True,
        )
        self.add_history(
            chat_id=-1001,
            message_id=404,
            text="量子网络产品推广优惠",
            sent_at=target_time - timedelta(minutes=3),
            thread_root_id=404,
            category="promotion_spam",
        )
        target = record(
            -1001,
            500,
            "量子网络故障是否已经恢复",
            sent_at=target_time,
            thread_root_id=target_thread,
            reply_to_message_id=109,
        )
        self.database.insert_message(target)
        row = self.database.get_message(-1001, 500)
        assert row is not None

        context = self.database.recent_community_context(int(row["id"]))
        texts = [item["text"] for item in context]
        self.assertEqual(len(context), 30)
        self.assertEqual([item["time"] for item in context], sorted(item["time"] for item in context))
        self.assertTrue(all(f"线程内排障步骤 {index}" in texts for index in range(10)))
        self.assertTrue(all(f"近期独立讨论 {index}" in texts for index in range(10)))
        self.assertTrue(all(f"量子网络故障相关观察 {index}" in texts for index in range(10)))
        self.assertNotIn("线程中过期但不相关的普通寒暄", texts)
        self.assertFalse(any("另一个群" in text for text in texts))
        self.assertFalse(any("违规消息" in text for text in texts))
        self.assertFalse(any("推广优惠" in text for text in texts))
        self.assertFalse(any("是否已经恢复" in text for text in texts))

    def test_character_limit_is_hard_and_utf8_text_remains_valid(self) -> None:
        target_time = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
        for index in range(30):
            self.add_history(
                chat_id=-1001,
                message_id=600 + index,
                text=f"量子网络故障观察 {index} " + "测" * 900,
                sent_at=target_time - timedelta(minutes=30, seconds=-index),
                thread_root_id=9000,
            )
        self.database.insert_message(
            record(
                -1001,
                700,
                "量子网络故障是否恢复",
                sent_at=target_time,
                thread_root_id=9000,
                reply_to_message_id=629,
            )
        )
        row = self.database.get_message(-1001, 700)
        assert row is not None
        context = self.database.recent_community_context(int(row["id"]))
        self.assertLessEqual(len(context), 30)
        self.assertLessEqual(context_character_count(context), COMMUNITY_CONTEXT_CHAR_LIMIT)
        self.assertTrue(all(item["text"].encode("utf-8").decode("utf-8") for item in context))

    def test_multi_participant_group_outage_uses_context_without_exposing_identity(self) -> None:
        target_time = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
        self.add_history(
            chat_id=-1001,
            message_id=800,
            text="WorkBuddy 官网无法访问，客户端连接持续超时",
            sent_at=target_time - timedelta(minutes=4),
            thread_root_id=800,
            sender_id=101,
        )
        self.add_history(
            chat_id=-1001,
            message_id=801,
            text="我这里也打不开",
            sent_at=target_time - timedelta(minutes=2),
            thread_root_id=801,
            sender_id=202,
        )
        self.database.insert_message(
            record(
                -1001,
                802,
                "又炸了",
                sent_at=target_time,
                thread_root_id=802,
                sender_id=303,
            )
        )
        row = self.database.get_message(-1001, 802)
        assert row is not None
        context, evidence = self.database.community_analysis_context(int(row["id"]))
        self.assertTrue(evidence.multi_participant_incident)
        self.assertEqual(evidence.distinct_participant_count, 3)
        self.assertTrue(evidence.subject_anchor_present)
        self.assertTrue(should_assess_community("又炸了", context, evidence))
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertIn("WorkBuddy", serialized)
        self.assertTrue(all(set(item) == {"time", "text"} for item in context))

    def test_subjectless_outage_guess_and_unrelated_group_chat_stay_local(self) -> None:
        target_time = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
        self.add_history(
            chat_id=-1001,
            message_id=900,
            text="今晚吃什么",
            sent_at=target_time - timedelta(minutes=4),
            thread_root_id=900,
            sender_id=101,
        )
        self.add_history(
            chat_id=-1001,
            message_id=901,
            text="我也不知道",
            sent_at=target_time - timedelta(minutes=2),
            thread_root_id=901,
            sender_id=202,
        )
        self.database.insert_message(
            record(
                -1001,
                902,
                "估计炸了",
                sent_at=target_time,
                thread_root_id=902,
                sender_id=303,
            )
        )
        row = self.database.get_message(-1001, 902)
        assert row is not None
        context, evidence = self.database.community_analysis_context(int(row["id"]))
        self.assertFalse(evidence.subject_anchor_present)
        self.assertFalse(evidence.multi_participant_incident)
        self.assertFalse(should_assess_community("估计炸了", context, evidence))

    def test_same_thread_subject_supports_short_status_but_length_alone_does_not(self) -> None:
        target_time = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
        self.add_history(
            chat_id=-1001,
            message_id=950,
            text="量子云服务客户端无法连接",
            sent_at=target_time - timedelta(minutes=3),
            thread_root_id=950,
            sender_id=101,
        )
        self.database.insert_message(
            record(
                -1001,
                951,
                "炸了",
                sent_at=target_time,
                thread_root_id=950,
                reply_to_message_id=950,
                sender_id=101,
            )
        )
        row = self.database.get_message(-1001, 951)
        assert row is not None
        context, evidence = self.database.community_analysis_context(int(row["id"]))
        self.assertGreaterEqual(evidence.same_thread_support_count, 1)
        self.assertTrue(should_assess_community("炸了", context, evidence))
        self.assertFalse(
            should_assess_community(
                "这是一段超过二十四个字但只是在谈论周末安排和晚餐选择的普通闲聊内容",
                (),
            )
        )

    def test_broad_words_need_evidence_while_direct_status_is_retained(self) -> None:
        for text in (
            "这个版本可用吗",
            "这个配置怎么解决",
            "有人确认一下风险吗",
            "哪里可以查看操作步骤",
        ):
            with self.subTest(text=text):
                self.assertFalse(should_assess_community(text, ()))

        self.assertTrue(should_assess_community("WorkBuddy 服务已恢复正常", ()))
        self.assertTrue(
            should_assess_community(
                "实测 DevTool v3.2 升级后 API 返回错误码 401，重复三次结果一致",
                (),
            )
        )
        self.assertTrue(should_assess_community("修改 DNS 配置后已解决问题", ()))

    def test_product_review_evidence_is_same_product_multi_participant_and_time_bounded(self) -> None:
        target_time = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)
        self.add_history(
            chat_id=-1001,
            message_id=1000,
            text="我用了 AlphaVPS 一个月，晚高峰丢包较多，不推荐续费",
            sent_at=target_time - timedelta(minutes=30),
            thread_root_id=1000,
            sender_id=101,
        )
        self.add_history(
            chat_id=-1001,
            message_id=1001,
            text="我用过 BetaVPS，客服响应很慢，价格也偏贵",
            sent_at=target_time - timedelta(minutes=20),
            thread_root_id=1001,
            sender_id=202,
        )
        self.database.insert_message(
            record(
                -1001,
                1002,
                "我也用了 AlphaVPS，线路速度不错，但工单处理比较慢",
                sent_at=target_time,
                thread_root_id=1002,
                sender_id=303,
            )
        )
        row = self.database.get_message(-1001, 1002)
        assert row is not None
        _, evidence = self.database.community_analysis_context(int(row["id"]))
        self.assertTrue(evidence.multi_participant_product_review)
        self.assertEqual(evidence.product_review_message_count, 2)
        self.assertEqual(evidence.product_review_participant_count, 2)
        self.assertIn("name:alphavps", product_subject_keys(row["text"]))

        self.database.insert_message(
            record(
                -1001,
                1003,
                "我用过 DeltaVPS，速度和稳定性都一般，不推荐",
                sent_at=target_time + timedelta(minutes=1),
                thread_root_id=1003,
                sender_id=101,
            )
        )
        different = self.database.get_message(-1001, 1003)
        assert different is not None
        _, different_evidence = self.database.community_analysis_context(int(different["id"]))
        self.assertFalse(different_evidence.multi_participant_product_review)

        self.database.insert_message(
            record(
                -1001,
                1006,
                "我再补充 BetaVPS 的价格也偏贵，客服响应还是慢",
                sent_at=target_time + timedelta(minutes=2),
                thread_root_id=1001,
                sender_id=202,
            )
        )
        repeated_author = self.database.get_message(-1001, 1006)
        assert repeated_author is not None
        _, repeated_author_evidence = self.database.community_analysis_context(
            int(repeated_author["id"])
        )
        self.assertFalse(repeated_author_evidence.multi_participant_product_review)
        self.assertEqual(repeated_author_evidence.product_review_participant_count, 1)

        self.add_history(
            chat_id=-1001,
            message_id=1004,
            text="我用了 GammaVPS，速度很好而且价格便宜",
            sent_at=target_time - timedelta(hours=2, seconds=1),
            thread_root_id=1005,
            sender_id=404,
        )
        self.database.insert_message(
            record(
                -1001,
                1005,
                "我也用了 GammaVPS，线路稳定，性价比不错",
                sent_at=target_time,
                thread_root_id=1005,
                sender_id=505,
            )
        )
        expired = self.database.get_message(-1001, 1005)
        assert expired is not None
        _, expired_evidence = self.database.community_analysis_context(int(expired["id"]))
        self.assertFalse(expired_evidence.multi_participant_product_review)


if __name__ == "__main__":
    unittest.main()
