from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app.chat_activity import SCHEMA_KEY, initialize_chat_activity
from app.database import Database, to_iso, utc_now
from tests.test_queues import mark_eligible, record


def configure_watch(database: Database, ids: frozenset[int], now) -> None:
    database.update_runtime_config(
        important_keywords=("网络",), trusted_sender_ids=frozenset(),
        watch_chat_ids=ids, immediate_score=60, now=now,
    )
    database.replace_available_chats(
        [{"chat_id": -1001, "chat_name": "示例服务公告", "chat_type": "channel", "username": None}],
        now=now,
    )


def remove_activity_schema_from_fixture(database: Database) -> None:
    """Simulate the preceding release using only an isolated test database."""
    with database.connection:
        for name in ("chat_activity_recorded", "chat_activity_delivery", "chat_activity_push_counted"):
            database.connection.execute(f"DROP TRIGGER {name}")
        for name in ("chat_push_receipts", "chat_watch_events", "chat_activity"):
            database.connection.execute(f"DROP TABLE {name}")
        database.connection.execute("ALTER TABLE messages DROP COLUMN media_text_unavailable")
        database.connection.execute("DELETE FROM metadata WHERE key = ?", (SCHEMA_KEY,))


class ChatActivityTests(unittest.TestCase):
    def test_watch_changes_are_atomic_and_restart_does_not_reset_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            now = utc_now()
            configure_watch(database, frozenset({-1001}), now)
            database.initialize_runtime_config(
                important_keywords=("网络",), trusted_sender_ids=frozenset(),
                watch_chat_ids=frozenset({-2002}), immediate_score=60,
                now=now + timedelta(hours=1),
            )
            activity = database.list_chat_options(hours=24, now=now)[0]["activity"]
            self.assertEqual(activity["watch_started_at"], to_iso(now))
            self.assertTrue(activity["watch_start_known"])
            self.assertEqual(database.get_runtime_config()["watch_chat_ids"], frozenset({-1001}))
            configure_watch(database, frozenset(), now + timedelta(hours=2))
            configure_watch(database, frozenset({-1001}), now + timedelta(hours=3))
            activity = database.list_chat_options(hours=24, now=now)[0]["activity"]
            self.assertEqual(activity["watch_started_at"], to_iso(now + timedelta(hours=3)))
            self.assertEqual(
                [row["action"] for row in database.connection.execute(
                    "SELECT action FROM chat_watch_events ORDER BY id"
                )],
                ["enabled", "disabled", "enabled"],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                with database.connection:
                    database.connection.execute(
                        "CREATE TRIGGER fail_watch BEFORE INSERT ON chat_watch_events "
                        "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
                    )
                    configure_watch(database, frozenset(), now + timedelta(hours=4))
            self.assertEqual(database.get_runtime_config()["watch_chat_ids"], frozenset({-1001}))
            database.close()
            reopened = Database(path)
            self.assertEqual(
                reopened.list_chat_options(hours=24, now=now)[0]["activity"]["watch_started_at"],
                to_iso(now + timedelta(hours=3)),
            )
            reopened.close()

    def test_message_and_push_totals_are_idempotent_and_survive_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            configure_watch(database, frozenset({-1001}), now)
            item = replace(record(-1001, 1), created_at=to_iso(now - timedelta(days=5)))
            self.assertTrue(database.insert_message(item))
            self.assertFalse(database.insert_message(item))
            row = database.get_message(-1001, 1)
            mark_eligible(database, row["id"])
            with database.connection:
                for channel in ("telegram", "ntfy"):
                    database.connection.execute("""
                        INSERT INTO deliveries(
                            message_row_id, channel, delivery_type, state,
                            available_at, created_at, updated_at
                        ) VALUES(?, ?, 'immediate', 'processing', ?, ?, ?)
                    """, (row["id"], channel, to_iso(now), to_iso(now), to_iso(now)))
            units = list(database.connection.execute("SELECT id, channel FROM deliveries ORDER BY id"))
            database.finish_delivery_unit([units[0]["id"]], now=now, succeeded=True)
            database.finish_delivery_unit([units[1]["id"]], now=now, succeeded=False, error_category="network_error")
            activity = database.list_chat_options(hours=720, now=now)[0]["activity"]
            self.assertEqual(activity["recorded_count"], 1)
            self.assertEqual(activity["successful_push_count"], 1)
            self.assertEqual(activity["retained_pushed_count"], 1)
            self.assertEqual(activity["retained_failed_push_count"], 1)
            # Repeated completion and a second output success never count twice.
            database.finish_delivery_unit([units[0]["id"]], now=now, succeeded=True)
            with database.connection:
                database.connection.execute("UPDATE deliveries SET state = 'processing' WHERE channel = 'ntfy'")
            database.finish_delivery_unit([units[1]["id"]], now=now, succeeded=True)
            self.assertEqual(database.list_chat_options(hours=720, now=now)[0]["activity"]["successful_push_count"], 1)
            self.assertEqual(database.cleanup(3, now), 1)
            result = database.list_chat_options(hours=720, now=now)[0]
            self.assertEqual(result["message_count"], 0)
            self.assertEqual(result["activity"]["recorded_count"], 1)
            self.assertEqual(result["activity"]["successful_push_count"], 1)
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM chat_push_receipts").fetchone()[0], 0)
            database.close()

    def test_image_only_message_has_separate_audit_and_never_queues_a_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/messages.db")
            now = utc_now()
            configure_watch(database, frozenset({-1001}), now)
            item = replace(record(-1001, 2), text="图片消息（未启用图片文字识别）", media_text_unavailable=True)
            database.insert_message(item, enqueue_analysis=True, now=now)
            row = database.get_message(-1001, 2)
            self.assertEqual(row["prefilter_reason_code"], "image_text_unavailable")
            self.assertIn("未启用图片文字识别", row["prefilter_reason"])
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0], 0)
            activity = database.list_chat_options(hours=24, now=now)[0]["activity"]
            self.assertEqual(activity["media_unread_count"], 1)
            self.assertEqual(activity["retained_media_unread_count"], 1)
            self.assertEqual(activity["retained_filtered_count"], 0)
            database.close()

    def test_upgrade_backs_up_and_marks_partial_history_without_queueing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            now = utc_now()
            configure_watch(database, frozenset({-1001}), now)
            database.insert_message(record(-1001, 1))
            database.insert_message(replace(record(-1001, 2), text="文件名：sample.jpg"))
            with database.connection:
                database.connection.execute(
                    "UPDATE messages SET immediate_pushed_at = ? WHERE message_id = 1",
                    (to_iso(now),),
                )
            remove_activity_schema_from_fixture(database)
            database.close()
            reopened = Database(path)
            activity = reopened.list_chat_options(hours=24, now=now)[0]["activity"]
            self.assertEqual(activity["recorded_count"], 2)
            self.assertEqual(activity["successful_push_count"], 1)
            self.assertEqual(activity["media_unread_count"], 1)
            self.assertTrue(activity["includes_partial_history"])
            self.assertFalse(activity["watch_start_known"])
            self.assertIsNone(activity["watch_started_at"])
            self.assertEqual(reopened.connection.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0], 0)
            self.assertEqual(reopened.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
            backups = list(Path(directory).glob("messages.db.pre-activity-v1-*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(backups[0]) as backup:
                self.assertEqual(backup.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
                self.assertNotIn("media_text_unavailable", {row[1] for row in backup.execute("PRAGMA table_info(messages)")})
            reopened.close()
            again = Database(path)
            self.assertEqual(again.list_chat_options(hours=24, now=now)[0]["activity"]["recorded_count"], 2)
            self.assertEqual(len(list(Path(directory).glob("*.bak"))), 1)
            again.close()

    def test_backup_failure_prevents_migration_and_future_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            configure_watch(database, frozenset({-1001}), utc_now())
            remove_activity_schema_from_fixture(database)
            with patch("app.chat_activity.sqlite3.connect", side_effect=OSError("fixture backup failure")):
                with self.assertRaises(OSError):
                    initialize_chat_activity(database.connection)
            self.assertNotIn("media_text_unavailable", {
                row["name"] for row in database.connection.execute("PRAGMA table_info(messages)")
            })
            self.assertIsNone(database.connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (SCHEMA_KEY,)
            ).fetchone())
            initialize_chat_activity(database.connection)
            with database.connection:
                database.connection.execute("UPDATE metadata SET value = '2' WHERE key = ?", (SCHEMA_KEY,))
            database.close()
            with self.assertRaisesRegex(RuntimeError, "downgrade"):
                Database(path)

    def test_schema_failure_rolls_back_every_new_table_column_and_marker(self) -> None:
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if "CREATE TABLE chat_activity" in sql:
                    raise sqlite3.OperationalError("fixture schema failure")
                return super().execute(sql, parameters)

        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/messages.db"
            database = Database(path)
            database.insert_message(record(-1001, 1))
            remove_activity_schema_from_fixture(database)
            database.close()
            connection = sqlite3.connect(path, factory=FailingConnection)
            connection.row_factory = sqlite3.Row
            with self.assertRaisesRegex(sqlite3.OperationalError, "schema failure"):
                initialize_chat_activity(connection)
            self.assertNotIn("media_text_unavailable", {
                row["name"] for row in connection.execute("PRAGMA table_info(messages)")
            })
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'chat_activity'"
            ).fetchone()[0], 0)
            self.assertIsNone(connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (SCHEMA_KEY,)
            ).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            connection.close()
