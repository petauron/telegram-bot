from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from app.analysis_queue import (
    PersistentAnalysisQueue,
    retry_delay_seconds,
    should_retry_analysis_error,
)
from app.database import Database, MessageRecord, to_iso, utc_now
from app.llm import (
    BatchClassificationOutcome,
    ClassificationOutcome,
    NotificationPreparationOutcome,
    ScoringOutcome,
    model_runtime_config_from_value,
    preflight_persisted_message_for_batch,
)
from app.main import maintenance_loop


def record(chat_id: int, message_id: int, *, sent_offset: int = 0) -> MessageRecord:
    now = utc_now() + timedelta(seconds=sent_offset)
    return MessageRecord(
        chat_id=chat_id,
        message_id=message_id,
        chat_name="去标识会话",
        chat_username=None,
        sender_id=None,
        sender_name="匿名",
        sent_at=to_iso(now),
        text=f"公开产品发布安全更新 {message_id}",
        reply_to_message_id=None,
        thread_root_id=message_id,
        base_score=25,
        reasons=("本地审计",),
        link=None,
        normalized_text=f"公开产品发布安全更新{message_id}",
        primary_url=None,
        created_at=to_iso(now),
    )


def mark_eligible(database: Database, row_id: int, score: int = 88) -> None:
    with database.connection:
        database.connection.execute(
            """
            UPDATE messages
            SET prefilter_status = 'passed', ai_status = 'success',
                ai_category = 'external_information', ai_score = ?, score = ?,
                push_eligible = 1, push_gate_reason = 'eligible_unique',
                push_ready_at = created_at,
                semantic_dedupe_status = 'unique_no_candidates',
                notification_prepare_status = 'success'
            WHERE id = ?
            """,
            (score, score, row_id),
        )


