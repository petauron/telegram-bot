from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ACTIVE_JOB_STATES = frozenset({"queued", "processing", "retry"})
ACTIVE_MESSAGE_STATES = frozenset({"pending", "queued", "processing", "retry"})
TEST_CHAT_ID = -1001234567890


@dataclass(frozen=True)
class RecoveryMergeReport:
    messages_merged: int
    replies_merged: int
    completed_jobs_preserved: int
    incomplete_jobs_quarantined: int
    base_messages_preserved: int
    base_post_cutoff_messages_preserved: int
    deliveries_created: int
    integrity_check: str
    foreign_key_violations: int
    output_sha256: str


@dataclass(frozen=True)
class RecoveryRequeueReport:
    fixed_targets: int
    already_terminal: int
    enqueued: int
    already_active: int
    pre_cutoff_active_jobs: int
    deliveries_created: int


def _connect(path: Path, *, immutable: bool) -> sqlite3.Connection:
    suffix = "?immutable=1" if immutable else ""
    connection = sqlite3.connect(f"file:{path}{suffix}", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _single(
    connection: sqlite3.Connection,
    sql: str,
    values: tuple[object, ...] = (),
) -> Any:
    return connection.execute(sql, values).fetchone()[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _is_test_fixture(row: sqlite3.Row) -> bool:
    return int(row["chat_id"]) == TEST_CHAT_ID and (
        str(row["chat_name"] or "") in {"测试群", "去标识会话"}
        or str(row["chat_username"] or "") == "test_group"
        or str(row["sender_name"] or "") in {"测试发送者", "匿名"}
    )


def _message_key(row: sqlite3.Row) -> tuple[int, int]:
    return int(row["chat_id"]), int(row["message_id"])


def _reply_key(row: sqlite3.Row) -> tuple[int, int, int]:
    return int(row["chat_id"]), int(row["parent_message_id"]), int(row["sender_id"])


def _validate_database(connection: sqlite3.Connection) -> tuple[str, int]:
    integrity = str(_single(connection, "PRAGMA integrity_check"))
    foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    _require(integrity == "ok", f"SQLite integrity_check failed: {integrity}")
    _require(foreign_key_violations == 0, "SQLite foreign_key_check failed")
    return integrity, foreign_key_violations


def merge_recovery_increment(
    *,
    base_path: str | Path,
    increment_path: str | Path,
    output_path: str | Path,
    merged_at: str,
    expected_messages: int,
    expected_replies: int,
) -> RecoveryMergeReport:
    """Merge a verified post-cutoff increment without replaying notifications.

    Existing rows from ``base_path`` are never updated. Missing Telegram rows use
    their natural key and receive fresh SQLite primary keys. Completed audit jobs
    remain terminal; unfinished work becomes a terminal manual quarantine so it
    can later be reanalysed explicitly without creating historical deliveries.
    """

    base_path = Path(base_path)
    increment_path = Path(increment_path)
    output_path = Path(output_path)
    _require(base_path.is_file(), "Base database is missing")
    _require(increment_path.is_file(), "Increment database is missing")
    _require(not output_path.exists(), "Refusing to overwrite recovery output")
    _require(expected_messages > 0, "Expected message count must be positive")
    _require(expected_replies >= 0, "Expected reply count must be non-negative")

    base = _connect(base_path, immutable=True)
    increment = _connect(increment_path, immutable=True)
    output = sqlite3.connect(output_path)
    output.row_factory = sqlite3.Row
    output.execute("PRAGMA foreign_keys=ON")
    try:
        _validate_database(base)
        _validate_database(increment)
        base.backup(output)
        output.commit()
        output.execute("PRAGMA foreign_keys=ON")

        message_columns = _table_columns(base, "messages")
        _require(
            message_columns == _table_columns(increment, "messages"),
            "Message schemas differ",
        )
        job_columns = _table_columns(base, "analysis_jobs")
        _require(
            job_columns == _table_columns(increment, "analysis_jobs"),
            "Analysis-job schemas differ",
        )

        cutoff = str(
            _single(
                base,
                "SELECT value FROM metadata WHERE key='recovery_delivery_cutoff_at'",
            )
        )
        increment_cutoff = str(
            _single(
                increment,
                "SELECT value FROM metadata WHERE key='recovery_delivery_cutoff_at'",
            )
        )
        namespace = str(
            _single(
                base,
                "SELECT value FROM metadata WHERE key='llm_session_namespace_v1'",
            )
        )
        increment_namespace = str(
            _single(
                increment,
                "SELECT value FROM metadata WHERE key='llm_session_namespace_v1'",
            )
        )
        _require(cutoff == increment_cutoff, "Recovery cutoffs differ")
        _require(bool(namespace) and namespace == increment_namespace, "LLM namespaces differ")

        base_message_count = int(_single(base, "SELECT COUNT(*) FROM messages"))
        base_reply_count = int(_single(base, "SELECT COUNT(*) FROM replies"))
        base_job_count = int(_single(base, "SELECT COUNT(*) FROM analysis_jobs"))
        base_delivery_count = int(_single(base, "SELECT COUNT(*) FROM deliveries"))
        base_post_cutoff_count = int(
            _single(base, "SELECT COUNT(*) FROM messages WHERE created_at > ?", (cutoff,))
        )
        base_keys = {
            _message_key(row)
            for row in base.execute("SELECT chat_id, message_id FROM messages")
        }
        base_reply_keys = {
            _reply_key(row)
            for row in base.execute(
                "SELECT chat_id, parent_message_id, sender_id FROM replies"
            )
        }
        missing_messages = [
            row
            for row in increment.execute("SELECT * FROM messages ORDER BY sent_at, message_id, id")
            if _message_key(row) not in base_keys
        ]
        missing_replies = [
            row
            for row in increment.execute(
                "SELECT * FROM replies ORDER BY replied_at, chat_id, parent_message_id, sender_id"
            )
            if _reply_key(row) not in base_reply_keys
        ]
        _require(
            len(missing_messages) == expected_messages,
            "Unexpected missing-message set",
        )
        _require(
            len(missing_replies) == expected_replies,
            "Unexpected missing-reply set",
        )
        _require(not any(_is_test_fixture(row) for row in missing_messages), "Test message found in increment")
        _require(
            all(
                str(row["created_at"]) > cutoff
                and str(row["sent_at"]) > cutoff
                and str(row["source_type"] or "") == "telegram"
                for row in missing_messages
            ),
            "Increment contains non-Telegram or pre-cutoff messages",
        )
        _require(
            all(str(row["replied_at"]) > cutoff and int(row["chat_id"]) != TEST_CHAT_ID for row in missing_replies),
            "Increment contains test or pre-cutoff replies",
        )
        _require(
            all(
                row["source_id"] is None
                and row["semantic_dedupe_matched_message_id"] is None
                and not bool(row["push_eligible"])
                and row["immediate_pushed_at"] is None
                and row["digest_pushed_at"] is None
                for row in missing_messages
            ),
            "Increment contains a source reference or prior push state",
        )

        old_ids = tuple(int(row["id"]) for row in missing_messages)
        placeholders = ",".join("?" for _ in old_ids)
        related_deliveries = int(
            _single(
                increment,
                f"SELECT COUNT(*) FROM deliveries WHERE message_row_id IN ({placeholders})",
                old_ids,
            )
        )
        related_feedback = int(
            _single(
                increment,
                f"SELECT (SELECT COUNT(*) FROM feedback_records WHERE message_row_id IN ({placeholders})) + "
                f"(SELECT COUNT(*) FROM ntfy_feedback_targets WHERE message_row_id IN ({placeholders}))",
                (*old_ids, *old_ids),
            )
        )
        _require(related_deliveries == 0, "Increment already has deliveries")
        _require(related_feedback == 0, "Increment has feedback references")

        old_to_new: dict[int, int] = {}
        completed_jobs = 0
        quarantined_jobs = 0
        insert_columns = tuple(column for column in message_columns if column != "id")
        insert_sql = (
            f"INSERT INTO messages({','.join(insert_columns)}) "
            f"VALUES({','.join('?' for _ in insert_columns)})"
        )
        output.execute("BEGIN IMMEDIATE")
        try:
            for source_row in missing_messages:
                values = dict(source_row)
                is_active = (
                    str(values.get("analysis_queue_state") or "") in ACTIVE_JOB_STATES
                    or str(values.get("ai_status") or "") in ACTIVE_MESSAGE_STATES
                )
                if is_active:
                    values.update(
                        {
                            "analysis_queue_requested": 0,
                            "analysis_queue_state": "failed",
                            "analysis_queue_available_at": None,
                            "analysis_queue_error_category": "interrupted",
                            "analysis_queue_manual": 1,
                            "ai_status": "error",
                            "ai_completed_at": merged_at,
                            "ai_error_category": "interrupted",
                            "ai_error_stage": "queue",
                            "push_eligible": 0,
                            "push_ready_at": None,
                            "push_gate_reason": "recovery_increment_quarantined",
                            "semantic_dedupe_status": "not_required_analysis_error",
                            "notification_prepare_status": "not_required_analysis_error",
                        }
                    )
                cursor = output.execute(
                    insert_sql,
                    tuple(values[column] for column in insert_columns),
                )
                old_to_new[int(source_row["id"])] = int(cursor.lastrowid)

            reply_columns = _table_columns(base, "replies")
            reply_sql = (
                f"INSERT INTO replies({','.join(reply_columns)}) "
                f"VALUES({','.join('?' for _ in reply_columns)})"
            )
            for row in missing_replies:
                output.execute(reply_sql, tuple(row[column] for column in reply_columns))

            for old_id, new_id in old_to_new.items():
                source_jobs = increment.execute(
                    "SELECT * FROM analysis_jobs WHERE message_row_id = ? ORDER BY generation",
                    (old_id,),
                ).fetchall()
                _require(len(source_jobs) == 1, "Increment message must have exactly one analysis job")
                source_job = dict(source_jobs[0])
                active = str(source_job["state"]) in ACTIVE_JOB_STATES
                job_values = source_job.copy()
                job_values["message_row_id"] = new_id
                if active:
                    job_values.update(
                        {
                            "kind": "manual",
                            "state": "failed",
                            "lease_until": None,
                            "error_category": "interrupted",
                            "error_stage": "queue",
                            "result_status": "recovery_increment_quarantined",
                            "updated_at": merged_at,
                            "completed_at": merged_at,
                        }
                    )
                    quarantined_jobs += 1
                else:
                    _require(
                        str(source_job["state"]) in {"succeeded", "failed"},
                        "Unexpected terminal analysis-job state",
                    )
                    completed_jobs += 1
                copy_columns = tuple(column for column in job_columns if column != "id")
                output.execute(
                    f"INSERT INTO analysis_jobs({','.join(copy_columns)}) "
                    f"VALUES({','.join('?' for _ in copy_columns)})",
                    tuple(job_values[column] for column in copy_columns),
                )

            metadata = {
                "recovery_increment_merge_at": merged_at,
                "recovery_increment_messages": str(expected_messages),
                "recovery_increment_replies": str(expected_replies),
                "recovery_increment_delivery_policy": "manual_quarantine_no_historical_delivery",
            }
            output.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                metadata.items(),
            )
            output.commit()
        except Exception:
            output.rollback()
            raise

        output_keys = {
            _message_key(row)
            for row in output.execute("SELECT chat_id, message_id FROM messages")
        }
        expected_keys = base_keys | {_message_key(row) for row in missing_messages}
        output_reply_keys = {
            _reply_key(row)
            for row in output.execute(
                "SELECT chat_id, parent_message_id, sender_id FROM replies"
            )
        }
        expected_reply_keys = base_reply_keys | {_reply_key(row) for row in missing_replies}
        _require(output_keys == expected_keys, "Merged message set differs from exact union")
        _require(output_reply_keys == expected_reply_keys, "Merged reply set differs from exact union")
        _require(
            int(_single(output, "SELECT COUNT(*) FROM messages"))
            == base_message_count + expected_messages,
            "Merged message row count is incorrect",
        )
        _require(
            int(_single(output, "SELECT COUNT(*) FROM replies"))
            == base_reply_count + expected_replies,
            "Merged reply row count is incorrect",
        )
        _require(
            int(_single(output, "SELECT COUNT(*) FROM analysis_jobs"))
            == base_job_count + expected_messages,
            "Merged analysis-job row count is incorrect",
        )
        _require(
            int(_single(output, "SELECT COUNT(*) FROM deliveries")) == base_delivery_count,
            "Recovery merge created a delivery",
        )
        _require(
            int(
                _single(
                    output,
                    "SELECT COUNT(*) FROM messages WHERE created_at > ?",
                    (cutoff,),
                )
            )
            == base_post_cutoff_count + expected_messages,
            "Base post-cutoff flow was not preserved",
        )
        merged_ids = tuple(old_to_new.values())
        merged_placeholders = ",".join("?" for _ in merged_ids)
        _require(
            int(
                _single(
                    output,
                    f"SELECT COUNT(*) FROM deliveries WHERE message_row_id IN ({merged_placeholders})",
                    merged_ids,
                )
            )
            == 0,
            "Merged increment became deliverable",
        )
        _require(
            int(
                _single(
                    output,
                    f"SELECT COUNT(*) FROM messages WHERE id IN ({merged_placeholders}) AND push_eligible != 0",
                    merged_ids,
                )
            )
            == 0,
            "Merged increment is push eligible",
        )
        _require(
            int(
                _single(
                    output,
                    f"SELECT COUNT(*) FROM analysis_jobs WHERE message_row_id IN ({merged_placeholders}) "
                    "AND state IN ('queued','processing','retry')",
                    merged_ids,
                )
            )
            == 0,
            "Merged increment has active jobs",
        )
        _require(
            int(
                _single(
                    output,
                    "SELECT COUNT(*) FROM deliveries AS delivery JOIN messages AS message "
                    "ON message.id=delivery.message_row_id WHERE message.created_at <= ?",
                    (cutoff,),
                )
            )
            == 0,
            "Pre-cutoff delivery exists",
        )
        integrity, foreign_keys = _validate_database(output)
        output.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        output.commit()
    finally:
        output.close()
        increment.close()
        base.close()

    os.chmod(output_path, 0o600)
    return RecoveryMergeReport(
        messages_merged=expected_messages,
        replies_merged=expected_replies,
        completed_jobs_preserved=completed_jobs,
        incomplete_jobs_quarantined=quarantined_jobs,
        base_messages_preserved=base_message_count,
        base_post_cutoff_messages_preserved=base_post_cutoff_count,
        deliveries_created=0,
        integrity_check=integrity,
        foreign_key_violations=foreign_keys,
        output_sha256=_sha256(output_path),
    )


def requeue_recovery_targets(
    *,
    database_path: str | Path,
    manifest_path: str | Path,
    queued_at: str,
    expected_fixed_targets: int,
    expected_requeue_targets: int,
) -> RecoveryRequeueReport:
    """Idempotently enqueue a reviewed post-cutoff recovery set as live work.

    The private manifest contains only natural Telegram keys. Completed targets
    remain untouched. A target marked for recovery must still be the exact
    terminal quarantine row created by the incident recovery and must have no
    delivery. New work uses the normal live queue so classification, scoring,
    semantic dedupe, notification preparation, and configured channel delivery
    all retain their production semantics.
    """

    database_path = Path(database_path)
    manifest_path = Path(manifest_path)
    _require(database_path.is_file(), "Recovery queue database is missing")
    _require(manifest_path.is_file(), "Recovery target manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _require(manifest.get("schema") == 1, "Unsupported recovery target manifest")
    targets = manifest.get("targets")
    _require(isinstance(targets, list), "Recovery target manifest is malformed")
    _require(len(targets) == expected_fixed_targets, "Unexpected fixed target count")
    _require(
        int(manifest.get("fixed_count") or 0) == expected_fixed_targets,
        "Manifest fixed target count differs",
    )
    keys = [
        (int(target["chat_id"]), int(target["message_id"]))
        for target in targets
    ]
    _require(len(set(keys)) == len(keys), "Recovery target manifest has duplicates")
    needs_requeue = [bool(target.get("needs_requeue")) for target in targets]
    _require(
        sum(needs_requeue) == expected_requeue_targets,
        "Unexpected recovery requeue target count",
    )
    _require(
        {str(target.get("source")) for target in targets}
        <= {"first_start_pending", "premerge_pending"},
        "Recovery manifest contains an unknown source",
    )

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    already_terminal = 0
    enqueued = 0
    already_active = 0
    try:
        _validate_database(connection)
        cutoff = str(
            _single(
                connection,
                "SELECT value FROM metadata WHERE key='recovery_delivery_cutoff_at'",
            )
        )
        namespace = str(
            _single(
                connection,
                "SELECT value FROM metadata WHERE key='llm_session_namespace_v1'",
            )
        )
        _require(cutoff == str(manifest.get("cutoff")), "Recovery cutoff differs")
        _require(
            hashlib.sha256(namespace.encode()).hexdigest()
            == str(manifest.get("namespace_sha256")),
            "LLM namespace differs",
        )
        deliveries_before = int(_single(connection, "SELECT COUNT(*) FROM deliveries"))
        connection.execute("BEGIN IMMEDIATE")
        try:
            for target, should_requeue in zip(targets, needs_requeue, strict=True):
                chat_id = int(target["chat_id"])
                message_id = int(target["message_id"])
                row = connection.execute(
                    "SELECT * FROM messages WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                ).fetchone()
                _require(row is not None, "Fixed recovery target is missing")
                _require(
                    str(row["created_at"]) > cutoff
                    and str(row["source_type"] or "") == "telegram",
                    "Recovery target is pre-cutoff or non-Telegram",
                )
                _require(not _is_test_fixture(row), "Test fixture found in recovery target")
                active = connection.execute(
                    "SELECT id FROM analysis_jobs WHERE message_row_id=? "
                    "AND state IN ('queued','processing','retry') LIMIT 1",
                    (int(row["id"]),),
                ).fetchone()
                terminal = (
                    str(row["analysis_queue_state"] or "") == "succeeded"
                    and str(row["ai_status"] or "")
                    in {"success", "prefiltered", "filtered_non_information"}
                )
                if not should_requeue:
                    _require(terminal, "Reviewed completed target is no longer terminal")
                    _require(active is None, "Reviewed completed target has active work")
                    already_terminal += 1
                    continue
                if terminal:
                    _require(active is None, "Completed recovery target has active work")
                    already_terminal += 1
                    continue
                if active is not None:
                    already_active += 1
                    continue
                _require(
                    str(row["analysis_queue_state"] or "") == "failed"
                    and str(row["push_gate_reason"] or "")
                    == "recovery_increment_quarantined"
                    and not bool(row["push_eligible"]),
                    "Recovery target is not in the reviewed quarantine state",
                )
                _require(
                    int(
                        _single(
                            connection,
                            "SELECT COUNT(*) FROM analysis_jobs WHERE message_row_id=? "
                            "AND state='failed' "
                            "AND result_status='recovery_increment_quarantined'",
                            (int(row["id"]),),
                        )
                    )
                    == 1,
                    "Recovery target lacks its terminal quarantine job",
                )
                _require(
                    int(
                        _single(
                            connection,
                            "SELECT COUNT(*) FROM deliveries WHERE message_row_id=?",
                            (int(row["id"]),),
                        )
                    )
                    == 0,
                    "Recovery target already has a delivery",
                )
                generation = int(
                    _single(
                        connection,
                        "SELECT COALESCE(MAX(generation),0)+1 FROM analysis_jobs "
                        "WHERE message_row_id=?",
                        (int(row["id"]),),
                    )
                )
                connection.execute(
                    """
                    INSERT INTO analysis_jobs(
                        message_row_id, chat_id, kind, generation, state,
                        attempts, max_attempts, available_at, created_at, updated_at
                    ) VALUES(?, ?, 'live', ?, 'queued', 0, 5, ?, ?, ?)
                    """,
                    (
                        int(row["id"]),
                        chat_id,
                        generation,
                        queued_at,
                        queued_at,
                        queued_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE messages
                    SET analysis_queue_requested=1,
                        analysis_queue_state='queued',
                        analysis_queue_attempts=0,
                        analysis_queue_available_at=?,
                        analysis_queue_error_category=NULL,
                        analysis_queue_manual=0,
                        ai_status='queued',
                        ai_error_category=NULL,
                        ai_error_stage=NULL,
                        push_eligible=0,
                        push_gate_reason='recovery_reanalysis_queued',
                        push_ready_at=NULL,
                        semantic_dedupe_status='awaiting_analysis',
                        semantic_dedupe_model=NULL,
                        semantic_dedupe_effort=NULL,
                        semantic_dedupe_confidence=NULL,
                        semantic_dedupe_reason=NULL,
                        semantic_dedupe_response_text=NULL,
                        semantic_dedupe_matched_message_id=NULL,
                        semantic_dedupe_material_update=0,
                        semantic_dedupe_update_type=NULL,
                        semantic_dedupe_update_validated=0,
                        semantic_dedupe_update_rejection_reason=NULL,
                        semantic_dedupe_checked_at=NULL,
                        semantic_dedupe_error_category=NULL,
                        semantic_dedupe_candidate_count=0,
                        notification_prepare_status='awaiting_analysis',
                        notification_prepare_model=NULL,
                        notification_prepare_effort=NULL,
                        notification_title=NULL,
                        notification_body=NULL,
                        notification_prepare_response_text=NULL,
                        notification_prepare_checked_at=NULL,
                        notification_prepare_error_category=NULL,
                        feedback_context_sample_count=0,
                        feedback_context_summary=NULL,
                        feedback_context_json=NULL,
                        feedback_context_applied_at=NULL
                    WHERE id=?
                    """,
                    (queued_at, int(row["id"])),
                )
                enqueued += 1
            first_requeue_row = connection.execute(
                "SELECT value FROM metadata "
                "WHERE key='shutdown_backlog_requeue_at'"
            ).fetchone()
            recorded_enqueued_row = connection.execute(
                "SELECT value FROM metadata "
                "WHERE key='shutdown_backlog_enqueued'"
            ).fetchone()
            first_requeue_at = (
                str(first_requeue_row["value"])
                if first_requeue_row is not None
                else queued_at
            )
            recorded_enqueued = max(
                int(recorded_enqueued_row["value"])
                if recorded_enqueued_row is not None
                else 0,
                enqueued,
            )
            connection.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    ("shutdown_backlog_requeue_at", first_requeue_at),
                    ("shutdown_backlog_fixed_targets", str(expected_fixed_targets)),
                    ("shutdown_backlog_enqueued", str(recorded_enqueued)),
                    ("shutdown_backlog_manifest_sha256", _sha256(manifest_path)),
                    ("shutdown_backlog_last_run_at", queued_at),
                    ("shutdown_backlog_last_run_enqueued", str(enqueued)),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        deliveries_after = int(_single(connection, "SELECT COUNT(*) FROM deliveries"))
        pre_cutoff_active = int(
            _single(
                connection,
                "SELECT COUNT(*) FROM analysis_jobs AS job "
                "JOIN messages AS message ON message.id=job.message_row_id "
                "WHERE message.created_at<=? "
                "AND job.state IN ('queued','processing','retry')",
                (cutoff,),
            )
        )
        _require(pre_cutoff_active == 0, "Pre-cutoff analysis work was activated")
        _require(
            deliveries_after == deliveries_before,
            "Recovery requeue unexpectedly created a delivery",
        )
        _validate_database(connection)
    finally:
        connection.close()

    return RecoveryRequeueReport(
        fixed_targets=expected_fixed_targets,
        already_terminal=already_terminal,
        enqueued=enqueued,
        already_active=already_active,
        pre_cutoff_active_jobs=pre_cutoff_active,
        deliveries_created=deliveries_after - deliveries_before,
    )
