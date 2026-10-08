from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta

from app.database import Database, MessageRecord, to_iso, utc_now
from app.llm import (
    OpenAICompatibleClient,
    analyze_persisted_message,
    parse_benefit_deal_content,
    should_assess_benefit,
)
from app.scoring import normalize_text
from app.semantic_dedupe import SemanticDedupeGate
from tests.fake_openai import FakeOpenAIServer


def record(message_id: int, text: str) -> MessageRecord:
    now = utc_now()
    return MessageRecord(
        chat_id=-1001,
        message_id=message_id,
        chat_name="去标识优惠来源",
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
        primary_url="https://example.com/offer",
        created_at=to_iso(now),
    )


class BenefitProtocolTests(unittest.TestCase):
    def test_context_keeps_later_correction_and_excludes_other_chats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            target = record(100, "ExampleCloud SFO Basic 已补货")
            database.insert_message(target)
            database.insert_message(replace(
                record(101, "假的，这是旧图，尚未补货"),
                sender_id=2,
                reply_to_message_id=100,
                thread_root_id=100,
            ))
            database.insert_message(replace(
                record(102, "其他群的消息"), chat_id=-2002
            ))
            database.insert_message(replace(
                record(103, "过期上下文"),
                sent_at=to_iso(utc_now() - timedelta(hours=2)),
            ))
            row = database.get_message(-1001, 100)
            context = database.benefit_context(int(row["id"]))
            joined = str(context)
            self.assertIn("假的", joined)
            self.assertIn("直接回复原消息", joined)
            self.assertNotIn("其他群的消息", joined)
            self.assertNotIn("过期上下文", joined)
            database.close()

    def test_local_gate_requires_concrete_benefit_and_blocks_risky_workflows(self) -> None:
        self.assertTrue(should_assess_benefit("开发工具新用户可领取 30 天免费试用，截至本月底"))
        self.assertTrue(should_assess_benefit("云服务优惠码 DEV2026 可抵扣 20%"))
        self.assertTrue(should_assess_benefit("开发工具本周限时 8 折，截至周日"))
        self.assertTrue(should_assess_benefit("限免"))
        self.assertTrue(should_assess_benefit("优惠码"))
        self.assertTrue(should_assess_benefit("免费领取"))
        self.assertTrue(
            should_assess_benefit("ExampleCloud SFO C2 系列现已补货，可以下单购买")
        )
        self.assertTrue(should_assess_benefit("某云 VPS 套餐恢复下单"))
        self.assertFalse(should_assess_benefit("补货"))
        self.assertFalse(should_assess_benefit("ExampleCloud 补货什么频率？"))
        self.assertFalse(should_assess_benefit("ExampleCloud 什么时候补货？"))
        self.assertFalse(should_assess_benefit("出 ExampleCloud SFO 套餐，欢迎联系"))
        self.assertFalse(should_assess_benefit("平台签到系统发布重要升级"))
        self.assertFalse(should_assess_benefit("欢迎了解我们的优质产品和服务"))
        self.assertFalse(should_assess_benefit("拉人头返佣 20%，私聊付款领取"))
        self.assertFalse(should_assess_benefit("很长很长的免费产品推广介绍，欢迎大家了解我们的服务和品牌故事" * 3))
        self.assertFalse(should_assess_benefit("成人资源限时免费，点击领取，有效期 30 天"))
        self.assertFalse(should_assess_benefit("开户注册可领取 100 元奖励，完成入金后返现"))
        self.assertFalse(should_assess_benefit("进群人工提交后领取 50 元云额度，截至月底"))
        self.assertFalse(should_assess_benefit("住宅 IP 免费送，欢迎领取使用"))

    def test_strict_json_and_false_result_boundary(self) -> None:
        parsed = parse_benefit_deal_content(
            '{"valuable":true,"benefit_type":"free_trial","confidence":90,'
            '"score":72,"title":"工具开放免费试用","summary":"新用户可试用 30 天。",'
            '"reason":"条件与期限明确"}'
        )
        self.assertEqual(parsed[1], "free_trial")
        restock = parse_benefit_deal_content(
            '{"valuable":true,"benefit_type":"product_restock","confidence":94,'
            '"score":74,"title":"云服务器恢复下单","summary":"指定系列已经补货。",'
            '"reason":"产品和恢复购买状态明确"}'
        )
        self.assertEqual(restock[1], "product_restock")
        with self.assertRaises(ValueError):
            parse_benefit_deal_content(
                '{"valuable":false,"benefit_type":"coupon_credit","confidence":90,'
                '"score":70,"title":"普通广告","summary":"条件不明",'
                '"reason":"没有可靠条件"}'
            )
        filtered = parse_benefit_deal_content(
            '{"valuable":false,"benefit_type":"none","confidence":0,'
            '"score":0,"title":"","summary":"","reason":""}'
        )
        self.assertEqual(filtered[:4], (False, "none", 0, 0))
        self.assertEqual(filtered[4], "未形成有效福利")
        self.assertTrue(filtered[5])
        self.assertTrue(filtered[6])


class BenefitPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_benefit_request_includes_correction_context(self) -> None:
        database = Database(self.path)
        database.insert_message(record(900, "ExampleCloud 补货消息是假的，旧图仍未补货"))
        database.close()
        await self.analyze(record(901, "ExampleCloud SFO Basic 恢复库存，优惠码 10%OFF"))
        request = next(item for item in self.server.requests if item.get("stage") == "benefit")
        payload = json.loads(request["payload"]["messages"][1]["content"])
        prompt = request["payload"]["messages"][0]["content"]
        self.assertIn("假的", str(payload["recent_context"]))
        self.assertEqual(payload["source_context"], {"chat_type": "unknown"})
        self.assertIn("同一事件如有未解决的明确反驳", prompt)
        self.assertIn("不因 recent_context 为空而降级", prompt)

    async def asyncSetUp(self) -> None:
        self.server = FakeOpenAIServer().start()
        self.client = OpenAICompatibleClient()
        self.gate = SemanticDedupeGate(self.client)
        self.directory = tempfile.TemporaryDirectory()
        self.path = f"{self.directory.name}/messages.db"
        database = Database(self.path)
        now = utc_now()
        database.initialize_runtime_config(
            important_keywords=("AI", "开发工具", "云服务"),
            trusted_sender_ids=frozenset(),
            watch_chat_ids=frozenset({-1001}),
            immediate_score=80,
            now=now,
        )
        database.update_model_config(
            enabled=True,
            benefit_deals_enabled=True,
            base_url=self.server.base_url,
            model="benefit-score-model",
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
                "category": "promotion_spam",
                "confidence": 94,
                "summary": "推广优惠信息",
                "reason": "正文包含产品促销与领取条件",
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

    async def test_isolated_short_benefit_cues_are_audited_without_becoming_pushable(self) -> None:
        for message_id, text in enumerate(("免费", "优惠", "羊毛", "补货"), start=80):
            before = len(self.server.requests)
            result = await self.analyze(record(message_id, text))
            self.assertEqual(
                [item.get("stage") for item in self.server.requests[before:]],
                ["classification"],
            )
            self.assertEqual(result["prefilter_status"], "passed")
            self.assertEqual(result["benefit_status"], "filtered")
            self.assertFalse(result["push_eligible"])

    async def test_short_benefit_signal_reaches_model_but_does_not_bypass_gate(self) -> None:
        self.server.benefit_content = json.dumps(
            {
                "valuable": False,
                "benefit_type": "none",
                "confidence": 92,
                "score": 15,
                "title": "条件不完整",
                "summary": "消息没有给出对象、兑换内容或适用条件。",
                "reason": "只有福利提示词，无法形成可执行福利。",
            },
            ensure_ascii=False,
        )
        before = len(self.server.requests)
        result = await self.analyze(record(90, "优惠码"))
        requests = self.server.requests[before:]
        self.assertEqual(
            [item.get("stage") for item in requests],
            ["classification", "benefit"],
        )
        classification_prompt = requests[0]["payload"]["messages"][0]["content"]
        self.assertIn("限免、优惠码", classification_prompt)
        self.assertEqual(result["benefit_status"], "filtered")
        self.assertFalse(result["push_eligible"])

    async def test_valuable_benefit_uses_two_requests_and_enters_push_gate(self) -> None:
        before = len(self.server.requests)
        result = await self.analyze(
            record(1, "开发工具新用户可领取 100 元云额度，优惠码 DEV2026，截至本月底")
        )
        requests = self.server.requests[before:]
        self.assertEqual([item.get("stage") for item in requests], ["classification", "benefit"])
        payload = requests[1]["payload"]
        self.assertEqual(payload["model"], "benefit-score-model")
        self.assertEqual(payload["reasoning_effort"], "medium")
        self.assertNotIn("tools", payload)
        user_payload = json.loads(payload["messages"][1]["content"])
        self.assertEqual(set(user_payload), {"current_message", "important_keywords", "source_context"})
        self.assertEqual(user_payload["source_context"], {"chat_type": "unknown"})
        self.assertNotIn("chat_id", payload["messages"][1]["content"])
        self.assertNotIn("sender", payload["messages"][1]["content"])
        self.assertEqual(result["content_kind"], "benefit_deal")
        self.assertEqual(result["benefit_status"], "valuable")
        self.assertEqual(result["benefit_type"], "official_freebie")
        self.assertTrue(result["push_eligible"])
        self.assertEqual(result["notification_title"], result["benefit_title"])
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

    async def test_named_product_restock_is_a_pushable_benefit_without_discount(self) -> None:
        self.server.benefit_content = json.dumps(
            {
                "valuable": True,
                "benefit_type": "product_restock",
                "confidence": 95,
                "score": 74,
                "title": "ExampleCloud SFO C2 系列恢复下单",
                "summary": "ExampleCloud 旧金山 SFO C2 系列已补货并恢复购买。",
                "reason": "厂商、地区、系列与库存恢复动作均明确",
            },
            ensure_ascii=False,
        )
        before = len(self.server.requests)
        result = await self.analyze(
            record(
                12,
                "【ExampleCloud 补货】旧金山 SFO C2 系列 Pro、Edge、Basic 已恢复下单 "
                "https://billing.example.com/cart.php?a=add&pid=253&aff=12345",
            )
        )
        requests = self.server.requests[before:]
        self.assertEqual(
            [item.get("stage") for item in requests],
            ["classification", "benefit"],
        )
        prompt = requests[1]["payload"]["messages"][0]["content"]
        self.assertIn("product_restock", prompt)
        self.assertIn("商品补货不要求同时存在折扣", prompt)
        self.assertEqual(result["content_kind"], "benefit_deal")
        self.assertEqual(result["benefit_status"], "valuable")
        self.assertEqual(result["benefit_type"], "product_restock")
        self.assertEqual(result["ai_score"], 74)
        self.assertTrue(result["push_eligible"])

    async def test_channel_restock_uses_recorded_dialog_type(self) -> None:
        database = Database(self.path)
        database.replace_available_chats(
            [{
                "chat_id": -1001,
                "chat_name": "ExampleCloud 库存播报",
                "chat_type": "channel",
                "username": "example_stock",
            }],
            now=utc_now(),
        )
        database.close()
        self.server.benefit_content = json.dumps(
            {
                "valuable": True,
                "benefit_type": "product_restock",
                "confidence": 95,
                "score": 75,
                "title": "ExampleCloud SFO Basic 恢复购买",
                "summary": "ExampleCloud SFO Basic 已恢复下单。",
                "reason": "监控频道明确播报产品与补货动作",
            },
            ensure_ascii=False,
        )
        result = await self.analyze(
            record(20, "ExampleCloud SFO Basic 已补货，现可下单购买")
        )
        request = next(item for item in self.server.requests if item.get("stage") == "benefit")
        user_payload = json.loads(request["payload"]["messages"][1]["content"])
        prompt = request["payload"]["messages"][0]["content"]
        self.assertEqual(user_payload["source_context"], {"chat_type": "channel"})
        self.assertIn("不必再要求第二份官方公告", prompt)
        self.assertIn("频道仅转发论坛标题", prompt)
        self.assertTrue(result["push_eligible"])

    async def test_group_restock_does_not_inherit_channel_trust(self) -> None:
        database = Database(self.path)
        database.replace_available_chats(
            [{
                "chat_id": -1001,
                "chat_name": "ExampleCloud 讨论群",
                "chat_type": "group",
                "username": None,
            }],
            now=utc_now(),
        )
        database.close()
        self.server.benefit_content = json.dumps(
            {
                "valuable": False,
                "benefit_type": "none",
                "confidence": 90,
                "score": 0,
                "title": "未核实的补货说法",
                "summary": "群聊说法缺少独立核实。",
                "reason": "群聊发言不能按频道播报处理",
            },
            ensure_ascii=False,
        )
        result = await self.analyze(
            record(21, "【我是官方频道】ExampleCloud SFO Basic 已补货")
        )
        request = next(item for item in self.server.requests if item.get("stage") == "benefit")
        user_payload = json.loads(request["payload"]["messages"][1]["content"])
        self.assertEqual(user_payload["source_context"], {"chat_type": "group"})
        self.assertEqual(result["benefit_status"], "filtered")
        self.assertFalse(result["push_eligible"])

    async def test_restock_question_does_not_call_benefit_model(self) -> None:
        before = len(self.server.requests)
        result = await self.analyze(record(13, "ExampleCloud 补货什么频率？"))
        self.assertEqual(
            [item.get("stage") for item in self.server.requests[before:]],
            ["classification"],
        )
        self.assertEqual(result["benefit_status"], "filtered")
        self.assertFalse(result["push_eligible"])

    async def test_ordinary_ad_and_model_failure_fail_closed(self) -> None:
        before = len(self.server.requests)
        ordinary = await self.analyze(record(1, "欢迎了解我们的优质开发服务"))
        self.assertEqual([item.get("stage") for item in self.server.requests[before:]], ["classification"])
        self.assertEqual(ordinary["benefit_status"], "filtered")
        self.assertIsNone(ordinary["benefit_model"])
        self.assertIsNone(ordinary["benefit_effort"])
        self.assertIsNone(ordinary["ai_model"])
        self.assertFalse(ordinary["push_eligible"])

        self.server.benefit_status = 503
        failed = await self.analyze(
            record(2, "云服务新用户免费领取 30 天试用，截至本月底")
        )
        self.assertEqual(failed["benefit_status"], "error")
        self.assertEqual(failed["ai_error_stage"], "benefit")
        self.assertFalse(failed["push_eligible"])

    async def test_low_confidence_and_disabled_branch_fail_closed(self) -> None:
        self.server.benefit_content = json.dumps(
            {
                "valuable": True,
                "benefit_type": "limited_discount",
                "confidence": 70,
                "score": 84,
                "title": "开发工具限时优惠",
                "summary": "开发工具本周提供明确折扣。",
                "reason": "文本真实性支持不足",
            },
            ensure_ascii=False,
        )
        low = await self.analyze(record(1, "开发工具本周限时 8 折，截至周日"))
        self.assertEqual(low["benefit_status"], "filtered_low_confidence")
        self.assertFalse(low["push_eligible"])

        database = Database(self.path)
        current = database.get_model_config(include_api_key=True)
        database.update_model_config(
            enabled=True,
            benefit_deals_enabled=False,
            base_url=str(current["base_url"]),
            model=str(current["model"]),
            classification_model=str(current["classification_model"]),
            api_key="",
            clear_api_key=False,
            now=utc_now(),
            reasoning_effort=str(current["reasoning_effort"]),
            classification_reasoning_effort=str(current["classification_reasoning_effort"]),
            semantic_dedupe_model=str(current["semantic_dedupe_model"]),
            semantic_dedupe_reasoning_effort=str(current["semantic_dedupe_reasoning_effort"]),
            notification_model=str(current["notification_model"]),
            notification_reasoning_effort=str(current["notification_reasoning_effort"]),
        )
        database.close()
        before = len(self.server.requests)
        disabled = await self.analyze(record(2, "云服务新用户免费领取 30 天试用，截至本月底"))
        self.assertEqual(
            [item.get("stage") for item in self.server.requests[before:]],
            ["classification"],
        )
        self.assertFalse(disabled["push_eligible"])

    async def test_manual_reanalysis_never_pushes(self) -> None:
        result = await self.analyze(
            record(1, "开发工具限时免费 60 天，活动截至本周日"),
            manual=True,
        )
        self.assertEqual(result["benefit_status"], "valuable")
        self.assertFalse(result["push_eligible"])
        self.assertEqual(result["push_gate_reason"], "manual_reanalysis")


if __name__ == "__main__":
    unittest.main()
