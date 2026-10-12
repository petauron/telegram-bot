from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace

from telethon.tl import types

from app.database import Database, utc_now
from app.main import MessageProcessor
from app.llm import ModelRuntimeConfig, OpenAICompatibleClient
from app.notification_prepare import deterministic_notification_fallback
from app.source_context import SOURCE_CONTEXT_INSTRUCTIONS, source_context_payload
from app.utils import image_text_unavailable
from tests.fake_openai import FakeOpenAIServer
from tests.test_llm import TEST_SESSION_KEY, sample_record


class SourceContextTests(unittest.TestCase):
    def test_metadata_is_bounded_and_does_not_forward_ids_or_extra_fields(self) -> None:
        context = source_context_payload({
            "name": "  示例\n公告 " * 200, "chat_type": "official",
            "source_type": "telegram", "chat_id": -1001, "sender_id": 1,
            "url": "https://example.com", "api_key": "not-a-real-secret",
            "trusted": True,
        })
        self.assertEqual(set(context), {"name", "chat_type", "source_type"})
        self.assertLessEqual(len(context["name"]), 120)
        self.assertNotIn("\n", context["name"])
        self.assertEqual(context["chat_type"], "unknown")

    def test_recorded_source_is_used_without_granting_trust_to_a_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            item = replace(sample_record(), chat_name="示例服务公告", text="我们停止账号绑定")
            database.insert_message(item)
            database.replace_available_chats([{
                "chat_id": item.chat_id, "chat_name": "名称不能充当官方认证",
                "chat_type": "channel", "username": None,
            }], now=utc_now())
            row = database.get_message(item.chat_id, item.message_id)
            self.assertEqual(database.message_source_context(row["id"]), {
                "name": "示例服务公告", "chat_type": "channel", "source_type": "telegram",
            })
            database.replace_available_chats([], now=utc_now())
            self.assertEqual(database.message_source_context(row["id"])["chat_type"], "channel")
            database.insert_message(replace(
                item, message_id=11, source_type="rss", chat_name="示例资讯源"
            ))
            self.assertEqual(database.message_source_context(
                database.get_message(item.chat_id, 11)["id"]
            )["chat_type"], "unknown")
            database.close()

    def test_image_audit_does_not_flag_captions_stickers_or_link_previews(self) -> None:
        photo = SimpleNamespace(raw_text="", media=types.MessageMediaPhoto())
        self.assertTrue(image_text_unavailable(photo))
        photo.raw_text = "有实际文字的评测"
        self.assertFalse(image_text_unavailable(photo))
        document = SimpleNamespace(mime_type="image/png", attributes=[])
        image = SimpleNamespace(raw_text="", media=types.MessageMediaDocument(document=document))
        self.assertTrue(image_text_unavailable(image))
        document.attributes = [types.DocumentAttributeSticker(alt="", stickerset=types.InputStickerSetEmpty())]
        self.assertFalse(image_text_unavailable(image))
        self.assertFalse(image_text_unavailable(SimpleNamespace(raw_text="", media=None)))
        self.assertFalse(image_text_unavailable(SimpleNamespace(
            raw_text="", media=SimpleNamespace(webpage=SimpleNamespace(photo=object()))
        )))

    def test_failed_digest_preparation_never_reintroduces_unselected_news(self) -> None:
        title, body = deterministic_notification_fallback({
            "ai_summary": "示例工具新增离线部署能力",
            "text": "工具新增离线能力\n消费产品传闻\n返佣广告",
        })
        self.assertEqual(title, "示例工具新增离线部署能力")
        self.assertEqual(body, title)
        self.assertNotIn("传闻", body)
        self.assertNotIn("返佣", body)


class SourceContextClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeOpenAIServer().start()
        self.client = OpenAICompatibleClient()
        self.config = ModelRuntimeConfig(
            enabled=True, base_url=self.server.base_url, api_key=self.server.api_key,
            model="test-score", classification_model="test-classify",
            notification_model="test-notification",
        )
        self.context = {"name": "示例服务公告", "chat_type": "channel", "source_type": "telegram"}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.server.close()

    async def test_all_text_stages_receive_the_same_provenance_and_no_trust_shortcut(self) -> None:
        common = dict(
            config=self.config, sent_at="2099-01-01T00:00:00Z",
            text="我们发布服务政策调整", session_key=TEST_SESSION_KEY,
            source_context=self.context,
        )
        await self.client.classify(**common)
        await self.client.score(**common, category="external_information")
        await self.client.assess_community_insight(**common)
        await self.client.assess_benefit_deal(**common)
        await self.client.prepare_notification(**common, selected_summary="示例服务政策变化")
        requests = [item["payload"] for item in self.server.requests if item["method"] == "POST"]
        self.assertEqual(len(requests), 5)
        for request in requests:
            payload = json.loads(request["messages"][1]["content"])
            self.assertEqual(payload["source_context"], self.context)
            self.assertIn(SOURCE_CONTEXT_INSTRUCTIONS, request["messages"][0]["content"])
            self.assertNotIn("tools", request)
            self.assertNotIn("chat_id", payload)

    async def test_batch_preserves_each_source_and_scoring_anchors_keep_risk_caps(self) -> None:
        await self.client.classify_batch(
            config=self.config, session_key=TEST_SESSION_KEY,
            messages=({
                "message_row_id": 1, "message_id": 10,
                "time": "2099-01-01T00:00:00Z",
                "text": "我们移除账号绑定", "source_context": self.context,
            },),
        )
        payload = json.loads(self.server.requests[-1]["payload"]["messages"][1]["content"])
        self.assertEqual(payload["batch_messages"][0]["source_context"], self.context)
        await self.client.score(
            config=self.config, sent_at="2099-01-01T00:00:00Z",
            text="开源离线音频工具可自部署，支持多轨处理；另有消费产品传闻",
            category="external_information", session_key=TEST_SESSION_KEY,
        )
        prompt = self.server.requests[-1]["payload"]["messages"][0]["content"]
        for phrase in (
            "项目小众、早期版本", "最高价值且独立自洽", "不能用无关消费传闻稀释",
            "不得超过 49 分", "单纯命中关键词绝不能", "点踩且置信度达到 80",
        ):
            self.assertIn(phrase, prompt)


class ImageCollectorAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_captionless_photo_is_recorded_once_but_caption_is_analysable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_runtime_config(
                important_keywords=("网络",), trusted_sender_ids=frozenset(),
                watch_chat_ids=frozenset({-1001}), immediate_score=60, now=now,
            )

            async def get_chat():
                return SimpleNamespace(title="示例公告", username=None)

            async def get_sender():
                return SimpleNamespace(first_name="匿名", last_name=None, username=None)

            event = SimpleNamespace(
                chat_id=-1001, sender_id=1, out=False,
                get_chat=get_chat, get_sender=get_sender,
                message=SimpleNamespace(
                    id=1, raw_text="", file=None, action=None,
                    media=types.MessageMediaPhoto(), reply_to_msg_id=None,
                    date=now, mentioned=False,
                ),
            )
            queue = SimpleNamespace(wake=lambda: None)
            processor = MessageProcessor(
                settings=SimpleNamespace(), database=database,
                analysis_queue=queue, delivery_queue=queue, self_id=999,
            )
            await processor.process(event)
            await processor.process(event)
            row = database.get_message(-1001, 1)
            self.assertTrue(row["media_text_unavailable"])
            self.assertEqual(row["prefilter_reason_code"], "image_text_unavailable")
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0], 0)
            self.assertEqual(database.connection.execute("SELECT recorded_count FROM chat_activity").fetchone()[0], 1)
            event.message.id = 2
            event.message.raw_text = "示例网络服务公告：维护已完成，全部服务恢复正常"
            await processor.process(event)
            self.assertFalse(database.get_message(-1001, 2)["media_text_unavailable"])
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0], 1)
            database.close()
