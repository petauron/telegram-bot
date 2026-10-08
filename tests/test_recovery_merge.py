from __future__ import annotations

import shutil
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.database import Database, MessageRecord, to_iso, utc_now
from app.recovery_merge import merge_recovery_increment, requeue_recovery_targets


class RecoveryIncrementMergeTests(unittest.TestCase):
    @staticmethod
    def _record(message_id: int, created_at, text: str) -> MessageRecord:
        return MessageRecord(
            chat_id=-1001,
            message_id=message_id,
            chat_name="去标识来源",
            chat_username=None,
            sender_id=None,
            sender_name="去标识发送者",
            sent_at=to_iso(created_at),
            text=text,
            reply_to_message_id=None,
            thread_root_id=message_id,
            base_score=0,
            reasons=(),
            link=None,
            normalized_text=text,
            primary_url=None,
            created_at=to_iso(created_at),
        )

    def test_second_rollback_increment_is_merged_without_primary_key_reuse_or_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = root / "common.db"
            current_path = root / "current.db"
            increment_path = root / "first-start.db"
            output_path = root / "merged.db"
            cutoff = utc_now().replace(microsecond=0) - timedelta(minutes=10)
            common_db = Database(str(common))
            with common_db.connection:
                common_db.connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('recovery_delivery_cutoff_at',?)",
                    (to_iso(cutoff),),
                )
                common_db.connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('llm_session_namespace_v1',?)",
                    ("ab" * 32,),
                )
            common_db.close()
            shutil.copyfile(common, current_path)
            shutil.copyfile(common, increment_path)

            current = Database(str(current_path), initialize=False)
            current.insert_message(
                self._record(101, cutoff + timedelta(minutes=3), "当前库新流量一"),
                enqueue_analysis=True,
                now=cutoff + timedelta(minutes=3),
            )
            current.insert_message(
                self._record(102, cutoff + timedelta(minutes=4), "当前库新流量二"),
                enqueue_analysis=True,
                now=cutoff + timedelta(minutes=4),
            )
            current_ids = {
                int(row["id"])
                for row in current.connection.execute("SELECT id FROM messages")
            }
            current.close()

            increment = Database(str(increment_path), initialize=False)
            increment.insert_message(
                self._record(201, cutoff + timedelta(minutes=1), "首次启动遗漏一"),
                enqueue_analysis=True,
                now=cutoff + timedelta(minutes=1),
            )
            increment.insert_message(
                self._record(202, cutoff + timedelta(minutes=2), "首次启动遗漏二"),
                enqueue_analysis=True,
                now=cutoff + timedelta(minutes=2),
            )
            first = increment.get_message(-1001, 201)
            second = increment.get_message(-1001, 202)
            with increment.connection:
                increment.connection.execute(
                    "UPDATE messages SET ai_status='prefiltered', analysis_queue_state='succeeded', "
                    "push_gate_reason='prefiltered', prefilter_status='filtered' WHERE id=?",
                    (int(second["id"]),),
                )
                increment.connection.execute(
                    "UPDATE analysis_jobs SET state='succeeded', attempts=1, result_status='prefiltered', "
                    "completed_at=?, updated_at=? WHERE message_row_id=?",
                    (to_iso(cutoff + timedelta(minutes=2)), to_iso(cutoff + timedelta(minutes=2)), int(second["id"])),
                )
                increment.connection.execute(
                    "INSERT INTO replies(chat_id,parent_message_id,sender_id,replied_at) VALUES(?,?,?,?)",
                    (-1001, 201, 7, to_iso(cutoff + timedelta(minutes=2))),
                )
            source_ids = {int(first["id"]), int(second["id"])}
            self.assertEqual(source_ids, current_ids)
            increment.close()

            report = merge_recovery_increment(
                base_path=current_path,
                increment_path=increment_path,
                output_path=output_path,
                merged_at=to_iso(cutoff + timedelta(minutes=9)),
                expected_messages=2,
                expected_replies=1,
            )
            self.assertEqual(report.messages_merged, 2)
            self.assertEqual(report.replies_merged, 1)
            self.assertEqual(report.completed_jobs_preserved, 1)
            self.assertEqual(report.incomplete_jobs_quarantined, 1)
            self.assertEqual(report.deliveries_created, 0)

            merged = sqlite3.connect(output_path)
            merged.row_factory = sqlite3.Row
            self.assertEqual(
                {
                    int(row["message_id"])
                    for row in merged.execute("SELECT message_id FROM messages")
                },
                {101, 102, 201, 202},
            )
            recovered = merged.execute(
                "SELECT * FROM messages WHERE message_id=201"
            ).fetchone()
            self.assertNotIn(int(recovered["id"]), source_ids)
            self.assertEqual(recovered["analysis_queue_state"], "failed")
            self.assertEqual(recovered["push_gate_reason"], "recovery_increment_quarantined")
            self.assertFalse(bool(recovered["push_eligible"]))
            job = merged.execute(
                "SELECT * FROM analysis_jobs WHERE message_row_id=?",
                (int(recovered["id"]),),
            ).fetchone()
            self.assertEqual((job["kind"], job["state"]), ("manual", "failed"))
            self.assertEqual(merged.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 1)
            self.assertEqual(merged.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
            self.assertEqual(merged.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(merged.execute("PRAGMA foreign_key_check").fetchall(), [])
            merged.close()

    def test_reviewed_shutdown_backlog_requeues_only_quarantine_target_as_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "messages.db"
            manifest_path = root / "targets.json"
            cutoff = utc_now().replace(microsecond=0) - timedelta(minutes=10)
            queued_at = to_iso(cutoff + timedelta(minutes=9))
            database = Database(str(database_path))
            namespace = "cd" * 32
            with database.connection:
                database.connection.executemany(
                    "INSERT INTO metadata(key,value) VALUES(?,?)",
                    (
                        ("recovery_delivery_cutoff_at", to_iso(cutoff)),
                        ("llm_session_namespace_v1", namespace),
                    ),
                )
            database.insert_message(
                self._record(301, cutoff + timedelta(minutes=1), "已完成目标"),
                enqueue_analysis=True,
                now=cutoff + timedelta(minutes=1),
            )
            database.insert_message(
                self._record(302, cutoff + timedelta(minutes=2), "恢复隔离目标"),
                enqueue_analysis=True,
                now=cutoff + timedelta(minutes=2),
            )
            database.insert_message(
                self._record(303, cutoff + timedelta(minutes=3), "上线后无关流量"),
                enqueue_analysis=True,
                now=cutoff + timedelta(minutes=3),
            )
            completed = database.get_message(-1001, 301)
            quarantined = database.get_message(-1001, 302)
            unrelated = database.get_message(-1001, 303)
            with database.connection:
                database.connection.execute(
                    "UPDATE messages SET ai_status='prefiltered', "
                    "analysis_queue_state='succeeded', push_gate_reason='prefiltered' "
                    "WHERE id=?",
                    (int(completed["id"]),),
                )
                database.connection.execute(
                    "UPDATE analysis_jobs SET state='succeeded', attempts=1, "
                    "result_status='prefiltered', completed_at=?, updated_at=? "
                    "WHERE message_row_id=?",
                    (queued_at, queued_at, int(completed["id"])),
                )
                database.connection.execute(
                    "UPDATE messages SET ai_status='error', "
                    "analysis_queue_state='failed', analysis_queue_requested=0, "
                    "push_gate_reason='recovery_increment_quarantined' WHERE id=?",
                    (int(quarantined["id"]),),
                )
                database.connection.execute(
                    "UPDATE analysis_jobs SET kind='manual', state='failed', attempts=1, "
                    "result_status='recovery_increment_quarantined', "
                    "error_category='interrupted', completed_at=?, updated_at=? "
                    "WHERE message_row_id=?",
                    (queued_at, queued_at, int(quarantined["id"])),
                )
            unrelated_job_before = database.connection.execute(
                "SELECT * FROM analysis_jobs WHERE message_row_id=?",
                (int(unrelated["id"]),),
            ).fetchone()
            database.close()

            manifest = {
                "schema": 1,
                "cutoff": to_iso(cutoff),
                "namespace_sha256": hashlib.sha256(namespace.encode()).hexdigest(),
                "fixed_count": 2,
                "targets": [
                    {
                        "chat_id": -1001,
                        "message_id": 301,
                        "source": "premerge_pending",
                        "needs_requeue": False,
                    },
                    {
                        "chat_id": -1001,
                        "message_id": 302,
                        "source": "first_start_pending",
                        "needs_requeue": True,
                    },
                ],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            report = requeue_recovery_targets(
                database_path=database_path,
                manifest_path=manifest_path,
                queued_at=queued_at,
                expected_fixed_targets=2,
                expected_requeue_targets=1,
            )
            self.assertEqual(report.already_terminal, 1)
            self.assertEqual(report.enqueued, 1)
            self.assertEqual(report.already_active, 0)
            self.assertEqual(report.deliveries_created, 0)

            reopened = Database(str(database_path), initialize=False)
            recovered = reopened.get_message(-1001, 302)
            self.assertEqual(recovered["analysis_queue_state"], "queued")
            self.assertEqual(recovered["analysis_queue_manual"], 0)
            self.assertEqual(recovered["push_gate_reason"], "recovery_reanalysis_queued")
            jobs = reopened.connection.execute(
                "SELECT kind,state,generation FROM analysis_jobs "
                "WHERE message_row_id=? ORDER BY generation",
                (int(recovered["id"]),),
            ).fetchall()
            self.assertEqual(
                [(row["kind"], row["state"], row["generation"]) for row in jobs],
                [("manual", "failed", 1), ("live", "queued", 2)],
            )
            unrelated_job_after = reopened.connection.execute(
                "SELECT * FROM analysis_jobs WHERE message_row_id=?",
                (int(unrelated["id"]),),
            ).fetchone()
            self.assertEqual(tuple(unrelated_job_before), tuple(unrelated_job_after))
            self.assertEqual(
                reopened.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],
                0,
            )
            self.assertEqual(
                reopened.connection.execute(
                    "SELECT value FROM metadata "
                    "WHERE key='shutdown_backlog_enqueued'"
                ).fetchone()[0],
                "1",
            )

            repeated_at = to_iso(cutoff + timedelta(minutes=9, seconds=1))
            repeated = requeue_recovery_targets(
                database_path=database_path,
                manifest_path=manifest_path,
                queued_at=repeated_at,
                expected_fixed_targets=2,
                expected_requeue_targets=1,
            )
            self.assertEqual(repeated.enqueued, 0)
            self.assertEqual(repeated.already_active, 1)
            self.assertEqual(
                reopened.connection.execute(
                    "SELECT COUNT(*) FROM analysis_jobs WHERE message_row_id=?",
                    (int(recovered["id"]),),
                ).fetchone()[0],
                2,
            )
            metadata = dict(
                reopened.connection.execute(
                    "SELECT key,value FROM metadata WHERE key LIKE 'shutdown_backlog_%'"
                ).fetchall()
            )
            self.assertEqual(metadata["shutdown_backlog_requeue_at"], queued_at)
            self.assertEqual(metadata["shutdown_backlog_enqueued"], "1")
            self.assertEqual(metadata["shutdown_backlog_last_run_at"], repeated_at)
            self.assertEqual(metadata["shutdown_backlog_last_run_enqueued"], "0")
            reopened.close()
