from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from app.database import Database, MessageRecord, to_iso, utc_now
from app.dedupe import select_semantic_candidates
from app.llm import (
    NotificationPreparationOutcome,
    OpenAICompatibleClient,
    analyze_persisted_message,
)
from app.scoring import normalize_text
from app.semantic_dedupe import SemanticDedupeGate
from tests.fake_openai import FakeOpenAIServer


def record(
    chat_id: int,
    message_id: int,
    text: str,
    *,
    now=None,
) -> MessageRecord:
    timestamp = now or utc_now()
    return MessageRecord(
        chat_id=chat_id,
        message_id=message_id,
        chat_name="去标识来源",
        chat_username=None,
        sender_id=None,
        sender_name="匿名",
        sent_at=to_iso(timestamp),
        text=text,
        reply_to_message_id=None,
        thread_root_id=message_id,
        base_score=25,
        reasons=("本地审计",),
        link=None,
        normalized_text=normalize_text(text),
        primary_url=None,
        created_at=to_iso(timestamp),
    )


class SemanticDedupePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeOpenAIServer().start()
        self.client = OpenAICompatibleClient()
        self.gate = SemanticDedupeGate(self.client)
        self.directory = tempfile.TemporaryDirectory()
        self.path = f"{self.directory.name}/messages.db"
        database = Database(self.path)
        now = utc_now()
        database.initialize_runtime_config(
            important_keywords=("AI", "开源", "漏洞", "涨价"),
            trusted_sender_ids=frozenset(),
            watch_chat_ids=frozenset({-1001, -2002, -3003}),
            immediate_score=80,
            now=now,
        )
        database.update_model_config(
            enabled=True,
            base_url=self.server.base_url,
            model="test-model-a",
            classification_model="test-model-z",
            api_key=self.server.api_key,
            clear_api_key=False,
            now=now,
            semantic_dedupe_model="test-dedupe-model",
            semantic_dedupe_reasoning_effort="medium",
            notification_model="test-notification-model",
            notification_reasoning_effort="high",
        )
        database.initialize_push_config(
            telegram_bot_token="test-token",
            telegram_chat_id="123456",
            now=now,
        )
        database.close()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.server.close()
        self.directory.cleanup()

    async def analyze(
        self,
        database: Database,
        item: MessageRecord,
        *,
        score: int,
        manual: bool = False,
        enqueue_delivery: bool = True,
    ) -> dict:
        database.insert_message(item)
        self.server.scoring_content = json.dumps(
            {
                "score": score,
                "summary": f"事件摘要 {item.message_id}",
                "reason": "与技术兴趣直接相关且有具体变化",
            },
            ensure_ascii=False,
        )
        result = await analyze_persisted_message(
            database=database,
            client=self.client,
            row=database.get_message(item.chat_id, item.message_id),
            now=utc_now(),
            manual=manual,
            semantic_gate=self.gate,
        )
        assert result is not None
        if not manual and enqueue_delivery:
            database.enqueue_immediate_deliveries(
                item.chat_id,
                item.message_id,
                now=utc_now(),
            )
        return result

    async def test_no_candidates_skips_dedupe_request_and_marks_ready(self) -> None:
        database = Database(self.path)
        before = len(self.server.requests)
        result = await self.analyze(
            database,
            record(-1001, 1, "AI 平台上线新的代码分析能力"),
            score=68,
        )
        stages = [item.get("stage") for item in self.server.requests[before:]]
        self.assertEqual(stages, ["classification", "scoring", "notification"])
        self.assertEqual(result["semantic_dedupe_status"], "unique_no_candidates")
        self.assertTrue(result["push_eligible"])
        self.assertIsNotNone(result["push_ready_at"])
        self.assertEqual(result["notification_prepare_status"], "success")
        self.assertEqual(result["notification_prepare_model"], "test-notification-model")
        self.assertEqual(result["notification_prepare_effort"], "high")
        database.close()

    async def test_below_push_line_never_calls_dedupe_or_notification(self) -> None:
        database = Database(self.path)
        before = len(self.server.requests)
        result = await self.analyze(
            database,
            record(-1001, 1, "普通但完整的开发行业资讯"),
            score=59,
        )
        self.assertEqual(
            [item.get("stage") for item in self.server.requests[before:]],
            ["classification", "scoring"],
        )
        self.assertEqual(
            result["notification_prepare_status"], "not_required_below_threshold"
        )
        self.assertFalse(result["push_eligible"])
        database.close()

    async def test_invalid_notification_json_fails_open_once_and_delivery_reuses_fallback(self) -> None:
        database = Database(self.path)
        database.initialize_push_config(
            telegram_bot_token="test-token",
            telegram_chat_id="123456",
            now=utc_now(),
        )
        database.update_push_config(
            telegram_enabled=True,
            telegram_bot_token="",
            clear_telegram_bot_token=False,
            telegram_chat_id="123456",
            ntfy_enabled=True,
            ntfy_base_url="https://ntfy.example.com",
            ntfy_topic="test-topic",
            ntfy_access_token="",
            clear_ntfy_access_token=False,
            now=utc_now(),
        )
        self.server.notification_content = '{"title":"缺少正文"}'
        before = len(self.server.requests)
        result = await self.analyze(
            database,
            record(-1001, 1, "云平台发布重要安全修复及影响说明"),
            score=82,
            enqueue_delivery=False,
        )
        self.assertEqual(
            [item.get("stage") for item in self.server.requests[before:]],
            ["classification", "scoring", "notification"],
        )
        self.assertEqual(result["notification_prepare_status"], "failed_fallback")
        self.assertEqual(result["notification_prepare_error_category"], "invalid_response")
        self.assertTrue(result["notification_title"])
        self.assertTrue(result["notification_body"])
        self.assertTrue(result["push_eligible"])

        calls = len(self.server.requests)
        self.assertEqual(
            database.enqueue_immediate_deliveries(
                result["chat_id"], result["message_id"], now=utc_now()
            ),
            2,
        )
        first = database.claim_next_delivery_unit(now=utc_now())
        database.retry_delivery_unit(
            first["ids"],
            now=utc_now(),
            delay_seconds=1,
            error_category="network_error",
        )
        recovered = database.claim_next_delivery_unit(now=utc_now() + timedelta(seconds=2))
        self.assertEqual(
            {row["notification_title"] for row in (*first["rows"], *recovered["rows"])},
            {result["notification_title"]},
        )
        self.assertEqual(len(self.server.requests), calls)
        database.close()

    async def test_notification_timeout_fails_open_without_analysis_retry(self) -> None:
        database = Database(self.path)
        with patch.object(
            self.client,
            "prepare_notification",
            new=AsyncMock(
                return_value=NotificationPreparationOutcome(
                    status="error",
                    model="test-notification-model",
                    effort="high",
                    error_category="timeout",
                )
            ),
        ):
            result = await self.analyze(
                database,
                record(-1001, 1, "开发平台发布新的高价值构建能力"),
                score=72,
            )
        self.assertEqual(result["ai_status"], "success")
        self.assertEqual(result["notification_prepare_status"], "failed_fallback")
        self.assertEqual(result["notification_prepare_error_category"], "timeout")
        self.assertTrue(result["push_eligible"])
        database.close()

    async def test_cross_source_same_event_is_suppressed_once_and_idempotent(self) -> None:
        database = Database(self.path)
        first = await self.analyze(
            database,
            record(-1001, 1, "AI 代理利用预订系统漏洞执行未授权操作"),
            score=82,
        )
        self.server.dedupe_content = json.dumps(
            {
                "same_event": True,
                "match_index": 1,
                "confidence": 98,
                "material_update": False,
                "update_type": "none",
                "reason": "不同来源转述同一次安全事件",
            },
            ensure_ascii=False,
        )
        before = len(self.server.requests)
        second = await self.analyze(
            database,
            record(-2002, 2, "智能体对健身房预约服务实施相同漏洞攻击"),
            score=82,
        )
        stages = [item.get("stage") for item in self.server.requests[before:]]
        self.assertEqual(stages, ["classification", "scoring", "dedupe"])
        self.assertEqual(second["semantic_dedupe_status"], "suppressed")
        self.assertEqual(
            second["notification_prepare_status"],
            "not_required_semantic_duplicate",
        )
        self.assertFalse(second["push_eligible"])
        self.assertEqual(second["semantic_dedupe_matched_message_id"], first["id"])
        calls = len(self.server.requests)
        unchanged = await self.gate.evaluate(
            database=database,
            row_id=second["id"],
            manual=False,
            now=utc_now(),
        )
        self.assertEqual(len(self.server.requests), calls)
        self.assertEqual(unchanged["semantic_dedupe_status"], "suppressed")
        database.close()

    async def test_cross_batch_duplicate_cannot_replace_already_delivered_event(self) -> None:
        database = Database(self.path)
        first = await self.analyze(
            database,
            record(-1001, 1, "某音乐服务公布中国区订阅新价格"),
            score=82,
        )
        delivered_at = to_iso(utc_now())
        with database.connection:
            database.connection.execute(
                """
                UPDATE deliveries
                SET state = 'succeeded', updated_at = ?, delivered_at = ?
                WHERE message_row_id = ? AND channel = 'telegram'
                  AND delivery_type = 'immediate'
                """,
                (delivered_at, delivered_at, first["id"]),
            )
        self.server.dedupe_content = json.dumps(
            {
                "same_event": True,
                "match_index": 1,
                "confidence": 99,
                "material_update": False,
                "update_type": "none",
                "reason": "另一来源改写了同一次价格公告",
            },
            ensure_ascii=False,
        )
        later = await self.analyze(
            database,
            record(-2002, 2, "中国区音乐订阅费用已调整"),
            score=90,
        )
        self.assertEqual(later["semantic_dedupe_status"], "suppressed")
        self.assertFalse(later["push_eligible"])
        original = database.get_message_by_id(first["id"])
        self.assertTrue(original["push_eligible"])
        self.assertEqual(original["semantic_dedupe_status"], "unique_no_candidates")
        database.close()

    async def test_material_update_and_low_confidence_are_fail_safe(self) -> None:
        database = Database(self.path)
        first = await self.analyze(
            database,
            record(-1001, 1, "云服务发生区域性故障"),
            score=80,
        )
        self.server.dedupe_content = json.dumps(
            {
                "same_event": True,
                "match_index": 1,
                "confidence": 97,
                "material_update": True,
                "update_type": "service_status_change",
                "reason": "同一故障已恢复，状态发生实质变化",
            },
            ensure_ascii=False,
        )
        restored = await self.analyze(
            database,
            record(-2002, 2, "云服务确认区域故障已经恢复"),
            score=80,
        )
        self.assertEqual(restored["semantic_dedupe_status"], "material_update")
        self.assertTrue(restored["push_eligible"])
        self.assertEqual(restored["semantic_dedupe_matched_message_id"], first["id"])
        self.assertEqual(restored["semantic_dedupe_update_type"], "service_status_change")
        self.assertTrue(restored["semantic_dedupe_update_validated"])
        self.assertIsNone(restored["semantic_dedupe_update_rejection_reason"])

        self.server.dedupe_content = json.dumps(
            {
                "same_event": True,
                "match_index": 1,
                "confidence": 70,
                "material_update": False,
                "update_type": "none",
                "reason": "可能相关但证据不足",
            },
            ensure_ascii=False,
        )
        uncertain = await self.analyze(
            database,
            record(-3003, 3, "另一项云平台状态公告"),
            score=70,
        )
        self.assertEqual(uncertain["semantic_dedupe_status"], "low_confidence_pass")
        self.assertTrue(uncertain["push_eligible"])
        database.close()

    async def test_release_details_and_mixed_side_topic_are_suppressed(self) -> None:
        database = Database(self.path)
        first = await self.analyze(
            database,
            record(-1001, 1, "OpenAI 推出网络安全模型 GPT-5.6-Cyber。"),
            score=75,
        )
        self.server.dedupe_content = json.dumps(
            {
                "same_event": True,
                "match_index": 1,
                "confidence": 95,
                "material_update": True,
                "update_type": "impact_status_change",
                "reason": "同一发布事件增加了项目名称和漏洞发现细节",
            },
            ensure_ascii=False,
        )
        before = len(self.server.requests)
        detail = await self.analyze(
            database,
            record(
                -2002,
                2,
                "GPT-Daybreak 使用 GPT-5.6-Cyber，并披露浏览器漏洞发现成绩。",
            ),
            score=85,
        )
        self.assertEqual(
            [item.get("stage") for item in self.server.requests[before:]],
            ["classification", "scoring", "dedupe"],
        )
        self.assertEqual(detail["semantic_dedupe_status"], "suppressed_unverified_update")
        self.assertFalse(detail["semantic_dedupe_material_update"])
        self.assertEqual(detail["semantic_dedupe_update_type"], "impact_status_change")
        self.assertFalse(detail["semantic_dedupe_update_validated"])
        self.assertTrue(detail["semantic_dedupe_update_rejection_reason"])
        self.assertFalse(detail["push_eligible"])
        self.assertEqual(
            detail["notification_prepare_status"],
            "not_required_semantic_duplicate",
        )

        mixed = await self.analyze(
            database,
            record(
                -3003,
                3,
                "GPT-5.6-Cyber 上线，同时介绍 ChatGPT 企业版高级席位。",
            ),
            score=78,
        )
        self.assertEqual(mixed["semantic_dedupe_status"], "suppressed_unverified_update")
        self.assertFalse(mixed["push_eligible"])
        self.assertEqual(mixed["semantic_dedupe_matched_message_id"], first["id"])
        database.close()

    async def test_dedupe_failure_fails_open_without_analysis_failure(self) -> None:
        database = Database(self.path)
        await self.analyze(
            database,
            record(-1001, 1, "平台宣布订阅价格调整"),
            score=82,
        )
        self.server.dedupe_status = 500
        result = await self.analyze(
            database,
            record(-2002, 2, "另一平台发布高价值服务变更"),
            score=82,
        )
        self.assertEqual(result["ai_status"], "success")
        self.assertEqual(result["semantic_dedupe_status"], "failed_open")
        self.assertEqual(result["semantic_dedupe_error_category"], "upstream_error")
        self.assertTrue(result["push_eligible"])
        self.assertEqual(result["push_gate_reason"], "eligible_notification_prepared")
        self.assertEqual(result["semantic_dedupe_effort"], "medium")
        database.close()

    async def test_higher_score_replaces_concurrent_undelivered_representative(self) -> None:
        database = Database(self.path)
        first = await self.analyze(
            database,
            record(-1001, 1, "平台公布产品价格调整"),
            score=65,
            enqueue_delivery=False,
        )
        with database.connection:
            database.connection.execute(
                "UPDATE messages SET analysis_queue_state = 'processing' WHERE id = ?",
                (first["id"],),
            )
        self.server.dedupe_content = json.dumps(
            {
                "same_event": True,
                "match_index": 1,
                "confidence": 99,
                "material_update": False,
                "update_type": "none",
                "reason": "同一价格公告的更完整转述",
            },
            ensure_ascii=False,
        )
        better = await self.analyze(
            database,
            record(-2002, 2, "平台确认产品新价格和生效日期"),
            score=82,
        )
        original = database.get_message_by_id(first["id"])
        self.assertEqual(original["semantic_dedupe_status"], "superseded")
        self.assertFalse(original["push_eligible"])
        self.assertEqual(better["semantic_dedupe_status"], "representative_replaced")
        self.assertIsNone(better["semantic_dedupe_matched_message_id"])
        self.assertEqual(
            original["semantic_dedupe_matched_message_id"],
            better["id"],
        )
        cluster = database.semantic_message_cluster(better["id"])
        self.assertEqual(cluster["representative"]["id"], better["id"])
        self.assertEqual(cluster["similar_count"], 1)
        self.assertEqual([item["id"] for item in cluster["items"]], [first["id"]])
        self.assertTrue(better["push_eligible"])
        self.assertEqual(database.get_deliveries(first["id"]), [])
        deliveries = database.get_deliveries(better["id"])
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["delivery_type"], "immediate")
        database.close()

    async def test_manual_reanalysis_runs_dedupe_but_never_pushes(self) -> None:
        database = Database(self.path)
        first = await self.analyze(
            database,
            record(-1001, 1, "开源项目发布新的部署能力"),
            score=70,
        )
        database.enqueue_immediate_deliveries(
            first["chat_id"], first["message_id"], now=utc_now()
        )
        self.server.dedupe_content = json.dumps(
            {
                "same_event": True,
                "match_index": 1,
                "confidence": 98,
                "material_update": False,
                "update_type": "none",
                "reason": "同一项目发布消息",
            },
            ensure_ascii=False,
        )
        before = len(self.server.requests)
        manual = await self.analyze(
            database,
            record(-2002, 2, "开源工具上线同一项部署功能"),
            score=70,
            manual=True,
        )
        self.assertEqual(
            [item.get("stage") for item in self.server.requests[before:]],
            ["classification", "scoring", "dedupe"],
        )
        self.assertEqual(manual["semantic_dedupe_status"], "suppressed")
        self.assertFalse(manual["push_eligible"])
        self.assertIsNone(manual["push_ready_at"])
        self.assertEqual(manual["push_gate_reason"], "manual_reanalysis")
        database.close()

    async def test_two_workers_serialize_gate_and_only_one_event_passes(self) -> None:
        first_db = Database(self.path)
        second_db = Database(self.path)
        now = utc_now()
        first_record = record(-1001, 1, "同一行业数据报告由来源甲发布", now=now)
        second_record = record(-2002, 2, "来源乙转述同一行业数据报告", now=now)
        first_db.insert_message(first_record)
        second_db.insert_message(second_record)
        with first_db.connection:
            first_db.connection.execute(
                "UPDATE messages SET analysis_queue_state = 'processing' WHERE id IN (?, ?)",
                (
                    first_db.get_message(-1001, 1)["id"],
                    first_db.get_message(-2002, 2)["id"],
                ),
            )
        self.server.scoring_content = json.dumps(
            {"score": 72, "summary": "行业数据报告", "reason": "数据具体且来源明确"},
            ensure_ascii=False,
        )
        self.server.dedupe_content = json.dumps(
            {
                "same_event": True,
                "match_index": 1,
                "confidence": 99,
                "material_update": False,
                "update_type": "none",
                "reason": "同一份报告的跨来源转述",
            },
            ensure_ascii=False,
        )

        async def run(database: Database, chat_id: int, message_id: int):
            return await analyze_persisted_message(
                database=database,
                client=self.client,
                row=database.get_message(chat_id, message_id),
                now=now,
                semantic_gate=self.gate,
            )

        results = await asyncio.gather(
            run(first_db, -1001, 1),
            run(second_db, -2002, 2),
        )
        self.assertEqual(
            sorted(row["semantic_dedupe_status"] for row in results),
            ["suppressed", "unique_no_candidates"],
        )
        self.assertEqual(sum(bool(row["push_eligible"]) for row in results), 1)
        self.assertEqual(
            [item.get("stage") for item in self.server.requests].count("dedupe"),
            1,
        )
        first_db.close()
        second_db.close()


