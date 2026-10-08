from __future__ import annotations

import unittest

from app.semantic_update import validate_semantic_update


class SemanticUpdateValidationTests(unittest.TestCase):
    def validate(self, update_type: str, current: str, candidate: str):
        return validate_semantic_update(
            update_type,
            current={"text": current},
            candidate={"text": candidate},
        )

    def test_late_release_details_are_not_material_updates(self) -> None:
        candidate = "OpenAI 推出网络安全模型 GPT-5.6-Cyber。"
        detail_only = (
            (
                "impact_status_change",
                "GPT-Daybreak 项目使用 GPT-5.6-Cyber，并披露模型发现了浏览器漏洞和更多技术细节。",
            ),
            (
                "impact_status_change",
                "GPT-5.6-Cyber 上线，同时介绍企业版高级席位。",
            ),
            (
                "price_or_policy_change",
                "同一付费方案补充月付 199 元和年付 1499 元两个档位。",
            ),
            (
                "impact_status_change",
                "同一水印功能补充 C2PA 标准和法规背景。",
            ),
            (
                "impact_status_change",
                "同一开源模型补充 SWE-Bench 76% 的基准成绩。",
            ),
            (
                "new_version_or_cve",
                "同一安全模型补充发现 CVE-2026-12345 和多个版本基准的成绩。",
            ),
        )
        for update_type, current in detail_only:
            with self.subTest(current=current):
                result = self.validate(update_type, current, candidate)
                self.assertFalse(result.valid)
                self.assertTrue(result.rejection_reason)

    def test_service_recovery_and_worsening_are_validated(self) -> None:
        recovered = self.validate(
            "service_status_change",
            "云服务故障已解除，服务恢复正常。",
            "云服务发生故障，部分功能不可用。",
        )
        worsened = self.validate(
            "service_status_change",
            "云服务故障扩大并再次中断。",
            "云服务性能下降。",
        )
        self.assertTrue(recovered.valid)
        self.assertTrue(worsened.valid)

    def test_confirmation_requires_an_uncertain_candidate(self) -> None:
        self.assertTrue(
            self.validate(
                "confirmation_or_correction",
                "官方确认此前传闻属实。",
                "网传该平台可能调整服务。",
            ).valid
        )
        self.assertFalse(
            self.validate(
                "confirmation_or_correction",
                "官方确认并补充更多产品参数。",
                "平台已经正式发布该产品。",
            ).valid
        )

    def test_new_identifiers_and_explicit_changes_are_validated(self) -> None:
        cases = (
            (
                "new_version_or_cve",
                "产品发布 v2.4.0 新版本。",
                "产品发布 v2.3.0。",
            ),
            (
                "new_version_or_cve",
                "公告新增 CVE-2026-12345。",
                "此前公告涉及 CVE-2026-10000。",
            ),
            (
                "price_or_policy_change",
                "平台再次涨价，月费由 10 元调整为 12 元。",
                "平台月费为 10 元。",
            ),
            (
                "region_or_availability_change",
                "该功能新增日本地区并扩展至日本市场。",
                "该功能目前仅在美国市场提供。",
            ),
            (
                "date_or_deadline_change",
                "截止日期调整为 8月15日。",
                "原截止日期为 8月10日。",
            ),
            (
                "impact_status_change",
                "事件影响范围扩大，新增受影响用户。",
                "事件仅影响少量测试账户。",
            ),
        )
        for update_type, current, candidate in cases:
            with self.subTest(update_type=update_type):
                self.assertTrue(self.validate(update_type, current, candidate).valid)

    def test_body_identifier_cannot_override_release_summary(self) -> None:
        result = validate_semantic_update(
            "new_version_or_cve",
            current={
                "summary": "同一安全模型发布事件补充技术成绩",
                "text": "长文中安全公告披露 CVE-2026-12345，并包含多个测试版本数字。",
            },
            candidate={"text": "该安全模型已经正式发布。"},
        )
        self.assertFalse(result.valid)


if __name__ == "__main__":
    unittest.main()
