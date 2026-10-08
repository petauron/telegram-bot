from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.config import DEFAULT_KEYWORDS, Settings
from app.database import Database, MessageRecord, to_iso, utc_now
from app.dedupe import select_digest_items, select_semantic_candidates
from app.llm import DEFAULT_CLASSIFICATION_MODEL, ClassificationOutcome
from app.llm_context import (
    RECENT_CONTEXT_CHAR_LIMIT,
    RECENT_CONTEXT_LIMIT,
    context_character_count,
)
from app.main import MessageProcessor, heartbeat_loop, sync_available_chats
from app.push import _utf16_units, digest_chunks, immediate_chunks
from app.scoring import reply_bonus, score_message


class ScoringTests(unittest.TestCase):
    def test_default_interests_use_specific_news_signals(self) -> None:
        keywords = tuple(DEFAULT_KEYWORDS.split(","))
        for value in ("服务中断", "数据泄露", "重大更新", "封禁政策"):
            self.assertIn(value, keywords)
        for value in ("紧急", "封禁", "异常", "截止"):
            self.assertNotIn(value, keywords)

    def test_configured_rules_add_up(self) -> None:
        result = score_message(
            "紧急维护：CVE-2026-12345，今晚 23:30 升级 v2.3.1，详情 https://example.com/a",
            mentioned_me=False,
            reply_to_me=False,
            trusted_sender=False,
            keywords=("紧急", "维护", "CVE", "升级"),
        )
        self.assertEqual(result.score, 95)
        self.assertEqual(result.primary_url, "https://example.com/a")

    def test_reply_bonus_thresholds(self) -> None:
        self.assertEqual(reply_bonus(1), 0)
        self.assertEqual(reply_bonus(2), 10)
        self.assertEqual(reply_bonus(3), 20)
        self.assertEqual(reply_bonus(5), 30)