class SemanticDedupeDatabaseTests(unittest.TestCase):
    def test_candidate_query_scans_full_window_before_twelve_item_shortlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            for index in range(30):
                item_time = now - timedelta(minutes=31 - index)
                database.insert_message(
                    record(-1000 - index, index + 1, f"窗口候选 {index}", now=item_time)
                )
                with database.connection:
                    database.connection.execute(
                        """
                        UPDATE messages
                        SET ai_status = 'success', ai_category = 'external_information',
                            ai_score = 70, score = 70, ai_summary = ?,
                            push_eligible = 1, push_ready_at = ?,
                            semantic_dedupe_status = 'unique',
                            analysis_queue_state = 'processing'
                        WHERE chat_id = ? AND message_id = ?
                        """,
                        (
                            "目标平台价格调整" if index == 0 else f"普通事件 {index}",
                            to_iso(item_time),
                            -1000 - index,
                            index + 1,
                        ),
                    )
            current_record = record(
                -9000,
                999,
                "目标平台公布新的订阅价格",
                now=now,
            )
            database.insert_message(current_record)
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET ai_status = 'success', ai_category = 'external_information',
                        ai_score = 82, score = 82,
                        ai_summary = '目标平台订阅价格上涨',
                        semantic_dedupe_status = 'pending'
                    WHERE chat_id = -9000 AND message_id = 999
                    """
                )
            current = database.get_message(-9000, 999)
            prepared = database.begin_semantic_dedupe(current["id"], now=now)
            self.assertIsNotNone(prepared)
            self.assertEqual(len(prepared["candidates"]), 30)
            shortlisted = select_semantic_candidates(
                prepared["current"], tuple(prepared["candidates"])
            )
            self.assertEqual(len(shortlisted), 12)
            self.assertIn(
                database.get_message(-1000, 1)["id"],
                {row["id"] for row in shortlisted},
            )
            database.close()

    def test_digest_waits_for_analysis_job_commit_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_digest_clock(now - timedelta(minutes=15))
            database.insert_message(
                record(-1001, 1, "队列内晚完成资讯", now=now),
                enqueue_analysis=True,
                now=now,
            )
            job = database.claim_next_analysis_job(now=now)
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET ai_status = 'success', ai_category = 'external_information',
                        ai_score = 72, score = 72, push_eligible = 1,
                        push_ready_at = ?, semantic_dedupe_status = 'unique',
                        notification_prepare_status = 'success'
                    WHERE message_id = 1
                    """,
                    (to_iso(now),),
                )
            self.assertEqual(database.digest_window_candidates(now + timedelta(seconds=1)), [])
            database.finish_analysis_job(
                int(job["id"]),
                now=now + timedelta(seconds=2),
                succeeded=True,
                result_status="success",
            )
            self.assertEqual(
                [row["message_id"] for row in database.digest_window_candidates(now + timedelta(seconds=3))],
                [1],
            )
            database.close()

    def test_digest_uses_ready_time_and_never_backfills_historical_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_digest_clock(now)
            historical = record(
                -1001,
                1,
                "历史高分资讯",
                now=now - timedelta(hours=2),
            )
            late = replace(
                record(-1001, 2, "晚完成的高分资讯", now=now - timedelta(minutes=20)),
                created_at=to_iso(now - timedelta(minutes=20)),
            )
            database.insert_message(historical)
            database.insert_message(late)
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = 'passed', ai_status = 'success',
                        ai_category = 'external_information', ai_score = 70,
                        score = 70, push_eligible = 1,
                        semantic_dedupe_status = 'historical_unreviewed'
                    WHERE message_id = 1
                    """
                )
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = 'passed', ai_status = 'success',
                        ai_category = 'external_information', ai_score = 72,
                        score = 72, push_eligible = 1,
                        push_ready_at = ?, semantic_dedupe_status = 'unique',
                        notification_prepare_status = 'success'
                    WHERE message_id = 2
                    """,
                    (to_iso(now + timedelta(minutes=1)),),
                )
            self.assertEqual(database.digest_window_candidates(now), [])
            later = database.digest_window_candidates(now + timedelta(minutes=2))
            self.assertEqual([row["message_id"] for row in later], [2])
            database.enqueue_digest_deliveries(later, [], cutoff=now + timedelta(minutes=2))
            self.assertEqual(
                database.digest_window_candidates(now + timedelta(minutes=3)),
                [],
            )
            self.assertIsNone(database.get_message(-1001, 1)["push_ready_at"])
            database.close()

    def test_migration_is_idempotent_and_does_not_backfill_ready_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            database.insert_message(record(-1001, 1, "现有历史消息"))
            database.close()
            Database(path).close()
            reopened = Database(path)
            row = reopened.get_message(-1001, 1)
            self.assertIsNone(row["push_ready_at"])
            self.assertEqual(row["semantic_dedupe_status"], "historical_unreviewed")
            self.assertIsNone(row["semantic_dedupe_update_type"])
            self.assertFalse(row["semantic_dedupe_update_validated"])
            self.assertIsNone(row["semantic_dedupe_update_rejection_reason"])
            self.assertEqual(
                reopened.connection.execute(
                    "SELECT COUNT(*) FROM semantic_dedupe_lock"
                ).fetchone()[0],
                1,
            )
            reopened.close()

    def test_restart_recovers_interrupted_semantic_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            now = utc_now()
            database.insert_message(record(-1001, 1, "需要恢复的语义检查", now=now))
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET ai_status = 'success', ai_category = 'external_information',
                        ai_score = 80, score = 80,
                        semantic_dedupe_status = 'checking',
                        push_gate_reason = 'semantic_dedupe_checking'
                    WHERE message_id = 1
                    """
                )
            self.assertTrue(
                database.acquire_semantic_dedupe_lock(
                    "test-owner", now=now, lease_seconds=240
                )
            )
            database.close()

            reopened = Database(path)
            self.assertEqual(reopened.recover_semantic_dedupe(now=now), 1)
            row = reopened.get_message(-1001, 1)
            self.assertEqual(row["semantic_dedupe_status"], "pending")
            self.assertFalse(row["push_eligible"])
            self.assertTrue(
                reopened.acquire_semantic_dedupe_lock(
                    "new-owner", now=now, lease_seconds=240
                )
            )
            reopened.close()


if __name__ == "__main__":
    unittest.main()
