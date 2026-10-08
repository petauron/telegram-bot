from __future__ import annotations

import unittest

from app.prefilter import (
    PrefilterReason,
    evaluate_prefilter,
    evaluate_prequeue_prefilter,
    recent_exact_duplicate_result,
)


class PrefilterTests(unittest.TestCase):
    def assert_filtered(self, text: str, reason: PrefilterReason) -> None:
        result = evaluate_prefilter(text)
        self.assertTrue(result.filtered)
        self.assertEqual(result.reason_code, reason.value)
        self.assertTrue(result.reason)

    def test_symbols_emoji_punctuation_and_keycap_are_filtered(self) -> None:
        for text in ("😂 🎉", "……？！", "   \n", "1️⃣"):
            with self.subTest(text=text):
                self.assert_filtered(text, PrefilterReason.SYMBOLS_ONLY)

    def test_bare_links_are_filtered_but_semantic_context_is_preserved(self) -> None:
        for text in (
            "https://example.test/article",
            "🔥 https://example.test/article …",
            "www.example.test/release",
            "t.me/example_channel/123",
        ):
            with self.subTest(text=text):
                self.assert_filtered(text, PrefilterReason.BARE_LINK)
        for text in (
            "宕机 https://status.example.test/incident",
            "CVE-2026-12345 https://example.test/advisory",
            "平台发布重要升级：https://example.test/release",
            "[查看重要升级](https://example.test/release)",
        ):
            with self.subTest(text=text):
                self.assertFalse(evaluate_prefilter(text).filtered)

    def test_database_confirmed_duplicate_has_stable_audit_reason(self) -> None:
        result = recent_exact_duplicate_result()
        self.assertTrue(result.filtered)
        self.assertEqual(result.reason_code, PrefilterReason.RECENT_EXACT_DUPLICATE.value)
        self.assertIn("72 小时", result.reason)

    def test_known_sticker_placeholders_are_anchored(self) -> None:
        for text in (
            "文件名：sticker.webp",
            "文件名: sticker.webm",
            "AnimatedSticker.tgs",
        ):
            with self.subTest(text=text):
                self.assert_filtered(text, PrefilterReason.STICKER_PLACEHOLDER)
        self.assertFalse(evaluate_prefilter("新版支持 sticker.webp 格式").filtered)

    def test_pure_bot_command_and_checkin_workflow(self) -> None:
        self.assert_filtered("/checkin@sample_bot", PrefilterReason.BOT_COMMAND)
        self.assert_filtered("/疯狂星期四", PrefilterReason.BOT_COMMAND)
        for text in ("签到", "簽到", "/签到", "/簽到", "  签到\n"):
            with self.subTest(text=text):
                self.assert_filtered(text, PrefilterReason.CHECKIN_POINTS)
        self.assert_filtered(
            "签到成功，连续签到 3 天，获得积分 5",
            PrefilterReason.CHECKIN_POINTS,
        )
        for text in (
            "感谢您的签到，10 分到账",
            "用户甲，感谢您的签到，5 分到账。",
        ):
            with self.subTest(text=text):
                self.assert_filtered(text, PrefilterReason.CHECKIN_POINTS)
        for text in (
            "签到机制发生安全漏洞",
            "平台签到系统发布重要升级",
            "某产品新增签到功能公告",
        ):
            with self.subTest(text=text):
                self.assertFalse(evaluate_prefilter(text).filtered)

    def test_welcome_verification_workflows_require_combined_anchors(self) -> None:
        examples = (
            "入群验证：欢迎加入群组，请完成验证",
            "欢迎 新成员 加入群组！请完成入群验证。",
            "👋 欢迎 用户！请点击下方按钮前往私信完成验证（限时 120 秒）",
            "请在 5 分钟内点击按钮完成验证",
            "Welcome new member, complete group verification",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assert_filtered(text, PrefilterReason.WELCOME_VERIFICATION)
        for text in ("欢迎发布新版", "欢迎阅读本周安全新闻", "新成员发表了产品报告"):
            with self.subTest(text=text):
                self.assertFalse(evaluate_prefilter(text).filtered)

    def test_moderation_automation_requires_full_signature(self) -> None:
        examples = (
            "用户已禁言 - 入群风控",
            "禁止发送外部引用消息 • 现有警告 2 • 处理完成",
            "检测到违规消息，已删除并封禁",
            "提问无截图，自动踢出群聊",
            "用户甲由于验证回答错误，未能完成入群验证，已被封禁 2 分钟。",
            "🚫 自动拦截：命中封禁阈值 • 处理：已封禁 • 原消息：已删除",
            "已将 用户 sample 封禁，原因：触发拦截词",
            "ℹ️ 封禁操作 • 对象：用户 sample • 状态：已在封禁状态，无需重复操作",
            "🚫 已标记为广告，图片指纹已加入，消息已删除，用户已封禁",
            "常见问题请多看置顶或者机器人帮助，本群主要以聊天为主，本群退群后自动封禁",
            "欢迎加入群组，新人建议先看官方文档，群内可以吹水但是要注意群规",
            "用户甲 禁止发送外部引用消息 • 警告(1/5)",
            "用户甲 请先关注如下频道或加入群组后才能发言\n惩罚: 禁言到 - 2026-08-11 12:00:00 +0800",
            "用户乙 请先关注指定频道或加入群组后才能发言\n惩罚：禁言",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assert_filtered(text, PrefilterReason.MODERATION_AUTOMATION)
        for text in (
            "某平台发布账号封禁政策",
            "监管机构公布新的平台禁言规则",
            "安全报告分析机器人账号封禁趋势",
            "媒体调查自动拦截与封禁阈值是否侵犯用户权益",
            "平台公告调整广告消息删除和账号封禁政策",
            "媒体报道某社区要求用户先关注频道或加入群组后才能发言",
            "平台将取消关注频道后才能发言和违规禁言的旧政策",
        ):
            with self.subTest(text=text):
                self.assertFalse(evaluate_prefilter(text).filtered)

    def test_backend_selection_requires_bot_workflow_combination(self) -> None:
        self.assert_filtered(
            "请选择一个后端，点击按钮切换后端",
            PrefilterReason.BACKEND_SELECTION,
        )
        self.assert_filtered("请选择可用后端：", PrefilterReason.BACKEND_SELECTION)
        self.assertFalse(evaluate_prefilter("数据库后端发布重要安全升级").filtered)

    def test_short_operational_signals_are_not_removed(self) -> None:
        keywords = ("宕机", "被墙", "CVE", "涨价", "恢复")
        for text in ("宕机", "被墙", "CVE-2026-1234", "涨价", "恢复"):
            with self.subTest(text=text):
                self.assertFalse(
                    evaluate_prefilter(text, protected_keywords=keywords).filtered
                )

    def test_short_unprotected_text_is_filtered_before_model_analysis(self) -> None:
        for text in ("a", "d", "官网", "对啊", "abc", "abcd", "代理工具", " a \n"):
            with self.subTest(text=text):
                self.assert_filtered(text, PrefilterReason.SHORT_UNPROTECTED_TEXT)
        for text in (
            "炸了",
            "又炸了",
            "挂了",
            "崩了",
            "恢复",
            "已修复",
            "已恢复",
            "已更新",
            "停服了",
            "开源了",
            "限免",
            "优惠码",
            "兑换码",
            "免费领取",
            "免费试用",
            "补货",
            "补货了",
            "免费",
            "优惠",
            "羊毛",
            "代金券",
        ):
            with self.subTest(text=text):
                self.assertFalse(evaluate_prefilter(text).filtered)
        self.assertFalse(
            evaluate_prefilter(
                "版本发布", protected_keywords=("版本发布",)
            ).filtered
        )
        self.assertFalse(evaluate_prefilter("平台版本发布").filtered)
        self.assertFalse(
            evaluate_prefilter("油价", protected_keywords=("油价",)).filtered
        )
        self.assertTrue(evaluate_prefilter("油价").filtered)
        self.assertTrue(evaluate_prefilter("折扣").filtered)

    def test_service_and_known_bot_status_workflows_are_filtered(self) -> None:
        service = evaluate_prefilter("用户加入了群组", is_service_message=True)
        self.assertTrue(service.filtered)
        self.assertEqual(
            service.reason_code,
            PrefilterReason.TELEGRAM_SERVICE_MESSAGE.value,
        )
        for text in (
            "🤔 Thinking.…",
            "▎解 析 中...",
            "111111111",
            "正在完成操作...",
            "您好，为您创建了一个测试任务，请选择测试的类型",
            "任务提交成功，正在处理中，任务名称：example，测试项：5个",
            "您本日的规则触发数量上限，请明日再试",
        ):
            with self.subTest(text=text):
                self.assert_filtered(text, PrefilterReason.BOT_STATUS_WORKFLOW)
        self.assertFalse(evaluate_prefilter("产品发布 Thinking 推理模式").filtered)

    def test_bot_generated_digest_uses_combined_signature(self) -> None:
        self.assert_filtered(
            "🍉 群聊吃瓜日报 (Daily Gossip) 今日自动汇总",
            PrefilterReason.BOT_DIGEST_WORKFLOW,
        )
        for text in (
            "媒体发布 Daily Gossip 产品分析",
            "群聊吃瓜日报栏目宣布停更",
        ):
            with self.subTest(text=text):
                self.assertFalse(evaluate_prefilter(text).filtered)

    def test_historical_automated_workflows_use_combined_signatures(self) -> None:
        examples = (
            ("发言权限尚未解锁（0/20），请先完成 20 条中文文字发言", PrefilterReason.MODERATION_AUTOMATION),
            ("验证已过期，未能完成入群验证，已被封禁 2 分钟", PrefilterReason.MODERATION_AUTOMATION),
            ("今日活跃任务完成，+2 积分。", PrefilterReason.CHECKIN_POINTS),
            ("积分获取方式和积分使用规则，请查阅帮助页面", PrefilterReason.CHECKIN_POINTS),
            ("推荐的代理工具：", PrefilterReason.SUPPORT_AUTOMATION),
            ("客服不是24小时在线，有问题留言等待回复，不要催", PrefilterReason.SUPPORT_AUTOMATION),
            (
                "只有一次性不限时套餐可以叠加，续费相同月付套餐叠加时间，购买不同月付套餐会覆盖掉原套餐",
                PrefilterReason.SUPPORT_AUTOMATION,
            ),
            (
                "把订阅链接、账号、新密码发送到客服或官网工单，信息发全",
                PrefilterReason.SUPPORT_AUTOMATION,
            ),
        )
        for text, reason in examples:
            with self.subTest(text=text):
                self.assert_filtered(text, reason)
        for text in (
            "某平台更新发言权限政策",
            "研究报告分析积分制度的安全风险",
            "AI 产品发布新的 Thinking 推理模式",
        ):
            with self.subTest(text=text):
                self.assertFalse(evaluate_prefilter(text).filtered)

    def test_prequeue_filter_is_an_explicit_context_free_subset(self) -> None:
        safe_examples = (
            ("😂 🎉", False, PrefilterReason.SYMBOLS_ONLY),
            ("文件名：sticker.webp", False, PrefilterReason.STICKER_PLACEHOLDER),
            ("/help@sample_bot", False, PrefilterReason.BOT_COMMAND),
            ("欢迎 新成员 加入群组！请完成入群验证。", False, PrefilterReason.WELCOME_VERIFICATION),
            ("检测到违规消息，已删除并封禁", False, PrefilterReason.MODERATION_AUTOMATION),
            ("感谢您的签到，10 分到账", False, PrefilterReason.CHECKIN_POINTS),
            ("请选择一个后端，点击按钮切换后端", False, PrefilterReason.BACKEND_SELECTION),
            ("客服不是24小时在线，有问题留言等待回复，不要催", False, PrefilterReason.SUPPORT_AUTOMATION),
            ("Telegram 服务事件：成员变更", True, PrefilterReason.TELEGRAM_SERVICE_MESSAGE),
        )
        for text, is_service, reason in safe_examples:
            with self.subTest(text=text):
                result = evaluate_prequeue_prefilter(
                    text,
                    is_service_message=is_service,
                )
                self.assertTrue(result.filtered)
                self.assertEqual(result.reason_code, reason.value)

        # This decision may be rescued by same-thread/community context and
        # therefore must stay inside the ordered worker.
        self.assertFalse(evaluate_prequeue_prefilter("官网").filtered)
        self.assertEqual(
            evaluate_prefilter("官网").reason_code,
            PrefilterReason.SHORT_UNPROTECTED_TEXT.value,
        )
        self.assertFalse(evaluate_prequeue_prefilter("炸了").filtered)


if __name__ == "__main__":
    unittest.main()