class AnalysisQueueDatabaseTests(unittest.TestCase):
    def test_retry_delay_is_exponential_and_bounded(self) -> None:
        self.assertEqual([retry_delay_seconds(value) for value in range(1, 6)], [5, 10, 20, 40, 80])
        self.assertEqual(retry_delay_seconds(20), 300)

    def test_invalid_structured_response_retries_only_once(self) -> None:
        self.assertTrue(should_retry_analysis_error("invalid_response", 1))
        self.assertFalse(should_retry_analysis_error("invalid_response", 2))
        self.assertTrue(should_retry_analysis_error("network_error", 4))
        self.assertFalse(should_retry_analysis_error("configuration", 1))

    def test_safe_prefilters_persist_without_creating_analysis_jobs(self) -> None:
        examples = (
            ("文件名：sticker.webp", False, "sticker_placeholder"),
            ("😂 🎉", False, "symbols_only"),
            ("欢迎 新成员 加入群组！请完成入群验证。", False, "welcome_verification"),
            ("检测到违规消息，已删除并封禁", False, "moderation_automation"),
            ("感谢您的签到，10 分到账", False, "checkin_points"),
            ("/help@sample_bot", False, "bot_command"),
            ("请选择一个后端，点击按钮切换后端", False, "backend_selection"),
            ("客服不是24小时在线，有问题留言等待回复，不要催", False, "support_automation"),
            ("Telegram 服务事件：成员变更", True, "telegram_service_message"),
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            for message_id, (text, is_service, reason) in enumerate(examples, 1):
                item = replace(
                    record(-1001, message_id, sent_offset=message_id),
                    text=text,
                    normalized_text=text,
                    is_service_message=is_service,
                )
                self.assertTrue(database.insert_message(item, enqueue_analysis=True, now=now))
                stored = database.get_message(-1001, message_id)
                self.assertEqual(stored["prefilter_status"], "filtered")
                self.assertEqual(stored["prefilter_reason_code"], reason)
                self.assertEqual(stored["ai_status"], "prefiltered")
                self.assertEqual(stored["analysis_queue_state"], "succeeded")
                self.assertFalse(stored["analysis_queue_requested"])
                self.assertEqual(stored["push_gate_reason"], "prefiltered")
                self.assertEqual(
                    stored["semantic_dedupe_status"],
                    "not_required_prefilter",
                )
                self.assertEqual(
                    stored["notification_prepare_status"],
                    "not_required_prefilter",
                )
                self.assertIsNotNone(stored["ai_completed_at"])
            self.assertEqual(
                database.connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0],
                0,
            )
            database.close()

    def test_contextual_short_and_ordered_duplicate_checks_still_create_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            texts = ("官网", "炸了", "同一条需要按群终态判断的完整消息", "同一条需要按群终态判断的完整消息")
            for message_id, text in enumerate(texts, 1):
                item = replace(
                    record(-1001, message_id, sent_offset=message_id),
                    text=text,
                    normalized_text=text,
                )
                self.assertTrue(database.insert_message(item, enqueue_analysis=True, now=now))
                stored = database.get_message(-1001, message_id)
                self.assertEqual(stored["prefilter_status"], "not_evaluated")
                self.assertEqual(stored["analysis_queue_state"], "queued")
                self.assertTrue(stored["analysis_queue_requested"])
            self.assertEqual(
                database.connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0],
                len(texts),
            )
            database.close()

    def test_per_chat_order_fairness_retry_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            now = utc_now()
            self.assertTrue(database.insert_message(record(-1001, 1), enqueue_analysis=True, now=now))
            self.assertTrue(database.insert_message(record(-1001, 2, sent_offset=1), enqueue_analysis=True, now=now))
            self.assertTrue(database.insert_message(record(-2002, 1), enqueue_analysis=True, now=now))
            self.assertFalse(database.insert_message(record(-1001, 1), enqueue_analysis=True, now=now))
            self.assertEqual(
                database.connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0],
                3,
            )

            first = database.claim_next_analysis_job(now=now)
            self.assertEqual(first["chat_id"], -1001)
            second = database.claim_next_analysis_job(now=now)
            self.assertEqual(second["chat_id"], -2002)
            self.assertIsNone(database.claim_next_analysis_job(now=now))

            self.assertTrue(
                database.retry_analysis_job(
                    int(first["id"]),
                    now=now,
                    delay_seconds=retry_delay_seconds(int(first["attempts"])),
                    error_category="busy",
                    error_stage="classification",
                )
            )
            stats = database.analysis_queue_stats(hours=24, now=now)
            self.assertEqual(stats["retry"], 1)
            self.assertEqual(stats["health"], "degraded")
            self.assertIsNone(database.claim_next_analysis_job(now=now + timedelta(seconds=1)))

            database.finish_analysis_job(
                int(second["id"]),
                now=now,
                succeeded=True,
                result_status="filtered_non_information",
            )
            retried = database.claim_next_analysis_job(now=now + timedelta(seconds=6))
            self.assertEqual(retried["id"], first["id"])
            database.finish_analysis_job(
                int(retried["id"]),
                now=now + timedelta(seconds=6),
                succeeded=True,
                result_status="success",
            )
            next_same_chat = database.claim_next_analysis_job(now=now + timedelta(seconds=7))
            self.assertEqual(next_same_chat["chat_id"], -1001)
            self.assertEqual(next_same_chat["message_row_id"], 2)
            database.close()

    def test_four_parallel_claims_use_four_distinct_chats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            chat_ids = (-1001, -2002, -3003, -4004)
            for chat_id in chat_ids:
                self.assertTrue(
                    database.insert_message(
                        record(chat_id, 1),
                        enqueue_analysis=True,
                        now=now,
                    )
                )
                self.assertTrue(
                    database.insert_message(
                        record(chat_id, 2, sent_offset=1),
                        enqueue_analysis=True,
                        now=now,
                    )
                )

            claimed = [database.claim_next_analysis_job(now=now) for _ in range(4)]
            self.assertTrue(all(job is not None for job in claimed))
            self.assertEqual({int(job["chat_id"]) for job in claimed}, set(chat_ids))
            # Each second message remains blocked behind the in-flight job from
            # its own chat even when four workers are available.
            self.assertIsNone(database.claim_next_analysis_job(now=now))
            database.close()

    def test_batch_claim_flushes_at_two_seconds_and_never_mixes_chats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now().replace(microsecond=0)
            database.insert_message(record(-1001, 1), enqueue_analysis=True, now=now)
            database.insert_message(record(-2002, 1), enqueue_analysis=True, now=now)
            self.assertIsNone(
                database.claim_next_analysis_batch(
                    now=now + timedelta(seconds=1),
                    target_items=20,
                    max_wait_seconds=2,
                )
            )
            first = database.claim_next_analysis_batch(
                now=now + timedelta(seconds=2),
                target_items=20,
                max_wait_seconds=2,
            )
            self.assertEqual(first["item_count"], 1)
            self.assertEqual({job["chat_id"] for job in first["jobs"]}, {-1001})
            second = database.claim_next_analysis_batch(
                now=now + timedelta(seconds=2),
                target_items=20,
                max_wait_seconds=2,
            )
            self.assertEqual(second["item_count"], 1)
            self.assertEqual({job["chat_id"] for job in second["jobs"]}, {-2002})
            database.close()

    def test_fresh_low_volume_chat_does_not_block_an_old_ready_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now().replace(microsecond=0)
            database.insert_message(record(-1001, 1), enqueue_analysis=True, now=now)
            for message_id in range(1, 21):
                database.insert_message(
                    record(-2002, message_id, sent_offset=message_id),
                    enqueue_analysis=True,
                    now=now - timedelta(seconds=10),
                )
            batch = database.claim_next_analysis_batch(
                now=now + timedelta(seconds=1),
                target_items=20,
                max_wait_seconds=2,
            )
            self.assertEqual(batch["chat_id"], -2002)
            self.assertEqual(batch["item_count"], 20)
            database.close()

    def test_batch_claim_uses_target_max_items_char_budget_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now().replace(microsecond=0)
            for message_id in range(1, 61):
                database.insert_message(
                    record(-1001, message_id, sent_offset=message_id),
                    enqueue_analysis=True,
                    now=now,
                )
            batch = database.claim_next_analysis_batch(
                now=now,
                target_items=20,
                max_items=50,
                max_chars=12_000,
                max_wait_seconds=2,
            )
            self.assertEqual(batch["item_count"], 50)
            self.assertEqual(
                [job["message_row_id"] for job in batch["jobs"]],
                list(range(1, 51)),
            )
            self.assertIsNone(
                database.claim_next_analysis_batch(
                    now=now + timedelta(seconds=3),
                    target_items=20,
                    max_wait_seconds=2,
                )
            )
            database.close()

        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now().replace(microsecond=0)
            for message_id in range(1, 5):
                text = f"消息{message_id}" + ("内容" * 700)
                database.insert_message(
                    replace(
                        record(-1001, message_id, sent_offset=message_id),
                        text=text,
                        normalized_text=text,
                    ),
                    enqueue_analysis=True,
                    now=now - timedelta(seconds=5),
                )
            batch = database.claim_next_analysis_batch(
                now=now,
                target_items=1,
                max_items=50,
                max_chars=3_000,
                max_wait_seconds=2,
            )
            self.assertEqual(batch["item_count"], 2)
            self.assertLessEqual(batch["input_chars"], 3_000)
            database.close()

    def test_restart_recovers_processing_and_legacy_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            now = utc_now()
            database.insert_message(record(-1001, 1), enqueue_analysis=True, now=now)
            claimed = database.claim_next_analysis_job(now=now)
            self.assertEqual(claimed["state"], "processing")
            database.insert_message(record(-2002, 2))
            with database.connection:
                database.connection.execute(
                    "UPDATE messages SET ai_status = 'pending' WHERE chat_id = -2002"
                )
            database.close()

            reopened = Database(path)
            recovered = reopened.recover_analysis_jobs(now=now + timedelta(seconds=1))
            self.assertEqual(recovered, {"interrupted": 1, "adopted": 1})
            self.assertEqual(
                reopened.connection.execute(
                    "SELECT COUNT(*) FROM analysis_jobs WHERE state = 'retry'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                reopened.connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE ai_status = 'retry'"
                ).fetchone()[0],
                2,
            )
            reopened.close()

    def test_expired_lease_is_visible_as_retry_when_another_chat_is_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            now = utc_now()
            database.insert_message(record(-1001, 1), enqueue_analysis=True, now=now)
            database.insert_message(record(-2002, 1), enqueue_analysis=True, now=now)
            first = database.claim_next_analysis_job(now=now, lease_seconds=30)
            self.assertEqual(first["chat_id"], -1001)

            second = database.claim_next_analysis_job(
                now=now + timedelta(seconds=31),
                lease_seconds=30,
            )
            self.assertEqual(second["chat_id"], -2002)
            interrupted = database.get_message(-1001, 1)
            self.assertEqual(interrupted["analysis_queue_state"], "retry")
            self.assertEqual(interrupted["analysis_queue_error_category"], "interrupted")
            self.assertFalse(interrupted["push_eligible"])
            database.close()

    def test_successful_live_job_atomically_creates_channel_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_runtime_config(
                important_keywords=(),
                trusted_sender_ids=frozenset(),
                watch_chat_ids=frozenset({-1001}),
                immediate_score=80,
                now=now,
            )
            database.initialize_push_config(
                telegram_bot_token="test-token",
                telegram_chat_id="123",
                now=now,
            )
            database.insert_message(record(-1001, 1), enqueue_analysis=True, now=now)
            claimed = database.claim_next_analysis_job(now=now)
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET ai_status = 'success', ai_category = 'external_information',
                        ai_score = 60, score = 60, push_eligible = 1,
                        push_gate_reason = 'eligible',
                        notification_prepare_status = 'success'
                    WHERE chat_id = -1001 AND message_id = 1
                    """
                )
            created = database.finish_analysis_job(
                int(claimed["id"]),
                now=now,
                succeeded=True,
                result_status="success",
            )
            self.assertEqual(created, 1)
            row = database.get_message(-1001, 1)
            self.assertEqual(row["analysis_queue_state"], "succeeded")
            self.assertEqual(
                [(item["channel"], item["state"]) for item in row["deliveries"]],
                [("telegram", "queued")],
            )
            self.assertEqual(
                database.finish_analysis_job(
                    int(claimed["id"]),
                    now=now,
                    succeeded=True,
                    result_status="success",
                ),
                0,
            )
            database.close()

    def test_schema_migration_is_idempotent_and_history_is_not_enqueued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            database.insert_message(record(-1001, 1))
            database.close()
            Database(path).close()
            reopened = Database(path)
            tables = {
                row["name"]
                for row in reopened.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {
                    "analysis_jobs",
                    "analysis_chat_schedule",
                    "analysis_batches",
                    "analysis_batch_items",
                    "analysis_batch_calls",
                    "deliveries",
                }
                <= tables
            )
            self.assertEqual(
                reopened.connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0],
                0,
            )
            reopened.close()


class DeliveryLedgerTests(unittest.TestCase):
    def test_digest_initial_delay_uses_persisted_clock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now().replace(microsecond=0)
            database.initialize_digest_clock(now - timedelta(minutes=20))
            self.assertEqual(
                database.digest_initial_delay(now, interval_seconds=15 * 60),
                0,
            )
            with database.connection:
                database.connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'last_digest_at'",
                    (to_iso(now - timedelta(minutes=5)),),
                )
            self.assertEqual(
                database.digest_initial_delay(now, interval_seconds=15 * 60),
                10 * 60,
            )
            database.close()

    def _configured_database(self, path: str) -> Database:
        database = Database(path)
        now = utc_now()
        database.initialize_push_config(
            telegram_bot_token="test-token",
            telegram_chat_id="123",
            now=now,
        )
        database.update_push_config(
            telegram_enabled=True,
            telegram_bot_token="",
            clear_telegram_bot_token=False,
            telegram_chat_id="123",
            ntfy_enabled=True,
            ntfy_base_url="https://push.example.test",
            ntfy_topic="news",
            ntfy_access_token="",
            clear_ntfy_access_token=False,
            now=now,
        )
        return database

    def test_immediate_partial_success_recovers_per_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._configured_database(f"{directory}/messages.db")
            now = utc_now()
            database.insert_message(record(-1001, 1))
            row = database.get_message(-1001, 1)
            mark_eligible(database, row["id"])
            self.assertEqual(
                database.enqueue_immediate_deliveries(-1001, 1, now=now),
                2,
            )
            self.assertEqual(
                database.enqueue_immediate_deliveries(-1001, 1, now=now),
                0,
            )

            telegram = database.claim_next_delivery_unit(now=now)
            self.assertEqual(telegram["channel"], "telegram")
            database.finish_delivery_unit(telegram["ids"], now=now, succeeded=True)
            ntfy = database.claim_next_delivery_unit(now=now)
            self.assertEqual(ntfy["channel"], "ntfy")
            self.assertTrue(
                database.retry_delivery_unit(
                    ntfy["ids"],
                    now=now,
                    delay_seconds=5,
                    error_category="network_error",
                )
            )
            partial = database.get_message(-1001, 1)
            self.assertIsNone(partial["immediate_pushed_at"])
            states = {item["channel"]: item["state"] for item in partial["deliveries"]}
            self.assertEqual(states, {"ntfy": "retry", "telegram": "succeeded"})
            delivery_stats = database.delivery_queue_stats(hours=24, now=now)
            self.assertEqual(delivery_stats["retry"], 1)
            self.assertEqual(delivery_stats["health"], "degraded")
            self.assertIn(
                "network_error",
                {item["category"] for item in delivery_stats["error_categories"]},
            )

            recovered = database.claim_next_delivery_unit(now=now + timedelta(seconds=6))
            database.finish_delivery_unit(
                recovered["ids"], now=now + timedelta(seconds=6), succeeded=True
            )
            complete = database.get_message(-1001, 1)
            self.assertIsNotNone(complete["immediate_pushed_at"])
            self.assertTrue(all(item["state"] == "succeeded" for item in complete["deliveries"]))

    def test_recovery_cutoff_blocks_historical_delivery_but_allows_new_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._configured_database(f"{directory}/messages.db")
            cutoff = utc_now().replace(microsecond=0)
            with database.connection:
                database.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES(?, ?)",
                    ("recovery_delivery_cutoff_at", to_iso(cutoff)),
                )

            for message_id, offset in ((1, -60), (2, 60), (3, -60), (4, 60)):
                database.insert_message(record(-1001, message_id, sent_offset=offset))
                row = database.get_message(-1001, message_id)
                mark_eligible(database, row["id"], score=72)

            self.assertEqual(
                database.enqueue_immediate_deliveries(-1001, 1, now=cutoff),
                0,
            )
            self.assertEqual(
                database.enqueue_immediate_deliveries(-1001, 2, now=cutoff),
                2,
            )
            self.assertIsNone(database.reserve_immediate(-1001, 3, 60, cutoff))
            self.assertIsNotNone(database.reserve_immediate(-1001, 4, 60, cutoff))

            database.insert_message(record(-1001, 5, sent_offset=-60))
            database.insert_message(record(-1001, 6, sent_offset=60))
            for message_id in (5, 6):
                row = database.get_message(-1001, message_id)
                mark_eligible(database, row["id"], score=72)
            candidates = database.digest_window_candidates(cutoff + timedelta(minutes=2))
            self.assertNotIn(5, {row["message_id"] for row in candidates})
            self.assertIn(6, {row["message_id"] for row in candidates})
            database.close()

    def test_digest_batch_partial_failure_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._configured_database(f"{directory}/messages.db")
            now = utc_now()
            candidates = []
            for message_id in (1, 2):
                database.insert_message(record(-1001, message_id, sent_offset=message_id))
                row = database.get_message(-1001, message_id)
                mark_eligible(database, row["id"], score=70 + message_id)
                candidates.append(database.get_message(-1001, message_id))
            self.assertEqual(
                database.enqueue_digest_deliveries(candidates, candidates, cutoff=now),
                4,
            )
            self.assertEqual(
                database.enqueue_digest_deliveries(candidates, candidates, cutoff=now),
                0,
            )

            telegram_batch = database.claim_next_delivery_unit(now=now)
            self.assertEqual(telegram_batch["channel"], "telegram")
            self.assertEqual(len(telegram_batch["rows"]), 2)
            database.finish_delivery_unit(telegram_batch["ids"], now=now, succeeded=True)

            first_ntfy = database.claim_next_delivery_unit(now=now)
            self.assertEqual(first_ntfy["channel"], "ntfy")
            self.assertEqual(len(first_ntfy["rows"]), 1)
            database.finish_delivery_unit(first_ntfy["ids"], now=now, succeeded=True)
            second_ntfy = database.claim_next_delivery_unit(now=now)
            self.assertTrue(
                database.retry_delivery_unit(
                    second_ntfy["ids"],
                    now=now,
                    delay_seconds=5,
                    error_category="network_error",
                )
            )
            self.assertIsNone(database.get_message(-1001, 2)["digest_pushed_at"])
            retried = database.claim_next_delivery_unit(now=now + timedelta(seconds=6))
            database.finish_delivery_unit(
                retried["ids"], now=now + timedelta(seconds=6), succeeded=True
            )
            self.assertIsNotNone(database.get_message(-1001, 2)["digest_pushed_at"])
            database.close()

    def test_realtime_migration_retires_only_unsent_legacy_digest_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._configured_database(f"{directory}/messages.db")
            now = utc_now()
            database.insert_message(record(-1001, 1))
            row = database.get_message(-1001, 1)
            mark_eligible(database, row["id"], score=72)
            candidate = database.get_message(-1001, 1)
            self.assertEqual(
                database.enqueue_digest_deliveries([candidate], [candidate], cutoff=now),
                2,
            )
            delivered = database.claim_next_delivery_unit(now=now)
            database.finish_delivery_unit(delivered["ids"], now=now, succeeded=True)

            self.assertEqual(database.retire_pending_digest_deliveries(), 1)
            remaining = database.get_deliveries(row["id"])
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["state"], "succeeded")
            self.assertEqual(remaining[0]["delivery_type"], "digest")
            database.close()

    def test_digest_window_is_not_consumed_without_a_configured_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_digest_clock(now - timedelta(minutes=15))
            database.insert_message(record(-1001, 1))
            row = database.get_message(-1001, 1)
            mark_eligible(database, row["id"], score=72)
            candidate = database.get_message(-1001, 1)
            before = database.connection.execute(
                "SELECT value FROM metadata WHERE key = 'last_digest_at'"
            ).fetchone()["value"]

            self.assertEqual(
                database.enqueue_digest_deliveries([candidate], [candidate], cutoff=now),
                0,
            )
            after = database.connection.execute(
                "SELECT value FROM metadata WHERE key = 'last_digest_at'"
            ).fetchone()["value"]
            self.assertEqual(after, before)
            self.assertIsNone(database.get_message(-1001, 1)["digest_considered_at"])
            database.close()

    def test_digest_window_requires_ai_score_at_least_sixty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = self._configured_database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_digest_clock(now - timedelta(minutes=15))
            for message_id, score in ((1, 59), (2, 60)):
                database.insert_message(record(-1001, message_id, sent_offset=message_id))
                row = database.get_message(-1001, message_id)
                mark_eligible(database, row["id"], score=score)

            candidates = database.digest_window_candidates(now + timedelta(minutes=1))

            self.assertEqual([row["message_id"] for row in candidates], [2])
            database.close()


class MaintenanceLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_maintenance_cleans_without_creating_digest_deliveries(self) -> None:
        stop_event = asyncio.Event()

        class StubDatabase:
            cleanup_calls = 0

            def cleanup(self, *_: object) -> int:
                self.cleanup_calls += 1
                stop_event.set()
                return 0

        database = StubDatabase()
        await asyncio.wait_for(
            maintenance_loop(
                SimpleNamespace(retention_days=3),
                database,
                stop_event,
            ),
            timeout=0.5,
        )
        self.assertEqual(database.cleanup_calls, 1)


class StubModelClient:
    async def classify(self, **_: object) -> ClassificationOutcome:
        return ClassificationOutcome(
            status="success",
            model="classification-model",
            category="external_information",
            confidence=96,
            summary="外部更新",
            reason="有明确产品事件",
            response_text='{"category":"external_information"}',
            effort="low",
        )

    async def score(self, **_: object) -> ScoringOutcome:
        return ScoringOutcome(
            status="success",
            model="scoring-model",
            score=87,
            summary="产品安全更新已发布",
            reason="信息具体且具有时效性",
            response_text='{"score":87}',
            effort="default",
        )


class StructuredResponseModelClient:
    def __init__(self, *, always_invalid: bool = False) -> None:
        self.always_invalid = always_invalid
        self.classification_calls = 0

    async def classify(self, **_: object) -> ClassificationOutcome:
        self.classification_calls += 1
        if self.always_invalid or self.classification_calls == 1:
            return ClassificationOutcome(
                status="error",
                model="classification-model",
                response_text="not-json",
                error_category="invalid_response",
                effort="low",
            )
        return ClassificationOutcome(
            status="success",
            model="classification-model",
            category="external_information",
            confidence=95,
            summary="公开产品更新",
            reason="消息包含明确产品事件",
            response_text='{"category":"external_information"}',
            effort="low",
        )

    async def score(self, **_: object) -> ScoringOutcome:
        return ScoringOutcome(
            status="success",
            model="scoring-model",
            score=55,
            summary="普通产品更新",
            reason="影响有限，低于实时推送线",
            response_text='{"score":55}',
            effort="default",
        )


class BatchModelClient:
    def __init__(
        self,
        *,
        split_above: int | None = None,
        transient_once: bool = False,
        partial_once: bool = False,
        score: int = 55,
    ) -> None:
        self.split_above = split_above
        self.transient_once = transient_once
        self.partial_once = partial_once
        self.score_value = score
        self.batch_calls: list[tuple[int, ...]] = []
        self.single_classification_calls = 0
        self.score_calls = 0

    async def classify(self, **_: object) -> ClassificationOutcome:
        self.single_classification_calls += 1
        raise AssertionError("批量存活项不得再次逐条分类")

    async def classify_batch(self, *, messages, **_: object) -> BatchClassificationOutcome:
        row_ids = tuple(int(item["message_row_id"]) for item in messages)
        self.batch_calls.append(row_ids)
        if self.transient_once and len(self.batch_calls) == 1:
            return BatchClassificationOutcome(
                status="error",
                model="classification-model",
                outcomes={},
                unresolved_row_ids=row_ids,
                error_category="network_error",
                effort="low",
                latency_ms=10,
            )
        if self.split_above is not None and len(messages) > self.split_above:
            return BatchClassificationOutcome(
                status="error",
                model="classification-model",
                outcomes={},
                unresolved_row_ids=row_ids,
                response_text="invalid batch json",
                error_category="invalid_response",
                effort="low",
                latency_ms=5,
            )
        selected_messages = tuple(messages)
        unresolved: tuple[int, ...] = ()
        if self.partial_once and len(self.batch_calls) == 1 and len(messages) > 1:
            midpoint = len(messages) // 2
            selected_messages = tuple(messages[:midpoint])
            unresolved = tuple(
                int(item["message_row_id"]) for item in messages[midpoint:]
            )
        outcomes = {}
        for index, item in enumerate(reversed(selected_messages)):
            row_id = int(item["message_row_id"])
            category = "external_information" if row_id % 20 == 0 else "discussion"
            outcomes[row_id] = ClassificationOutcome(
                status="success",
                model="classification-model",
                category=category,
                confidence=94,
                summary=f"分类 {index}",
                reason="批量分类结果",
                response_text=f'{{"message_row_id":{row_id}}}',
                effort="low",
            )
        return BatchClassificationOutcome(
            status="partial" if unresolved else "success",
            model="classification-model",
            outcomes=outcomes,
            unresolved_row_ids=unresolved,
            response_text="one shared raw batch response",
            error_category="invalid_response" if unresolved else None,
            effort="low",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            latency_ms=20,
        )

    async def score(self, **_: object) -> ScoringOutcome:
        self.score_calls += 1
        return ScoringOutcome(
            status="success",
            model="scoring-model",
            score=self.score_value,
            summary="普通产品更新",
            reason="低于推送线",
            response_text='{"score":55}',
            effort="default",
        )

    async def prepare_notification(self, **_: object) -> NotificationPreparationOutcome:
        return NotificationPreparationOutcome(
            status="success",
            model="notification-model",
            title="客户更新标题",
            body="客户更新正文",
            response_text='{"title":"客户更新标题","body":"客户更新正文"}',
            effort="low",
        )

class AnalysisQueueWorkerTests(unittest.IsolatedAsyncioTestCase):
    def _create_batch_database(self, path: str, *, count: int) -> None:
        database = Database(path)
        now = utc_now() - timedelta(seconds=5)
        database.initialize_runtime_config(
            important_keywords=("安全",),
            trusted_sender_ids=frozenset(),
            watch_chat_ids=frozenset({-1001}),
            immediate_score=60,
            now=now,
        )
        database.update_model_config(
            enabled=True,
            base_url="http://model.test/v1",
            classification_model="classification-model",
            model="scoring-model",
            api_key="test-key",
            clear_api_key=False,
            now=now,
        )
        for message_id in range(1, count + 1):
            text = f"普通讨论内容编号 {message_id}"
            database.insert_message(
                replace(
                    record(-1001, message_id, sent_offset=message_id),
                    text=text,
                    normalized_text=text,
                ),
                enqueue_analysis=True,
                now=now,
            )
        database.close()

    async def test_twenty_items_use_one_batch_call_and_survivor_reuses_classification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            self._create_batch_database(path, count=20)
            client = BatchModelClient()
            queue = PersistentAnalysisQueue(
                path,
                client,
                worker_count=1,
                batch_target=20,
                batch_max_items=50,
                batch_max_chars=12_000,
                batch_max_wait_seconds=2,
            )
            batch = await queue._claim_batch()
            self.assertEqual(batch["item_count"], 20)
            await queue._process_batch(batch)

            database = Database(path, initialize=False)
            try:
                self.assertEqual(len(client.batch_calls), 1)
                self.assertEqual(len(client.batch_calls[0]), 20)
                self.assertEqual(client.batch_calls[0], tuple(range(1, 21)))
                self.assertEqual(client.single_classification_calls, 0)
                self.assertEqual(client.score_calls, 1)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM analysis_jobs WHERE state = 'succeeded'"
                    ).fetchone()[0],
                    20,
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM deliveries"
                    ).fetchone()[0],
                    0,
                )
                audit = database.connection.execute(
                    "SELECT * FROM analysis_batches WHERE id = ?", (int(batch["id"]),)
                ).fetchone()
                self.assertEqual(audit["state"], "succeeded")
                self.assertEqual(audit["call_count"], 1)
                self.assertEqual(audit["split_count"], 0)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM analysis_batch_calls "
                        "WHERE response_text = 'one shared raw batch response'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM messages "
                        "WHERE ai_category_response_text = 'one shared raw batch response'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                database.close()

    async def test_worker_local_short_filter_runs_before_batch_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            self._create_batch_database(path, count=20)
            database = Database(path, initialize=False)
            with database.connection:
                database.connection.execute(
                    "UPDATE messages SET text = '嗯', normalized_text = '嗯' WHERE id = 1"
                )
            database.close()
            client = BatchModelClient()
            queue = PersistentAnalysisQueue(
                path,
                client,
                worker_count=1,
                batch_target=20,
                batch_max_wait_seconds=0,
            )
            batch = await queue._claim_batch()
            await queue._process_batch(batch)
            self.assertEqual(len(client.batch_calls), 1)
            self.assertEqual(len(client.batch_calls[0]), 19)
            self.assertNotIn(1, client.batch_calls[0])
            database = Database(path, initialize=False)
            try:
                filtered = database.get_message_by_id(1)
                self.assertEqual(filtered["ai_status"], "prefiltered")
                self.assertEqual(filtered["prefilter_reason_code"], "short_unprotected_text")
            finally:
                database.close()

    async def test_batch_completion_keeps_delivery_creation_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            self._create_batch_database(path, count=20)
            database = Database(path, initialize=False)
            database.initialize_push_config(
                telegram_bot_token="test-token",
                telegram_chat_id="123",
                now=utc_now(),
            )
            database.close()
            client = BatchModelClient(score=65)
            queue = PersistentAnalysisQueue(
                path,
                client,
                worker_count=1,
                batch_target=20,
                batch_max_wait_seconds=0,
            )
            batch = await queue._claim_batch()
            await queue._process_batch(batch)
            database = Database(path, initialize=False)
            try:
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM deliveries"
                    ).fetchone()[0],
                    1,
                )
                external_job = database.connection.execute(
                    """
                    SELECT job.id FROM analysis_jobs AS job
                    JOIN messages AS message ON message.id = job.message_row_id
                    WHERE message.ai_category = 'external_information'
                    """
                ).fetchone()
                self.assertEqual(
                    database.finish_analysis_job(
                        int(external_job["id"]),
                        now=utc_now(),
                        succeeded=True,
                        result_status="success",
                    ),
                    0,
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM deliveries"
                    ).fetchone()[0],
                    1,
                )
            finally:
                database.close()

    async def test_invalid_batch_is_bisected_and_successful_subsets_are_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            self._create_batch_database(path, count=4)
            client = BatchModelClient(split_above=2)
            queue = PersistentAnalysisQueue(
                path,
                client,
                worker_count=1,
                batch_target=4,
                batch_max_items=50,
                batch_max_wait_seconds=0,
            )
            batch = await queue._claim_batch()
            await queue._process_batch(batch)
            self.assertEqual([len(call) for call in client.batch_calls], [4, 2, 2])
            flattened = [row_id for call in client.batch_calls[1:] for row_id in call]
            self.assertEqual(sorted(flattened), sorted(client.batch_calls[0]))
            database = Database(path, initialize=False)
            try:
                audit = database.connection.execute(
                    "SELECT call_count, split_count, state FROM analysis_batches WHERE id = ?",
                    (int(batch["id"]),),
                ).fetchone()
                self.assertEqual(tuple(audit), (3, 1, "succeeded"))
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM analysis_jobs WHERE state = 'succeeded'"
                    ).fetchone()[0],
                    4,
                )
            finally:
                database.close()

    async def test_partial_batch_only_retries_the_missing_subset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            self._create_batch_database(path, count=4)
            client = BatchModelClient(partial_once=True)
            queue = PersistentAnalysisQueue(
                path,
                client,
                worker_count=1,
                batch_target=4,
                batch_max_wait_seconds=0,
            )
            batch = await queue._claim_batch()
            await queue._process_batch(batch)
            self.assertEqual([len(call) for call in client.batch_calls], [4, 1, 1])
            self.assertEqual(
                client.batch_calls[1] + client.batch_calls[2],
                client.batch_calls[0][2:],
            )
            for row_id in client.batch_calls[0][:2]:
                self.assertEqual(
                    sum(row_id in call for call in client.batch_calls),
                    1,
                )
            database = Database(path, initialize=False)
            try:
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM analysis_jobs WHERE state = 'succeeded'"
                    ).fetchone()[0],
                    4,
                )
            finally:
                database.close()

    async def test_transient_batch_error_retries_as_one_durable_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            self._create_batch_database(path, count=2)
            client = BatchModelClient(transient_once=True)
            queue = PersistentAnalysisQueue(
                path,
                client,
                worker_count=1,
                batch_target=2,
                batch_max_wait_seconds=0,
            )
            first = await queue._claim_batch()
            await queue._process_batch(first)
            database = Database(path, initialize=False)
            with database.connection:
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM analysis_jobs WHERE state = 'retry'"
                    ).fetchone()[0],
                    2,
                )
                database.connection.execute(
                    "UPDATE analysis_jobs SET available_at = ? WHERE state = 'retry'",
                    (to_iso(utc_now() - timedelta(seconds=1)),),
                )
            database.close()
            second = await queue._claim_batch()
            await queue._process_batch(second)
            database = Database(path, initialize=False)
            try:
                self.assertEqual(len(client.batch_calls), 2)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM analysis_jobs "
                        "WHERE state = 'succeeded' AND attempts = 2"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(DISTINCT generation) FROM analysis_jobs"
                    ).fetchone()[0],
                    1,
                )
            finally:
                database.close()

    async def test_restart_reuses_persisted_batch_classification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            self._create_batch_database(path, count=2)
            client = BatchModelClient()
            queue = PersistentAnalysisQueue(
                path,
                client,
                worker_count=1,
                batch_target=2,
                batch_max_wait_seconds=0,
            )
            first = await queue._claim_batch()
            database = Database(path, initialize=False)
            try:
                config = model_runtime_config_from_value(
                    database.get_model_config(include_api_key=True)
                )
                jobs_by_row = {
                    int(job["message_row_id"]): job for job in first["jobs"]
                }
                items = []
                for job in first["jobs"]:
                    row = database.get_message_by_id(int(job["message_row_id"]))
                    preflight = preflight_persisted_message_for_batch(
                        database=database,
                        row=row,
                        now=utc_now(),
                    )
                    self.assertTrue(preflight.ready)
                    items.append(
                        {
                            "message_row_id": int(row["id"]),
                            "message_id": int(row["message_id"]),
                            "time": str(row["sent_at"]),
                            "text": str(row["text"]),
                        }
                    )
                outcomes, _ = await queue._classify_batch_subset(
                    database=database,
                    batch_id=int(first["id"]),
                    jobs_by_row_id=jobs_by_row,
                    items=items,
                    config=config,
                    session_key=database.llm_session_key(-1001),
                    recent_context=(),
                )
                self.assertEqual(len(outcomes), 2)
                recovered = database.recover_analysis_jobs(now=utc_now())
                self.assertEqual(recovered["interrupted"], 2)
            finally:
                database.close()

            second = await queue._claim_batch()
            await queue._process_batch(second)
            self.assertEqual(len(client.batch_calls), 1)
            database = Database(path, initialize=False)
            try:
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM analysis_jobs WHERE state = 'succeeded'"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM analysis_batch_calls"
                    ).fetchone()[0],
                    1,
                )
            finally:
                database.close()

    async def _run_structured_response_case(self, *, always_invalid: bool) -> tuple[dict, int]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = f"{directory.name}/messages.db"
        database = Database(path)
        now = utc_now()
        database.initialize_runtime_config(
            important_keywords=("安全",),
            trusted_sender_ids=frozenset(),
            watch_chat_ids=frozenset({-1001}),
            immediate_score=80,
            now=now,
        )
        database.update_model_config(
            enabled=True,
            base_url="http://model.test/v1",
            classification_model="classification-model",
            model="scoring-model",
            api_key="test-key",
            clear_api_key=False,
            now=now,
        )
        database.insert_message(record(-1001, 1), enqueue_analysis=True, now=now)
        database.close()

        client = StructuredResponseModelClient(always_invalid=always_invalid)
        queue = PersistentAnalysisQueue(path, client, worker_count=1)
        first = await queue._claim()
        self.assertIsNotNone(first)
        await queue._process(first)

        probe = Database(path, initialize=False)
        with probe.connection:
            probe.connection.execute(
                "UPDATE analysis_jobs SET available_at = ? WHERE id = ?",
                (to_iso(utc_now() - timedelta(seconds=1)), int(first["id"])),
            )
        probe.close()
        second = await queue._claim()
        self.assertIsNotNone(second)
        await queue._process(second)

        probe = Database(path, initialize=False)
        try:
            job = probe.get_analysis_job(int(first["id"]))
            message = probe.get_message(-1001, 1)
        finally:
            probe.close()
        self.assertIsNotNone(job)
        self.assertIsNotNone(message)
        return {"job": job, "message": message}, client.classification_calls

    async def test_invalid_response_recovers_on_single_retry(self) -> None:
        result, calls = await self._run_structured_response_case(always_invalid=False)
        self.assertEqual(calls, 2)
        self.assertEqual(result["job"]["state"], "succeeded")
        self.assertEqual(result["job"]["attempts"], 2)
        self.assertEqual(result["message"]["ai_status"], "success")
        self.assertEqual(result["message"]["ai_score"], 55)
        self.assertFalse(result["message"]["push_eligible"])

    async def test_repeated_invalid_response_stops_after_two_attempts(self) -> None:
        result, calls = await self._run_structured_response_case(always_invalid=True)
        self.assertEqual(calls, 2)
        self.assertEqual(result["job"]["state"], "failed")
        self.assertEqual(result["job"]["attempts"], 2)
        self.assertEqual(result["job"]["error_category"], "invalid_response")
        self.assertFalse(result["message"]["push_eligible"])

    async def test_manual_job_runs_full_pipeline_but_never_becomes_push_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            now = utc_now()
            database.initialize_runtime_config(
                important_keywords=("安全",),
                trusted_sender_ids=frozenset(),
                watch_chat_ids=frozenset({-1001}),
                immediate_score=80,
                now=now,
            )
            database.update_model_config(
                enabled=True,
                base_url="http://model.test/v1",
                classification_model="classification-model",
                model="scoring-model",
                api_key="test-key",
                clear_api_key=False,
                now=now,
            )
            database.insert_message(record(-1001, 1))
            row = database.get_message(-1001, 1)
            _, created = database.enqueue_manual_analysis(row["id"], now=now)
            self.assertTrue(created)
            database.close()

            queue = PersistentAnalysisQueue(path, StubModelClient(), worker_count=1)
            await queue.start()
            try:
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    probe = Database(path, initialize=False)
                    try:
                        result = probe.get_message(-1001, 1)
                    finally:
                        probe.close()
                    if result["analysis_queue_state"] == "succeeded":
                        break
                else:
                    self.fail("manual analysis queue did not complete")
            finally:
                await queue.close()
            self.assertEqual(result["ai_status"], "success")
            self.assertEqual(result["ai_score"], 87)
            self.assertFalse(result["push_eligible"])
            self.assertEqual(result["push_gate_reason"], "manual_reanalysis")


if __name__ == "__main__":
    unittest.main()
