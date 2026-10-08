from __future__ import annotations

import json
import tempfile
import unittest
from email.header import decode_header, make_header
from unittest.mock import patch

import httpx

from app.database import Database, utc_now
from app.push import (
    BotPusher,
    NtfyPusher,
    PushDispatcher,
    _truncate_utf8,
    build_ntfy_notification,
    immediate_chunks,
    ntfy_priority,
    ntfy_title,
)
from app.push_config import validate_ntfy_base_url, validate_ntfy_topic


class PushConfigTests(unittest.TestCase):
    def test_env_bootstrap_is_one_time_and_secrets_are_not_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            now = utc_now()
            database.initialize_push_config(
                telegram_bot_token="first-token",
                telegram_chat_id="123456",
                now=now,
            )
            public = database.get_push_config()
            self.assertTrue(public["telegram"]["enabled"])
            self.assertTrue(public["telegram"]["bot_token_configured"])
            self.assertNotIn("bot_token", public["telegram"])
            self.assertNotIn("access_token", public["ntfy"])
            self.assertNotIn("feedback_topic", public["ntfy"])
            self.assertNotIn("feedback_signing_key", public["ntfy"])
            self.assertTrue(public["ntfy"]["feedback"]["topic"])
            self.assertEqual(public["ntfy"]["base_url"], "https://ntfy.example.com")

            saved = database.update_push_config(
                telegram_enabled=False,
                telegram_bot_token="",
                clear_telegram_bot_token=False,
                telegram_chat_id="123456",
                ntfy_enabled=True,
                ntfy_base_url="https://ntfy.example.com/",
                ntfy_topic="priority-news",
                ntfy_community_topic="priority-community",
                ntfy_benefit_topic="priority-benefits",
                ntfy_access_token="ntfy-secret",
                clear_ntfy_access_token=False,
                now=now,
            )
            self.assertFalse(saved["telegram"]["enabled"])
            self.assertTrue(saved["ntfy"]["enabled"])
            self.assertTrue(saved["ntfy"]["access_token_configured"])
            self.assertEqual(saved["ntfy"]["topic"], "priority-news")
            self.assertEqual(
                saved["ntfy"]["community_topic"], "priority-community"
            )
            self.assertEqual(saved["ntfy"]["benefit_topic"], "priority-benefits")
            self.assertTrue(saved["ntfy"]["feedback"]["enabled"])
            self.assertNotIn("ntfy-secret", json.dumps(saved))

            database.initialize_push_config(
                telegram_bot_token="replacement-must-not-win",
                telegram_chat_id="999999",
                now=now,
            )
            private = database.get_push_config(include_secrets=True)
            self.assertFalse(private["telegram"]["enabled"])
            self.assertEqual(private["telegram"]["bot_token"], "first-token")
            self.assertEqual(private["telegram"]["chat_id"], "123456")
            self.assertEqual(
                public["ntfy"]["feedback"]["topic"],
                private["ntfy"]["feedback_topic"],
            )
            database.close()

            reopened = Database(path)
            self.assertEqual(reopened.get_push_config()["ntfy"]["topic"], "priority-news")
            self.assertEqual(
                reopened.get_push_config()["ntfy"]["community_topic"],
                "priority-community",
            )
            self.assertEqual(
                reopened.get_push_config()["ntfy"]["benefit_topic"],
                "priority-benefits",
            )
            self.assertEqual(
                sum(
                    row["name"] == "ntfy_base_url"
                    for row in reopened.connection.execute("PRAGMA table_info(push_config)")
                ),
                1,
            )
            reopened.close()

    def test_validation_is_strict_without_rejecting_supported_server_paths(self) -> None:
        self.assertEqual(
            validate_ntfy_base_url("https://PUSH.EXAMPLE.test/ntfy/"),
            "https://push.example.test/ntfy",
        )
        self.assertEqual(validate_ntfy_topic("news_2026-prod"), "news_2026-prod")
        for unsafe in (
            "file:///tmp/ntfy.sock",
            "https://user:secret@example.test",
            "https://example.test/path?token=secret",
            "https://example.test/#fragment",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                validate_ntfy_base_url(unsafe)
        for invalid in ("", "has space", "topic/path", "主题"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_ntfy_topic(invalid, required=True)


class PushClientTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def row(**overrides):
        value = {
            "chat_id": -1001,
            "message_id": 7,
            "chat_name": "安全资讯群",
            "sender_name": "发布者",
            "text": "产品发布安全升级，修复 CVE-2026-12345。详情见公告。",
            "reply_count": 2,
            "link": "https://t.me/c/1/7",
            "ai_status": "success",
            "ai_score": 92,
            "ai_summary": "高危漏洞修复已发布，建议尽快升级",
            "ai_reason": "影响明确且具有较强时效性",
            "ai_category": "external_information",
            "ai_category_label": "外部资讯",
            "ai_response_text": "模型原始响应绝不能进入通知",
            "ai_category_response_text": "分类原始响应绝不能进入通知",
            "notification_prepare_status": "success",
            "notification_title": "高危漏洞修复已发布，建议尽快升级",
            "notification_body": "产品发布安全升级，修复 CVE-2026-12345。\n\n受影响用户建议尽快更新。",
            "notification_prepare_response_text": "整理原始响应绝不能进入通知",
        }
        value.update(overrides)
        return value

    async def test_telegram_and_ntfy_use_channel_specific_payloads(self) -> None:
        captured: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handle)
        telegram = BotPusher(
            "bot-test-token",
            "654321",
            api_base_url="https://telegram.test",
            transport=transport,
        )
        ntfy = NtfyPusher(
            "https://ntfy.example.com",
            "priority-news",
            "ntfy-test-token",
            transport=transport,
        )
        html_message = '<b>重点资讯</b>\n内容 &amp; 更新\n<a href="https://t.me/c/1/7">查看原消息</a>'
        try:
            self.assertTrue(await telegram.send_chunks((html_message,)))
            self.assertTrue(await ntfy.send_rows((self.row(),)))
        finally:
            await telegram.close()
            await ntfy.close()

        self.assertEqual(captured[0].url.path, "/botbot-test-token/sendMessage")
        telegram_payload = json.loads(captured[0].content)
        self.assertEqual(telegram_payload["chat_id"], "654321")
        self.assertEqual(telegram_payload["parse_mode"], "HTML")
        self.assertEqual(telegram_payload["text"], html_message)
        self.assertEqual(captured[1].url.path, "/priority-news")
        self.assertEqual(captured[1].headers["authorization"], "Bearer ntfy-test-token")
        decoded_title = str(make_header(decode_header(captured[1].headers["x-title"])))
        self.assertEqual(decoded_title, "🟥92｜新闻｜高危漏洞修复已发布，建议尽快升级")
        self.assertEqual(captured[1].headers["x-priority"], "max")
        self.assertNotIn("x-tags", captured[1].headers)
        self.assertEqual(captured[1].headers["x-markdown"], "yes")
        self.assertNotIn("x-click", captured[1].headers)
        ntfy_text = captured[1].content.decode("utf-8")
        self.assertIn("**来源**\n安全资讯群", ntfy_text)
        self.assertIn("**资讯摘要**\n- 产品发布安全升级", ntfy_text)
        self.assertIn("产品发布安全升级", ntfy_text)
        self.assertIn("\n- 受影响用户", ntfy_text)
        for hidden in (
            "AI：",
            "评分",
            "分类",
            "理由",
            "回复",
            "发布者",
            "模型原始响应绝不能进入通知",
            "分类原始响应绝不能进入通知",
            "整理原始响应绝不能进入通知",
            "https://t.me/c/1/7",
            "Telegram 原消息",
        ):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, ntfy_text)
        self.assertEqual(ntfy_text.count("高危漏洞修复已发布，建议尽快升级"), 0)

    def test_title_prefers_prepared_title_then_falls_back_to_ai_and_source(self) -> None:
        self.assertEqual(
            ntfy_title(self.row(notification_title="客户精华标题")),
            "🟥92｜新闻｜客户精华标题",
        )
        self.assertEqual(
            ntfy_title(self.row(notification_title="", text="原文标题", ai_summary="AI 提炼后的核心变化")),
            "🟥92｜新闻｜AI 提炼后的核心变化",
        )
        self.assertEqual(
            ntfy_title(
                self.row(
                    ai_status="error",
                    notification_title="",
                    ai_summary="不应使用",
                    text="平台发布重要升级。后续说明不应进入标题\n第二段",
                )
            ),
            "🟥92｜新闻｜平台发布重要升级。",
        )
        self.assertEqual(
            ntfy_title(self.row(ai_status="error", notification_title="", ai_summary="", text="https://example.test/a")),
            "🟥92｜新闻｜重要资讯提醒",
        )

    def test_title_is_control_safe_and_truncated_on_utf8_boundary(self) -> None:
        title = ntfy_title(
            self.row(notification_title="安全更新\r\nX-Evil: injected " + "中文" * 100)
        )
        self.assertNotIn("\r", title)
        self.assertNotIn("\n", title)
        self.assertTrue(title.startswith("🟥92｜新闻｜"))
        self.assertLessEqual(len(title.encode("utf-8")), 120)
        self.assertEqual(title.encode("utf-8").decode("utf-8"), title)
        self.assertLessEqual(len(_truncate_utf8("中" * 100, 17).encode("utf-8")), 17)

    def test_priority_boundaries_and_title_markers_are_program_controlled(self) -> None:
        expected = {
            0: ("low", "⬜0｜新闻｜"),
            59: ("low", "⬜59｜新闻｜"),
            60: ("default", "🟩60｜新闻｜"),
            69: ("default", "🟩69｜新闻｜"),
            70: ("default", "🟨70｜新闻｜"),
            79: ("default", "🟨79｜新闻｜"),
            80: ("high", "🟧80｜新闻｜"),
            89: ("high", "🟧89｜新闻｜"),
            90: ("max", "🟥90｜新闻｜"),
            100: ("max", "🟥100｜新闻｜"),
        }
        for score, (priority, prefix) in expected.items():
            with self.subTest(score=score):
                self.assertEqual(ntfy_priority(self.row(ai_score=score)), priority)
                self.assertTrue(ntfy_title(self.row(ai_score=score)).startswith(prefix))

    def test_community_signal_keeps_priority_first_and_is_customer_visible(self) -> None:
        row = self.row(
            content_kind="community_signal",
            ai_score=82,
            notification_title="某服务出现连接异常",
            notification_body="多条讨论给出了相同故障现象和复现条件。",
        )
        notification = build_ntfy_notification(row)
        self.assertTrue(notification.title.startswith("🟧82｜讨论｜"))
        self.assertIn("**讨论结论**", notification.body)
        self.assertNotIn("评分", notification.body)
        telegram = immediate_chunks(row)[0]
        self.assertIn("社区线索即时提醒", telegram)
        self.assertIn("【社区线索】", telegram)
        self.assertIn("<b>讨论结论</b>", telegram)

    def test_product_reputation_has_distinct_customer_label_without_audit_fields(self) -> None:
        row = self.row(
            content_kind="community_signal",
            community_signal_type="product_review",
            ai_score=72,
            notification_title="某产品稳定性较好但售后评价有分歧",
            notification_body="多位使用者认可日常稳定性，同时对工单响应速度有不同体验。",
            community_reason="内部审计理由不得进入通知",
            community_response_text="模型原始反响不得进入通知",
        )
        notification = build_ntfy_notification(row)
        self.assertTrue(notification.title.startswith("🟨72｜口碑｜"))
        self.assertIn("**口碑摘要**", notification.body)
        self.assertIn("多位使用者", notification.body)
        self.assertNotIn("内部审计", notification.body)
        self.assertNotIn("模型原始", notification.body)
        telegram = immediate_chunks(row)[0]
        self.assertIn("产品口碑提醒", telegram)
        self.assertIn("【产品口碑】", telegram)
        self.assertIn("<b>口碑摘要</b>", telegram)

    def test_benefit_deal_keeps_priority_first_and_is_customer_visible(self) -> None:
        row = self.row(
            ai_score=82,
            content_kind="benefit_deal",
            notification_title="开发工具开放限时免费额度",
            notification_body="新用户可领取免费额度，活动条件和期限明确。",
        )
        notification = build_ntfy_notification(row)
        self.assertTrue(notification.title.startswith("🟧82｜福利｜"))
        self.assertIn("**优惠详情**", notification.body)
        telegram = immediate_chunks(row)[0]
        self.assertIn("福利羊毛即时提醒", telegram)
        self.assertIn("【福利羊毛】", telegram)
        self.assertIn("<b>优惠详情</b>", telegram)

    def test_single_line_customer_body_is_split_into_readable_facts(self) -> None:
        row = self.row(
            notification_body=(
                "平台上线新能力，支持批量处理。"
                "新版本降低延迟；现有用户无需迁移。"
            ),
        )
        notification = build_ntfy_notification(row)
        self.assertIn("**资讯摘要**", notification.body)
        self.assertIn("- 平台上线新能力，支持批量处理。", notification.body)
        self.assertIn("\n- 新版本降低延迟；", notification.body)
        self.assertIn("\n- 现有用户无需迁移。", notification.body)
        self.assertNotIn("。新版本", notification.body)

        telegram = immediate_chunks(row)[0]
        self.assertIn("<b>资讯摘要</b>", telegram)
        self.assertIn("• 平台上线新能力，支持批量处理。", telegram)
        self.assertIn("\n• 新版本降低延迟；", telegram)
        self.assertIn("\n• 现有用户无需迁移。", telegram)

    def test_readable_body_caps_structural_items_without_losing_the_remainder(self) -> None:
        body = "".join(f"第{index}项事实。" for index in range(1, 12))
        notification = build_ntfy_notification(self.row(notification_body=body))
        detail = notification.body.split("**资讯摘要**\n", 1)[1]
        facts = detail.splitlines()
        self.assertEqual(len(facts), 8)
        self.assertTrue(all(item.startswith("- ") for item in facts))
        self.assertIn("第11项事实。", facts[-1])

    def test_customer_body_is_escaped_and_excludes_audit_and_platform_link(self) -> None:
        notification = build_ntfy_notification(
            self.row(
                notification_body="**伪粗体** [恶意结构]\n\n第二段 <script>内容</script>",
                chat_name="# 伪标题",
                ai_reason="`伪代码` > 注入",
                link="https://t.me/public_channel/7",
            )
        )
        self.assertIn(r"\*\*伪粗体\*\*", notification.body)
        self.assertIn(r"\[恶意结构\]", notification.body)
        self.assertIn("\n- " + r"第二段 \<script\>内容\</script\>", notification.body)
        self.assertIn(r"**来源**" + "\n" + r"\# 伪标题", notification.body)
        for hidden in (
            "伪代码",
            "发布者",
            "外部资讯",
            "AI",
            "评分",
            "分类",
            "理由",
            "回复",
            "模型原始响应绝不能进入通知",
            "分类原始响应绝不能进入通知",
            "https://t.me/public_channel/7",
            "Telegram",
        ):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, notification.body)
        self.assertFalse(hasattr(notification, "click"))

    def test_ntfy_body_byte_limit_never_breaks_markdown_escaping(self) -> None:
        notification = build_ntfy_notification(
            self.row(
                notification_body=("[复杂]*正文*\\路径\n\n" * 700),
                chat_name="#" * 400,
                primary_url="https://example.test/release?q=stable",
            )
        )
        self.assertLessEqual(len(notification.body.encode("utf-8")), 3_500)
        self.assertFalse(notification.body.endswith("\\"))
        self.assertIn("\n\n", notification.body)
        self.assertIn(
            "[查看原文](https://example.test/release?q=stable)",
            notification.body,
        )

    def test_multiline_rendering_and_external_link_are_context_safe(self) -> None:
        row = self.row(
            notification_title="平台发布安全更新",
            notification_body="第一段 <b>原文</b>\n\n\n第二段 [说明] \\ 路径",
            primary_url=(
                "https://example.test/release_(x)[1]\\note"
                "?q=a(b)[c]\\d&utm_source=tracking"
            ),
        )
        notification = build_ntfy_notification(row)
        self.assertIn(r"第一段 \<b\>原文\</b\>", notification.body)
        self.assertIn("\n- 第二段", notification.body)
        self.assertNotIn("\n\n\n", notification.body)
        self.assertIn(r"\[说明\] \\ 路径", notification.body)
        safe_url = (
            "https://example.test/release_%28x%29%5B1%5D%5Cnote"
            "?q=a%28b%29%5Bc%5D%5Cd"
        )
        self.assertIn(f"[查看原文]({safe_url})", notification.body)
        self.assertNotIn("utm_source", notification.body)

        telegram = immediate_chunks(row)[0]
        self.assertIn("• 第一段 &lt;b&gt;原文&lt;/b&gt;\n• 第二段", telegram)
        self.assertIn(f'href="{safe_url}"', telegram)
        self.assertNotIn("https://t.me/", telegram)

        unsafe = build_ntfy_notification(
            self.row(primary_url="https://example.test/path\x01broken")
        )
        self.assertNotIn("查看原文", unsafe.body)

        closing_bracket_url = build_ntfy_notification(
            self.row(primary_url="https://example.test/docs/[stable]")
        )
        self.assertIn(
            "[查看原文](https://example.test/docs/%5Bstable%5D)",
            closing_bracket_url.body,
        )
        malformed_authority = build_ntfy_notification(
            self.row(primary_url=r"https://example.test\\attacker.test/path")
        )
        self.assertNotIn("查看原文", malformed_authority.body)

    def test_affiliate_parameters_are_removed_without_dropping_official_product_link(self) -> None:
        row = self.row(
            content_kind="benefit_deal",
            notification_title="云服务器套餐恢复下单",
            notification_body="指定地区与系列已经补货。",
            primary_url=(
                "https://www.dmit.io/cart.php?a=add&pid=253&aff=12345"
                "&affiliate_id=partner&utm_source=campaign"
            ),
        )
        notification = build_ntfy_notification(row)
        safe_url = "https://www.dmit.io/cart.php?a=add&pid=253"
        self.assertIn(f"[查看原文]({safe_url})", notification.body)
        self.assertNotIn("aff=", notification.body)
        self.assertNotIn("affiliate_id", notification.body)
        self.assertNotIn("utm_source", notification.body)
        telegram = immediate_chunks(row)[0]
        self.assertIn(
            'href="https://www.dmit.io/cart.php?a=add&amp;pid=253"',
            telegram,
        )
        self.assertNotIn("affiliate_id", telegram)

    async def test_ntfy_digest_sends_one_structured_request_per_row(self) -> None:
        captured: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        channel = NtfyPusher(
            "https://ntfy.example.com",
            "priority-news",
            None,
            transport=httpx.MockTransport(handle),
        )
        try:
            self.assertTrue(
                await channel.send_rows(
                    (
                        self.row(message_id=1, notification_title="第一条资讯精华"),
                        self.row(message_id=2, notification_title="第二条资讯精华", link=None),
                    )
                )
            )
        finally:
            await channel.close()
        self.assertEqual(len(captured), 2)
        titles = [
            str(make_header(decode_header(request.headers["x-title"])))
            for request in captured
        ]
        self.assertEqual(
            titles,
            ["🟥92｜新闻｜第一条资讯精华", "🟥92｜新闻｜第二条资讯精华"],
        )
        for request in captured:
            self.assertNotIn("x-tags", request.headers)
            self.assertNotIn("x-click", request.headers)
            self.assertNotIn("https://t.me/", request.content.decode("utf-8"))

    async def test_ntfy_routes_each_content_kind_to_its_configured_topic(self) -> None:
        captured: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        channel = NtfyPusher(
            "https://ntfy.example.com",
            "radar-news",
            None,
            content_topics={
                "news": "radar-news",
                "community_signal": "radar-community",
                "benefit_deal": "radar-benefits",
            },
            transport=httpx.MockTransport(handle),
        )
        try:
            self.assertTrue(
                await channel.send_rows(
                    (
                        self.row(message_id=21, content_kind="news"),
                        self.row(message_id=22, content_kind="community_signal"),
                        self.row(message_id=23, content_kind="benefit_deal"),
                    )
                )
            )
        finally:
            await channel.close()
        self.assertEqual(
            [request.url.path for request in captured],
            ["/radar-news", "/radar-community", "/radar-benefits"],
        )

    async def test_ntfy_test_notification_uses_safe_native_headers(self) -> None:
        captured: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        channel = NtfyPusher(
            "https://ntfy.example.com",
            "priority-news",
            None,
            transport=httpx.MockTransport(handle),
        )
        try:
            self.assertTrue(await channel.send_test())
        finally:
            await channel.close()
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(
            str(make_header(decode_header(request.headers["x-title"]))),
            "🟡 测试｜群讯雷达渠道可用",
        )
        self.assertEqual(request.headers["x-priority"], "default")
        self.assertNotIn("x-tags", request.headers)
        self.assertEqual(request.headers["x-markdown"], "yes")
        self.assertNotIn("x-click", request.headers)
        self.assertNotIn("点击", request.content.decode("utf-8"))

    async def test_dispatcher_reloads_saved_channels_and_succeeds_if_one_channel_sends(self) -> None:
        class FakeBot:
            instances: list["FakeBot"] = []

            def __init__(self, *args, **kwargs) -> None:
                self.args = args
                self.closed = False
                self.sent: list[tuple[str, ...]] = []
                self.instances.append(self)

            async def close(self) -> None:
                self.closed = True

            async def send_chunks(self, chunks) -> bool:
                self.sent.append(tuple(chunks))
                return True

        class FakeNtfy:
            instances: list["FakeNtfy"] = []

            def __init__(self, *args, **kwargs) -> None:
                self.args = args
                self.closed = False
                self.rows: list[tuple[dict, ...]] = []
                self.result = "failing" not in args
                self.instances.append(self)

            async def close(self) -> None:
                self.closed = True

            async def send_rows(self, rows) -> bool:
                self.rows.append(tuple(rows))
                return self.result

        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_push_config(
                telegram_bot_token="working-token",
                telegram_chat_id="123",
                now=now,
            )
            dispatcher = PushDispatcher(database)
            with (
                patch("app.push.BotPusher", FakeBot),
                patch("app.push.NtfyPusher", FakeNtfy),
            ):
                self.assertTrue(await dispatcher.send_immediate(self.row()))
                first = FakeBot.instances[-1]
                database.update_push_config(
                    telegram_enabled=False,
                    telegram_bot_token="",
                    clear_telegram_bot_token=False,
                    telegram_chat_id="123",
                    ntfy_enabled=True,
                    ntfy_base_url="https://ntfy.example.com",
                    ntfy_topic="failing",
                    ntfy_access_token="",
                    clear_ntfy_access_token=False,
                    now=now,
                )
                self.assertFalse(await dispatcher.send_immediate(self.row(message_id=2)))
                self.assertTrue(first.closed)
                database.update_push_config(
                    telegram_enabled=True,
                    telegram_bot_token="working-token-2",
                    clear_telegram_bot_token=False,
                    telegram_chat_id="123",
                    ntfy_enabled=True,
                    ntfy_base_url="https://ntfy.example.com",
                    ntfy_topic="failing",
                    ntfy_access_token="",
                    clear_ntfy_access_token=False,
                    now=now,
                )
                self.assertTrue(
                    await dispatcher.send_digest(
                        (self.row(message_id=3), self.row(message_id=4))
                    )
                )
                self.assertEqual(len(FakeBot.instances[-1].sent), 1)
                self.assertIn("重要资讯摘要", FakeBot.instances[-1].sent[0][0])
                self.assertEqual(len(FakeNtfy.instances[-1].rows[0]), 2)
                await dispatcher.close()
            database.close()


if __name__ == "__main__":
    unittest.main()