class SettingsTests(unittest.TestCase):
    def test_analysis_workers_defaults_to_eight_and_accepts_override(self) -> None:
        required = {
            "TG_API_ID": "12345",
            "TG_API_HASH": "test-api-hash",
            "TG_PHONE": "+10000000000",
        }
        with patch.dict("os.environ", required, clear=True):
            settings = Settings.from_env(require_push=False, require_watch=False)
            self.assertEqual(settings.analysis_workers, 8)
            self.assertEqual(
                (
                    settings.analysis_batch_target,
                    settings.analysis_batch_max_items,
                    settings.analysis_batch_max_chars,
                    settings.analysis_batch_max_wait_seconds,
                ),
                (20, 50, 12_000, 2),
            )
        with patch.dict(
            "os.environ",
            {**required, "ANALYSIS_WORKERS": "6"},
            clear=True,
        ):
            self.assertEqual(
                Settings.from_env(require_push=False, require_watch=False).analysis_workers,
                6,
            )
        with patch.dict(
            "os.environ",
            {**required, "ANALYSIS_WORKERS": "0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "ANALYSIS_WORKERS"):
                Settings.from_env(require_push=False, require_watch=False)

        with patch.dict(
            "os.environ",
            {
                **required,
                "ANALYSIS_BATCH_TARGET": "21",
                "ANALYSIS_BATCH_MAX_ITEMS": "20",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "ANALYSIS_BATCH_TARGET"):
                Settings.from_env(require_push=False, require_watch=False)


class MessageProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_persists_queue_history_without_a_web_client(self) -> None:
        stop_event = asyncio.Event()

        class StubDatabase:
            heartbeat_at = None
            sampled_at = None

            def get_runtime_config(self):
                return {"watch_chat_ids": frozenset({-1001, -1002})}

            def set_listener_heartbeat(self, *, connected, self_id, watch_count, now):
                self.heartbeat_at = now
                self.asserted = (connected, self_id, watch_count)

            def record_queue_metric_sample(self, *, now):
                self.sampled_at = now
                stop_event.set()

        database = StubDatabase()
        await heartbeat_loop(
            SimpleNamespace(watch_chat_ids=frozenset()),
            database,
            SimpleNamespace(is_connected=lambda: True),
            42,
            stop_event,
        )
        self.assertEqual(database.asserted, (True, 42, 2))
        self.assertEqual(database.sampled_at, database.heartbeat_at)

    async def test_textless_service_event_is_persisted_for_prefilter_audit(self) -> None:
        class StubDatabase:
            inserted: MessageRecord | None = None
            enqueue_analysis = False

            def get_runtime_config(self):
                return {
                    "important_keywords": (),
                    "trusted_sender_ids": frozenset(),
                    "watch_chat_ids": frozenset({-1001}),
                    "immediate_score": 80,
                }

            def get_message(self, *_):
                return None

            def count_recent_normalized(self, *_):
                return 0

            def thread_root_for(self, *_):
                return None

            def insert_message(self, record, *, enqueue_analysis, now):
                _ = now
                self.inserted = record
                self.enqueue_analysis = enqueue_analysis
                return True

        class StubQueue:
            awake = False

            def wake(self):
                self.awake = True

        now = utc_now()
        event = SimpleNamespace(
            chat_id=-1001,
            sender_id=7,
            out=False,
            message=SimpleNamespace(
                id=41,
                raw_text="",
                media=None,
                file=None,
                action=SimpleNamespace(),
                reply_to_msg_id=None,
                date=now,
                mentioned=False,
            ),
        )

        async def get_chat():
            return SimpleNamespace(title="去标识会话", username=None)

        async def get_sender():
            return SimpleNamespace(first_name="匿名", last_name=None, username=None)

        event.get_chat = get_chat
        event.get_sender = get_sender
        database = StubDatabase()
        analysis_queue = StubQueue()
        processor = MessageProcessor(
            settings=SimpleNamespace(
                important_keywords=(),
                trusted_sender_ids=frozenset(),
                watch_chat_ids=frozenset({-1001}),
                immediate_score=80,
            ),
            database=database,
            analysis_queue=analysis_queue,
            delivery_queue=StubQueue(),
            self_id=999,
        )
        await processor.process(event)
        self.assertIsNotNone(database.inserted)
        self.assertTrue(database.inserted.is_service_message)
        self.assertTrue(database.inserted.text.startswith("Telegram 服务事件："))
        self.assertTrue(database.enqueue_analysis)
        self.assertTrue(analysis_queue.awake)


class DatabaseTests(unittest.TestCase):
    @staticmethod
    def _context_record(
        *,
        chat_id: int,
        message_id: int,
        sent_at: str,
        text: str,
        is_service_message: bool = False,
    ) -> MessageRecord:
        return MessageRecord(
            chat_id=chat_id,
            message_id=message_id,
            chat_name="去标识群",
            chat_username=None,
            sender_id=None,
            sender_name="匿名",
            sent_at=sent_at,
            text=text,
            reply_to_message_id=None,
            thread_root_id=message_id,
            base_score=0,
            reasons=(),
            link=None,
            normalized_text=text,
            primary_url=None,
            created_at=sent_at,
            is_service_message=is_service_message,
        )

    @staticmethod
    def _mark_history(
        database: Database,
        *,
        chat_id: int,
        message_id: int,
        status: str,
        category: str | None,
    ) -> None:
        with database.connection:
            database.connection.execute(
                """
                UPDATE messages
                SET ai_status = ?, ai_category = ?, prefilter_status = 'passed'
                WHERE chat_id = ? AND message_id = ?
                """,
                (status, category, chat_id, message_id),
            )

    def test_dashboard_stats_report_information_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            for message_id in range(1, 5):
                record = MessageRecord(
                    chat_id=-1001,
                    message_id=message_id,
                    chat_name="测试群",
                    chat_username=None,
                    sender_id=message_id,
                    sender_name="发送者",
                    sent_at=to_iso(now),
                    text=f"测试消息 {message_id}",
                    reply_to_message_id=None,
                    thread_root_id=message_id,
                    base_score=90,
                    reasons=("测试规则",),
                    link=None,
                    normalized_text=f"测试消息{message_id}",
                    primary_url=None,
                    created_at=to_iso(now),
                )
                self.assertTrue(database.insert_message(record))
            with database.connection:
                database.connection.execute(
                    "UPDATE messages SET prefilter_status = 'filtered', ai_status = 'prefiltered' WHERE message_id = 1"
                )
                database.connection.execute(
                    "UPDATE messages SET prefilter_status = 'passed', ai_status = 'filtered_non_information' WHERE message_id = 2"
                )
                database.connection.execute(
                    """UPDATE messages
                       SET prefilter_status = 'passed', ai_status = 'success',
                           ai_category = 'external_information', ai_score = 90,
                           push_eligible = 1, push_gate_reason = 'eligible'
                       WHERE message_id = 3"""
                )
                database.connection.execute(
                    "UPDATE messages SET prefilter_status = 'passed', ai_status = 'error' WHERE message_id = 4"
                )
            stats = database.dashboard_stats(hours=24, now=now)
            self.assertEqual(stats["window_messages"], 4)
            self.assertEqual(stats["prefiltered_messages"], 1)
            self.assertEqual(stats["non_information_messages"], 1)
            self.assertEqual(stats["eligible_messages"], 1)
            self.assertEqual(stats["analysis_errors"], 1)
            self.assertEqual(stats["important_messages"], 1)
            database.close()

    def test_queue_metric_history_is_persisted_bounded_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            now = utc_now().replace(microsecond=0)
            self.assertTrue(
                database.insert_message(
                    self._context_record(
                        chat_id=-1001,
                        message_id=1,
                        sent_at=to_iso(now),
                        text="去标识化资讯",
                    ),
                    enqueue_analysis=True,
                    now=now,
                )
            )
            message_row_id = int(
                database.connection.execute(
                    "SELECT id FROM messages WHERE chat_id = -1001 AND message_id = 1"
                ).fetchone()["id"]
            )
            with database.connection:
                database.connection.execute(
                    """
                    INSERT INTO deliveries(
                        message_row_id, channel, delivery_type, state,
                        available_at, created_at, updated_at
                    ) VALUES(?, 'ntfy', 'immediate', 'queued', ?, ?, ?)
                    """,
                    (message_row_id, to_iso(now), to_iso(now), to_iso(now)),
                )

            database.record_queue_metric_sample(now=now - timedelta(days=4))
            current = database.record_queue_metric_sample(now=now)
            self.assertEqual(current["analysis_pending"], 1)
            self.assertEqual(current["delivery_pending"], 1)

            with database.connection:
                database.connection.execute(
                    "UPDATE analysis_jobs SET state = 'processing'"
                )
                database.connection.execute(
                    "UPDATE deliveries SET state = 'retry'"
                )
            updated = database.record_queue_metric_sample(now=now)
            self.assertEqual(updated["analysis_pending"], 0)
            self.assertEqual(updated["analysis_processing"], 1)
            self.assertEqual(updated["delivery_pending"], 0)
            self.assertEqual(updated["delivery_retry"], 1)
            database.close()

            reopened = Database(path)
            history = reopened.queue_metric_history(minutes=60, now=now)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["sampled_at"], to_iso(now))
            self.assertEqual(history[0]["analysis_processing"], 1)
            self.assertEqual(history[0]["delivery_retry"], 1)
            self.assertEqual(
                reopened.connection.execute(
                    "SELECT COUNT(*) AS count FROM queue_metric_samples"
                ).fetchone()["count"],
                1,
            )
            reopened.close()

    def test_recovery_boundary_preserves_frozen_history_during_retention_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now().replace(microsecond=0)
            preserve_through = now - timedelta(days=2)
            records = (
                (1, now - timedelta(days=3)),
                (2, now - timedelta(hours=36)),
                (3, now - timedelta(hours=12)),
            )
            for message_id, created_at in records:
                database.insert_message(
                    self._context_record(
                        chat_id=-1001,
                        message_id=message_id,
                        sent_at=to_iso(created_at),
                        text=f"去标识消息 {message_id}",
                    )
                )
                with database.connection:
                    database.connection.execute(
                        "INSERT INTO replies(chat_id, parent_message_id, sender_id, replied_at) VALUES(?, ?, ?, ?)",
                        (-1001, message_id, message_id, to_iso(created_at)),
                    )
            with database.connection:
                database.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('recovery_history_preserve_through', ?)",
                    (to_iso(preserve_through),),
                )

            self.assertEqual(database.cleanup(1, now), 1)
            remaining_messages = {
                int(row["message_id"])
                for row in database.connection.execute(
                    "SELECT message_id FROM messages ORDER BY message_id"
                )
            }
            remaining_replies = {
                int(row["parent_message_id"])
                for row in database.connection.execute(
                    "SELECT parent_message_id FROM replies ORDER BY parent_message_id"
                )
            }
            self.assertEqual(remaining_messages, {1, 3})
            self.assertEqual(remaining_replies, {1, 3})
            database.close()

    def test_unique_key_and_digest_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_digest_clock(now - timedelta(minutes=1))
            record = MessageRecord(
                chat_id=-1001,
                message_id=1,
                chat_name="测试群",
                chat_username=None,
                sender_id=2,
                sender_name="发送者",
                sent_at=to_iso(now),
                text="紧急维护",
                reply_to_message_id=None,
                thread_root_id=1,
                base_score=40,
                reasons=("重要关键词 +25",),
                link=None,
                normalized_text="紧急维护",
                primary_url=None,
                created_at=to_iso(now),
            )
            self.assertTrue(database.insert_message(record))
            self.assertFalse(database.insert_message(record))
            database.initialize_runtime_config(
                important_keywords=("紧急", "维护"),
                trusted_sender_ids=frozenset({2}),
                watch_chat_ids=frozenset({-1001}),
                immediate_score=80,
                now=now,
            )
            runtime = database.update_runtime_config(
                important_keywords=("故障", "恢复"),
                trusted_sender_ids=frozenset({2, 3}),
                watch_chat_ids=frozenset({-1001, -1002}),
                immediate_score=60,
                now=now,
            )
            self.assertEqual(runtime["important_keywords"], ("故障", "恢复"))
            self.assertEqual(runtime["watch_chat_ids"], frozenset({-1001, -1002}))
            self.assertEqual(runtime["immediate_score"], 60)
            with self.assertRaisesRegex(ValueError, "60"):
                database.update_runtime_config(
                    important_keywords=("故障",),
                    trusted_sender_ids=frozenset(),
                    watch_chat_ids=frozenset({-1001}),
                    immediate_score=80,
                    now=now,
                )
            with database.connection:
                database.connection.execute(
                    """UPDATE messages
                       SET ai_status = 'success', ai_category = 'external_information',
                           ai_score = 91, score = 91,
                           push_eligible = 1, push_gate_reason = 'eligible_unique',
                           push_ready_at = created_at,
                           semantic_dedupe_status = 'unique_no_candidates',
                           notification_prepare_status = 'success'"""
                )
            candidates = database.digest_candidates(now + timedelta(seconds=1))
            self.assertEqual(len(candidates), 1)
            database.mark_digest_considered(candidates, candidates, now)
            self.assertEqual(database.digest_candidates(now + timedelta(seconds=2)), [])
            database.close()

    def test_recent_terminal_duplicate_is_same_chat_bounded_and_conservative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()

            def add(
                message_id: int,
                *,
                chat_id: int = -1001,
                text: str = "平台发布重要安全更新并修复多个漏洞",
                age_hours: int = 0,
                primary_url: str | None = None,
            ) -> int:
                sent_at = to_iso(now - timedelta(hours=age_hours))
                database.insert_message(
                    MessageRecord(
                        chat_id=chat_id,
                        message_id=message_id,
                        chat_name="测试群",
                        chat_username=None,
                        sender_id=None,
                        sender_name="匿名",
                        sent_at=sent_at,
                        text=text,
                        reply_to_message_id=None,
                        thread_root_id=message_id,
                        base_score=0,
                        reasons=(),
                        link=None,
                        normalized_text=score_message(
                            text,
                            mentioned_me=False,
                            reply_to_me=False,
                            trusted_sender=False,
                            keywords=(),
                        ).normalized_text,
                        primary_url=primary_url,
                        created_at=sent_at,
                    )
                )
                return int(database.get_message(chat_id, message_id)["id"])

            original_id = add(1, age_hours=1)
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = 'passed', ai_status = 'success',
                        ai_category = 'external_information', ai_score = 82
                    WHERE id = ?
                    """,
                    (original_id,),
                )
            duplicate_id = add(2)
            match = database.find_recent_terminal_duplicate(duplicate_id)
            self.assertIsNotNone(match)
            self.assertEqual(match["id"], original_id)

            other_chat_id = add(3, chat_id=-2002)
            self.assertIsNone(database.find_recent_terminal_duplicate(other_chat_id))

            different_url_id = add(4, primary_url="https://example.test/different")
            self.assertIsNone(database.find_recent_terminal_duplicate(different_url_id))

            short_original_id = add(5, text="宕机", age_hours=1)
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = 'passed', ai_status = 'success',
                        ai_category = 'external_information', ai_score = 90
                    WHERE id = ?
                    """,
                    (short_original_id,),
                )
            short_repeat_id = add(6, text="宕机")
            self.assertIsNone(database.find_recent_terminal_duplicate(short_repeat_id))

            stale_text = "较早发布的固定系统状态通知内容"
            stale_original_id = add(7, text=stale_text, age_hours=80)
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = 'passed', ai_status = 'filtered_non_information',
                        ai_category = 'internal_coordination'
                    WHERE id = ?
                    """,
                    (stale_original_id,),
                )
            stale_repeat_id = add(8, text=stale_text)
            self.assertIsNone(database.find_recent_terminal_duplicate(stale_repeat_id))

            failed_original_id = add(9, text="尚未可靠完成的固定系统通知", age_hours=1)
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = 'passed', ai_status = 'error',
                        ai_category = NULL, ai_score = NULL
                    WHERE id = ?
                    """,
                    (failed_original_id,),
                )
            failed_repeat_id = add(10, text="尚未可靠完成的固定系统通知")
            self.assertIsNone(database.find_recent_terminal_duplicate(failed_repeat_id))

            future_text = "只允许使用目标消息之前的重复判断记录"
            earlier_target_id = add(11, text=future_text, age_hours=2)
            later_terminal_id = add(12, text=future_text, age_hours=1)
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = 'passed', ai_status = 'success',
                        ai_category = 'external_information', ai_score = 85
                    WHERE id = ?
                    """,
                    (later_terminal_id,),
                )
            self.assertIsNone(database.find_recent_terminal_duplicate(earlier_target_id))
            database.close()

    def test_rapid_short_duplicate_requires_same_sender_and_non_information(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()

            def add(
                message_id: int,
                *,
                text: str,
                age_seconds: int,
                sender_id: int,
            ) -> int:
                sent_at = to_iso(now - timedelta(seconds=age_seconds))
                database.insert_message(
                    MessageRecord(
                        chat_id=-1001,
                        message_id=message_id,
                        chat_name="测试群",
                        chat_username=None,
                        sender_id=sender_id,
                        sender_name="匿名发送者",
                        sent_at=sent_at,
                        text=text,
                        reply_to_message_id=None,
                        thread_root_id=message_id,
                        base_score=0,
                        reasons=(),
                        link=None,
                        normalized_text=score_message(
                            text,
                            mentioned_me=False,
                            reply_to_me=False,
                            trusted_sender=False,
                            keywords=(),
                        ).normalized_text,
                        primary_url=None,
                        created_at=sent_at,
                    )
                )
                return int(database.get_message(-1001, message_id)["id"])

            original_id = add(
                20, text="操作结果一致", age_seconds=30, sender_id=7
            )
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = 'passed',
                        ai_status = 'filtered_non_information',
                        ai_category = 'internal_coordination'
                    WHERE id = ?
                    """,
                    (original_id,),
                )
            duplicate_id = add(
                21, text="操作结果一致", age_seconds=0, sender_id=7
            )
            match = database.find_recent_terminal_duplicate(duplicate_id)
            self.assertIsNotNone(match)
            self.assertEqual(match["id"], original_id)
            self.assertEqual(match["dedupe_scope"], "rapid_short")

            different_sender_id = add(
                22, text="操作结果一致", age_seconds=0, sender_id=8
            )
            self.assertIsNone(
                database.find_recent_terminal_duplicate(different_sender_id)
            )

            protected_original_id = add(
                23, text="平台开源了", age_seconds=30, sender_id=7
            )
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = 'passed',
                        ai_status = 'filtered_non_information',
                        ai_category = 'discussion'
                    WHERE id = ?
                    """,
                    (protected_original_id,),
                )
            protected_repeat_id = add(
                24, text="平台开源了", age_seconds=0, sender_id=7
            )
            self.assertIsNone(
                database.find_recent_terminal_duplicate(
                    protected_repeat_id,
                    protected_keywords=("开源",),
                )
            )

            incident_original_id = add(
                25, text="服务又挂了", age_seconds=30, sender_id=7
            )
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET prefilter_status = 'passed',
                        ai_status = 'filtered_non_information',
                        ai_category = 'discussion'
                    WHERE id = ?
                    """,
                    (incident_original_id,),
                )
            incident_repeat_id = add(
                26, text="服务又挂了", age_seconds=0, sender_id=7
            )
            self.assertIsNone(
                database.find_recent_terminal_duplicate(incident_repeat_id)
            )
            database.close()

    def test_legacy_messages_migrate_idempotently_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/legacy-messages.db"
            import sqlite3

            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_name TEXT NOT NULL,
                    chat_username TEXT,
                    sender_id INTEGER,
                    sender_name TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    reply_to_message_id INTEGER,
                    thread_root_id INTEGER NOT NULL,
                    base_score INTEGER NOT NULL,
                    reply_bonus INTEGER NOT NULL DEFAULT 0,
                    score INTEGER NOT NULL,
                    reasons_json TEXT NOT NULL,
                    reply_count INTEGER NOT NULL DEFAULT 0,
                    link TEXT,
                    normalized_text TEXT NOT NULL,
                    primary_url TEXT,
                    immediate_pushed_at TEXT,
                    digest_considered_at TEXT,
                    digest_pushed_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO messages(
                    chat_id, message_id, chat_name, sender_name, sent_at, text,
                    thread_root_id, base_score, reply_bonus, score, reasons_json,
                    reply_count, normalized_text, created_at
                ) VALUES(-1001, 9, '历史群', '历史发送者', '2026-01-01T00:00:00+00:00',
                    '历史消息', 9, 42, 10, 52, '["历史规则"]', 2, '历史消息',
                    '2026-01-01T00:00:00+00:00')
                """
            )
            connection.commit()
            connection.close()

            for _ in range(2):
                database = Database(path)
                row = database.get_message(-1001, 9)
                self.assertIsNotNone(row)
                self.assertEqual(row["local_score"], 42)
                self.assertEqual(row["base_score"], 42)
                self.assertEqual(row["score"], 52)
                self.assertEqual(row["ai_status"], "not_analyzed")
                self.assertIsNone(row["ai_category"])
                self.assertIsNone(row["ai_category_label"])
                self.assertIsNone(row["ai_scoring_effort"])
                self.assertIsNone(row["content_kind"])
                self.assertEqual(row["community_status"], "not_analyzed")
                self.assertIsNone(row["community_response_text"])
                self.assertEqual(row["prefilter_status"], "not_evaluated")
                self.assertFalse(row["push_eligible"])
                self.assertEqual(row["push_gate_reason"], "historical_unreviewed")
                self.assertIsNone(row["semantic_dedupe_update_type"])
                self.assertFalse(row["semantic_dedupe_update_validated"])
                self.assertIsNone(row["semantic_dedupe_update_rejection_reason"])
                self.assertEqual(
                    row["notification_prepare_status"], "historical_unprepared"
                )
                self.assertIsNone(row["notification_title"])
                self.assertFalse(row["is_service_message"])
                self.assertEqual(row["local_reasons"], ("历史规则",))
                self.assertEqual(
                    sum(
                        column["name"] == "is_service_message"
                        for column in database.connection.execute(
                            "PRAGMA table_info(messages)"
                        )
                    ),
                    1,
                )
                self.assertEqual(
                    database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    1,
                )
                database.close()

    def test_migration_revokes_stale_historical_push_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            now = utc_now()
            historical = self._context_record(
                chat_id=-1001,
                message_id=1,
                sent_at=to_iso(now - timedelta(hours=2)),
                text="去标识历史资讯",
            )
            current = self._context_record(
                chat_id=-1001,
                message_id=2,
                sent_at=to_iso(now - timedelta(minutes=2)),
                text="去标识当前资讯",
            )
            self.assertTrue(database.insert_message(historical))
            self.assertTrue(database.insert_message(current))
            with database.connection:
                database.connection.execute(
                    """
                    UPDATE messages
                    SET ai_status = 'success', ai_category = 'external_information',
                        content_kind = 'news', ai_score = 75, score = 75,
                        push_eligible = 1, push_ready_at = ?,
                        semantic_dedupe_status = 'historical_unreviewed',
                        notification_prepare_status = 'historical_unprepared'
                    WHERE message_id = 1
                    """,
                    (to_iso(now - timedelta(hours=1)),),
                )
                database.connection.execute(
                    """
                    UPDATE messages
                    SET ai_status = 'success', ai_category = 'external_information',
                        content_kind = 'news', ai_score = 75, score = 75,
                        push_eligible = 1, push_ready_at = ?,
                        semantic_dedupe_status = 'unique',
                        notification_prepare_status = 'success',
                        notification_title = '去标识标题',
                        notification_body = '去标识正文'
                    WHERE message_id = 2
                    """,
                    (to_iso(now - timedelta(minutes=1)),),
                )
            database.close()

            for _ in range(2):
                database = Database(path)
                historical_row = database.get_message(-1001, 1)
                current_row = database.get_message(-1001, 2)
                self.assertFalse(historical_row["push_eligible"])
                self.assertIsNone(historical_row["push_ready_at"])
                self.assertTrue(current_row["push_eligible"])
                self.assertIsNotNone(current_row["push_ready_at"])
                database.close()

    def test_four_stage_model_config_migrates_from_existing_models_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/legacy-model-config.db"
            import sqlite3

            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE runtime_config (
                    id INTEGER PRIMARY KEY,
                    important_keywords_json TEXT NOT NULL,
                    trusted_sender_ids_json TEXT NOT NULL,
                    watch_chat_ids_json TEXT NOT NULL,
                    immediate_score INTEGER NOT NULL,
                    model_enabled INTEGER NOT NULL,
                    model_base_url TEXT NOT NULL,
                    model_api_key TEXT,
                    model_classification_model TEXT,
                    model_classification_reasoning_effort TEXT,
                    model_model TEXT,
                    model_reasoning_effort TEXT,
                    model_updated_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO runtime_config VALUES(
                    1, '["安全"]', '[]', '[]', 80, 1,
                    'http://model.test/v1', 'secret-not-returned',
                    'classifier-existing', 'medium', 'scorer-existing', 'high',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
                )
                """
            )
            connection.commit()
            connection.close()

            for _ in range(2):
                database = Database(path)
                config = database.get_model_config()
                self.assertEqual(config["classification_model"], "classifier-existing")
                self.assertEqual(config["model"], "scorer-existing")
                self.assertEqual(config["semantic_dedupe_model"], "classifier-existing")
                self.assertEqual(config["semantic_dedupe_reasoning_effort"], "low")
                self.assertEqual(config["notification_model"], "scorer-existing")
                self.assertEqual(config["notification_reasoning_effort"], "low")
                self.assertTrue(config["community_insights_enabled"])
                self.assertTrue(config["benefit_deals_enabled"])
                self.assertTrue(config["api_key_configured"])
                self.assertNotIn("api_key", config)
                database.close()

    def test_anonymous_chat_session_keys_are_stable_and_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            first = database.llm_session_key(-1001234567890)
            repeated = database.llm_session_key(-1001234567890)
            other = database.llm_session_key(-1009876543210)
            self.assertEqual(first, repeated)
            self.assertNotEqual(first, other)
            self.assertRegex(first, r"\Atgchat-v1-[0-9a-f]{64}\Z")
            self.assertNotIn("1001234567890", first)
            self.assertEqual(
                database.connection.execute(
                    "SELECT COUNT(*) FROM metadata WHERE key = 'llm_session_namespace_v1'"
                ).fetchone()[0],
                1,
            )
            database.close()

            reopened = Database(path)
            self.assertEqual(reopened.llm_session_key(-1001234567890), first)
            reopened.close()

    def test_recent_context_filters_and_never_crosses_chat_or_time_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            chat_id = -1001

            candidates = (
                (
                    chat_id,
                    1,
                    now - timedelta(minutes=12),
                    "较早的群内治理通知",
                    False,
                    "success",
                    "internal_governance",
                ),
                (
                    chat_id,
                    2,
                    now - timedelta(minutes=11),
                    "外部产品发布了新版",
                    False,
                    "success",
                    "external_information",
                ),
                (
                    chat_id,
                    3,
                    now - timedelta(minutes=10),
                    "群标题更新为新名称",
                    True,
                    "success",
                    "external_information",
                ),
                (
                    chat_id,
                    4,
                    now - timedelta(minutes=9),
                    "🎉🎉",
                    False,
                    "success",
                    "discussion",
                ),
                (
                    chat_id,
                    5,
                    now - timedelta(minutes=8),
                    "入群验证：欢迎加入群组，请完成验证",
                    False,
                    "success",
                    "discussion",
                ),
                (
                    chat_id,
                    6,
                    now - timedelta(minutes=7),
                    "检测到违规消息，已删除并封禁",
                    False,
                    "success",
                    "external_information",
                ),
                (
                    chat_id,
                    7,
                    now - timedelta(minutes=6),
                    "推广内容",
                    False,
                    "filtered_non_information",
                    "promotion_spam",
                ),
                (
                    chat_id,
                    8,
                    now - timedelta(minutes=5),
                    "类别不明确",
                    False,
                    "filtered_non_information",
                    "unknown",
                ),
                (
                    chat_id,
                    9,
                    now - timedelta(minutes=4),
                    "仍在分析",
                    False,
                    "pending",
                    "external_information",
                ),
                (
                    chat_id,
                    10,
                    now - timedelta(minutes=3),
                    "分析失败",
                    False,
                    "error",
                    "external_information",
                ),
                (
                    -2002,
                    11,
                    now - timedelta(minutes=2),
                    "另一群的消息",
                    False,
                    "success",
                    "external_information",
                ),
                (
                    chat_id,
                    12,
                    now - timedelta(minutes=1),
                    "已修复",
                    False,
                    "success",
                    "external_information",
                ),
                (
                    chat_id,
                    49,
                    now,
                    "同一时间但排序在前",
                    False,
                    "success",
                    "external_information",
                ),
                (
                    chat_id,
                    51,
                    now,
                    "同一时间但排序在后",
                    False,
                    "success",
                    "external_information",
                ),
                (
                    chat_id,
                    52,
                    now + timedelta(minutes=1),
                    "未来消息",
                    False,
                    "success",
                    "external_information",
                ),
            )
            for (
                candidate_chat,
                message_id,
                sent,
                text,
                service,
                status,
                category,
            ) in candidates:
                sent_at = to_iso(sent)
                database.insert_message(
                    self._context_record(
                        chat_id=candidate_chat,
                        message_id=message_id,
                        sent_at=sent_at,
                        text=text,
                        is_service_message=service,
                    )
                )
                self._mark_history(
                    database,
                    chat_id=candidate_chat,
                    message_id=message_id,
                    status=status,
                    category=category,
                )

            database.insert_message(
                self._context_record(
                    chat_id=chat_id,
                    message_id=50,
                    sent_at=to_iso(now),
                    text="当前消息",
                )
            )
            target = database.get_message(chat_id, 50)
            context = database.recent_llm_context(target["id"])
            self.assertEqual(
                [item["text"] for item in context],
                ["外部产品发布了新版", "已修复", "同一时间但排序在前"],
            )
            self.assertTrue(all(set(item) == {"time", "text"} for item in context))
            serialized = " ".join(item["text"] for item in context)
            for forbidden in (
                "当前消息",
                "较早的群内治理通知",
                "另一群的消息",
                "未来消息",
                "同一时间但排序在后",
                "群标题更新为新名称",
                "🎉🎉",
                "入群验证",
                "已删除并封禁",
                "推广内容",
                "类别不明确",
                "仍在分析",
                "分析失败",
            ):
                self.assertNotIn(forbidden, serialized)
            database.close()

    def test_recent_context_uses_latest_twelve_in_order_and_character_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            chat_id = -1001
            for message_id in range(1, 13):
                sent_at = to_iso(now + timedelta(seconds=message_id))
                database.insert_message(
                    self._context_record(
                        chat_id=chat_id,
                        message_id=message_id,
                        sent_at=sent_at,
                        text=f"合格历史消息 {message_id}",
                    )
                )
                self._mark_history(
                    database,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="success",
                    category="external_information",
                )
            database.insert_message(
                self._context_record(
                    chat_id=chat_id,
                    message_id=20,
                    sent_at=to_iso(now + timedelta(minutes=1)),
                    text="当前消息",
                )
            )
            target = database.get_message(chat_id, 20)
            context = database.recent_llm_context(target["id"])
            self.assertEqual(len(context), 12)
            self.assertEqual(
                [item["text"] for item in context],
                [f"合格历史消息 {message_id}" for message_id in range(1, 13)],
            )
            self.assertLessEqual(
                context_character_count(context), RECENT_CONTEXT_CHAR_LIMIT
            )

            bounded = database.recent_llm_context(target["id"], character_limit=40)
            self.assertTrue(bounded)
            self.assertLessEqual(context_character_count(bounded), 40)
            self.assertEqual(bounded[-1]["text"], "合格历史消息 12")
            database.close()

    def test_recent_context_adds_eight_relevant_rows_with_hard_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            chat_id = -1001
            # Older candidates include eight useful matches, unrelated news, and
            # one strong match outside the 24-hour relevance window.
            older = [
                (index, now - timedelta(hours=12, minutes=30 - index), f"云平台安全漏洞修复版本 {index}")
                for index in range(1, 9)
            ]
            older.extend(
                (20 + index, now - timedelta(hours=10, minutes=index), f"普通文娱消息 {index}")
                for index in range(1, 6)
            )
            older.append((40, now - timedelta(hours=25), "云平台安全漏洞修复版本 过期"))
            recent = [
                (100 + index, now - timedelta(minutes=13 - index), f"最近合格资讯 {index}")
                for index in range(1, 13)
            ]
            for message_id, sent, text in (*older, *recent):
                database.insert_message(
                    self._context_record(
                        chat_id=chat_id,
                        message_id=message_id,
                        sent_at=to_iso(sent),
                        text=text,
                    )
                )
                self._mark_history(
                    database,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="success",
                    category="external_information",
                )
            database.insert_message(
                self._context_record(
                    chat_id=chat_id,
                    message_id=999,
                    sent_at=to_iso(now),
                    text="云平台安全漏洞发布修复版本",
                )
            )
            target = database.get_message(chat_id, 999)
            context = database.recent_llm_context(target["id"])
            texts = [item["text"] for item in context]
            self.assertEqual(len(context), RECENT_CONTEXT_LIMIT)
            self.assertTrue(all(f"最近合格资讯 {index}" in texts for index in range(1, 13)))
            self.assertTrue(all(f"云平台安全漏洞修复版本 {index}" in texts for index in range(1, 9)))
            self.assertNotIn("云平台安全漏洞修复版本 过期", texts)
            self.assertFalse(any("普通文娱消息" in text for text in texts))
            self.assertEqual(
                [item["time"] for item in context],
                sorted(item["time"] for item in context),
            )
            self.assertLessEqual(context_character_count(context), RECENT_CONTEXT_CHAR_LIMIT)

            bounded = database.recent_llm_context(target["id"], character_limit=180)
            self.assertLessEqual(context_character_count(bounded), 180)
            self.assertLessEqual(len(bounded), RECENT_CONTEXT_LIMIT)
            database.close()

    def test_short_current_signal_can_select_relevant_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            chat_id = -1001
            database.insert_message(
                self._context_record(
                    chat_id=chat_id,
                    message_id=1,
                    sent_at=to_iso(now - timedelta(hours=2)),
                    text="云服务宕机影响多个地区",
                )
            )
            self._mark_history(
                database,
                chat_id=chat_id,
                message_id=1,
                status="success",
                category="external_information",
            )
            for index in range(2, 14):
                database.insert_message(
                    self._context_record(
                        chat_id=chat_id,
                        message_id=index,
                        sent_at=to_iso(now - timedelta(minutes=20 - index)),
                        text=f"最近独立资讯 {index}",
                    )
                )
                self._mark_history(
                    database,
                    chat_id=chat_id,
                    message_id=index,
                    status="success",
                    category="external_information",
                )
            database.insert_message(
                self._context_record(
                    chat_id=chat_id,
                    message_id=99,
                    sent_at=to_iso(now),
                    text="宕机",
                )
            )
            target = database.get_message(chat_id, 99)
            context = database.recent_llm_context(target["id"])
            self.assertIn("云服务宕机影响多个地区", [item["text"] for item in context])
            database.close()

    def test_available_chats_and_legacy_runtime_config_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/legacy.db"
            import sqlite3

            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE runtime_config (
                    id INTEGER PRIMARY KEY,
                    important_keywords_json TEXT NOT NULL,
                    trusted_sender_ids_json TEXT NOT NULL,
                    immediate_score INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO runtime_config VALUES(1, '[\"紧急\"]', '[]', 80, '2026-01-01T00:00:00+00:00')"
            )
            connection.commit()
            connection.close()

            database = Database(path)
            now = utc_now()
            database.initialize_runtime_config(
                important_keywords=("紧急",),
                trusted_sender_ids=frozenset(),
                watch_chat_ids=frozenset({-1001}),
                immediate_score=80,
                now=now,
            )
            self.assertEqual(database.get_runtime_config()["watch_chat_ids"], frozenset({-1001}))
            self.assertEqual(database.get_model_config()["reasoning_effort"], "default")
            self.assertEqual(
                database.get_model_config()["classification_model"],
                DEFAULT_CLASSIFICATION_MODEL,
            )
            self.assertEqual(
                database.get_model_config()["classification_reasoning_effort"], "low"
            )
            database.update_model_config(
                enabled=False,
                base_url="http://model.test/v1",
                model="test-model",
                classification_model="test-classifier",
                api_key="",
                clear_api_key=False,
                now=now,
                reasoning_effort="high",
                classification_reasoning_effort="medium",
            )
            self.assertEqual(database.get_model_config()["reasoning_effort"], "high")
            self.assertEqual(
                database.get_model_config()["classification_model"], "test-classifier"
            )
            self.assertEqual(
                database.get_model_config()["classification_reasoning_effort"], "medium"
            )
            database.replace_available_chats(
                [
                    {
                        "chat_id": -1001,
                        "chat_name": "测试群",
                        "chat_type": "group",
                        "username": "test_group",
                    }
                ],
                now=now,
            )
            chats = database.list_chat_options(hours=24, now=now)
            self.assertEqual(chats[0]["chat_id"], -1001)
            self.assertTrue(chats[0]["watched"])
            database.close()

            reopened = Database(path)
            self.assertEqual(reopened.get_model_config()["reasoning_effort"], "high")
            self.assertEqual(
                reopened.get_model_config()["classification_model"], "test-classifier"
            )
            self.assertEqual(
                sum(
                    row["name"] == "model_reasoning_effort"
                    for row in reopened.connection.execute("PRAGMA table_info(runtime_config)")
                ),
                1,
            )
            self.assertEqual(
                sum(
                    row["name"] == "model_classification_model"
                    for row in reopened.connection.execute("PRAGMA table_info(runtime_config)")
                ),
                1,
            )
            reopened.close()

    def test_recover_pending_preserves_completed_classification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            record = MessageRecord(
                chat_id=-1001,
                message_id=11,
                chat_name="测试群",
                chat_username=None,
                sender_id=2,
                sender_name="发送者",
                sent_at=to_iso(now),
                text="今晚维护，请值班同学确认",
                reply_to_message_id=None,
                thread_root_id=11,
                base_score=40,
                reasons=("维护 +25",),
                link=None,
                normalized_text="今晚维护请值班同学确认",
                primary_url=None,
                created_at=to_iso(now),
            )
            database.insert_message(record)
            row_id = database.get_message(-1001, 11)["id"]
            self.assertTrue(
                database.begin_ai_analysis(
                    row_id,
                    model="test-model",
                    classification_model="test-classifier",
                    reasoning_effort="medium",
                    classification_reasoning_effort="high",
                    now=now,
                )
            )
            self.assertTrue(
                database.save_ai_classification(
                    row_id,
                    classification=ClassificationOutcome(
                        status="success",
                        model="test-classifier",
                        category="internal_coordination",
                        confidence=91,
                        summary="维护协调",
                        reason="有明确行动请求",
                        response_text="分类原始响应",
                        effort="high",
                    ),
                )
            )
            self.assertEqual(database.recover_pending_analyses(now=now), 1)
            recovered = database.get_message_by_id(row_id)
            self.assertEqual(recovered["ai_status"], "error")
            self.assertEqual(recovered["ai_error_category"], "interrupted")
            self.assertEqual(recovered["ai_error_stage"], "scoring")
            self.assertEqual(recovered["ai_category"], "internal_coordination")
            self.assertEqual(recovered["ai_category_confidence"], 91)
            self.assertEqual(recovered["ai_classification_model"], "test-classifier")
            self.assertEqual(recovered["ai_classification_effort"], "high")
            self.assertEqual(recovered["ai_scoring_effort"], "medium")
            self.assertEqual(recovered["base_score"], recovered["local_score"])
            self.assertFalse(recovered["push_eligible"])
            self.assertEqual(recovered["push_gate_reason"], "analysis_interrupted")
            database.close()

    def test_push_queries_require_eligibility_even_with_reply_bonus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_digest_clock(now - timedelta(minutes=1))
            record = MessageRecord(
                chat_id=-1001,
                message_id=99,
                chat_name="测试群",
                chat_username=None,
                sender_id=2,
                sender_name="发送者",
                sent_at=to_iso(now),
                text="管理员已封禁本群用户",
                reply_to_message_id=None,
                thread_root_id=99,
                base_score=100,
                reasons=("提及账号 +100",),
                link=None,
                normalized_text="管理员已封禁本群用户",
                primary_url=None,
                created_at=to_iso(now),
            )
            database.insert_message(record)
            for sender in (11, 12, 13, 14, 15):
                database.record_reply_and_update_parent(
                    chat_id=-1001,
                    parent_message_id=99,
                    sender_id=sender,
                    replied_at=now,
                    window_minutes=15,
                )
            row = database.get_message(-1001, 99)
            self.assertGreaterEqual(row["score"], 100)
            self.assertFalse(row["push_eligible"])
            self.assertIsNone(database.reserve_immediate(-1001, 99, 80, now))
            self.assertEqual(database.digest_candidates(now + timedelta(seconds=1)), [])
            database.close()

    def test_fixed_realtime_threshold_and_reply_updates_use_ai_score_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_digest_clock(now - timedelta(minutes=1))
            for message_id, ai_score in ((1, 59), (2, 60), (3, 79), (4, 80), (5, 95)):
                record = self._context_record(
                    chat_id=-1001,
                    message_id=message_id,
                    sent_at=to_iso(now),
                    text=f"外部资讯样例 {message_id}",
                )
                self.assertTrue(database.insert_message(record))
                with database.connection:
                    database.connection.execute(
                        """
                        UPDATE messages
                        SET local_score = 100, base_score = ?, score = ?,
                            ai_status = 'success', ai_category = 'external_information',
                            ai_score = ?, push_eligible = 1,
                            push_gate_reason = 'eligible_unique',
                            push_ready_at = created_at,
                            semantic_dedupe_status = 'unique_no_candidates',
                            notification_prepare_status = 'success'
                        WHERE chat_id = -1001 AND message_id = ?
                        """,
                        (ai_score, ai_score, ai_score, message_id),
                    )

            for sender_id in (10, 11, 12, 13, 14):
                database.record_reply_and_update_parent(
                    chat_id=-1001,
                    parent_message_id=1,
                    sender_id=sender_id,
                    replied_at=now,
                    window_minutes=15,
                )
            low_ai = database.get_message(-1001, 1)
            self.assertEqual(low_ai["local_score"], 100)
            self.assertEqual(low_ai["reply_bonus"], 30)
            self.assertEqual(low_ai["score"], 59)
            self.assertIsNone(database.reserve_immediate(-1001, 1, 80, now))
            for message_id in (2, 3, 4, 5):
                self.assertIsNotNone(
                    database.reserve_immediate(-1001, message_id, 999, now)
                )
            database.close()


class OutputSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = {
            "chat_id": -1001,
            "message_id": 7,
            "chat_name": "<群&>" * 100,
            "sender_name": "发送者 & <admin>" * 40,
            "text": "🚨<&>" * 1_000,
            "reasons": ("关键词 <script> & " * 100,),
            "reply_count": 5,
            "score": 120,
            "base_score": 90,
            "local_score": 70,
            "reply_bonus": 30,
            "link": "https://t.me/c/1/7",
            "thread_root_id": 7,
            "normalized_text": "异常通知",
            "primary_url": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "ai_status": "success",
            "ai_model": "test-model-with-a-very-long-name" * 20,
            "ai_score": 90,
            "ai_summary": "模型摘要" * 200,
            "ai_response_text": "不应进入推送的模型原始响应",
            "ai_category": "external_information",
            "ai_category_label": "外部资讯",
            "push_eligible": True,
            "ai_category_response_text": "不应进入推送的分类原始响应",
            "notification_prepare_status": "success",
            "notification_title": "安全服务发布重要修复",
            "notification_body": "第一段 <script>风险说明</script>\n\n第二段影响范围",
            "notification_prepare_response_text": "不应进入推送的整理原始响应",
        }

    def test_html_is_escaped_and_within_telegram_limit(self) -> None:
        for chunks in (immediate_chunks(self.row), digest_chunks([self.row] * 7)):
            self.assertTrue(chunks)
            self.assertTrue(all(_utf16_units(chunk) <= 3_900 for chunk in chunks))
            self.assertTrue(all("<script>" not in chunk for chunk in chunks))
            self.assertTrue(all("不应进入推送的模型原始响应" not in chunk for chunk in chunks))
            self.assertTrue(all("不应进入推送的分类原始响应" not in chunk for chunk in chunks))
            self.assertTrue(all("不应进入推送的整理原始响应" not in chunk for chunk in chunks))
            self.assertTrue(any("安全服务发布重要修复" in chunk for chunk in chunks))
            self.assertTrue(any("<b>资讯摘要</b>" in chunk for chunk in chunks))
            self.assertTrue(any("• 第一段 &lt;script&gt;风险说明&lt;/script&gt;\n• 第二段" in chunk for chunk in chunks))
            for hidden in ("分类：", "推送评分", "不参与评分", "本地规则", "https://t.me/"):
                self.assertTrue(all(hidden not in chunk for chunk in chunks))

    def test_digest_deduplicates_same_thread(self) -> None:
        second = dict(self.row, message_id=8, score=200, ai_score=80)
        selected = select_digest_items([second, self.row])
        self.assertEqual([row["message_id"] for row in selected], [7])

    def test_semantic_shortlist_keeps_recent_and_older_relevant_event(self) -> None:
        current = {
            "id": 999,
            "text": "国区音乐订阅费用发生调整",
            "normalized_text": "国区音乐订阅费用发生调整",
            "ai_summary": "国区 Apple Music 价格上涨至 12 元每月",
        }
        candidates = tuple(
            {
                "id": index,
                "text": f"无关行业资讯 {index}",
                "normalized_text": f"无关行业资讯{index}",
                "ai_summary": f"普通行业消息 {index}",
                "ai_completed_at": f"2026-08-10T00:{20-index:02d}:00+00:00",
            }
            for index in range(1, 15)
        )
        relevant = {
            "id": 15,
            "text": "音乐服务在中国市场更新订阅方案",
            "normalized_text": "音乐服务在中国市场更新订阅方案",
            "ai_summary": "国区 Apple Music 订阅价格再次上涨",
            "ai_completed_at": "2026-08-09T23:00:00+00:00",
        }
        selected = select_semantic_candidates(current, (*candidates, relevant))
        self.assertEqual(len(selected), 12)
        self.assertEqual([row["id"] for row in selected[:8]], list(range(1, 9)))
        self.assertIn(15, {row["id"] for row in selected})


class ChatSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_removes_chats_no_longer_visible(self) -> None:
        class FakeClient:
            async def iter_dialogs(self):
                yield SimpleNamespace(
                    id=-1001,
                    name="仍可见的群",
                    is_group=True,
                    is_channel=False,
                    entity=SimpleNamespace(username="visible_group"),
                )

        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            database.initialize_runtime_config(
                important_keywords=("紧急",),
                trusted_sender_ids=frozenset(),
                watch_chat_ids=frozenset({-1001, -9999}),
                immediate_score=80,
                now=now,
            )
            count = await sync_available_chats(FakeClient(), database)
            self.assertEqual(count, 1)
            self.assertEqual(
                database.get_runtime_config()["watch_chat_ids"],
                frozenset({-1001}),
            )
            database.close()


if __name__ == "__main__":
    unittest.main()
