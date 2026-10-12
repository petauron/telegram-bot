from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


SCHEMA_KEY = "chat_activity_schema_version"
SCHEMA_VERSION = "1"


def initialize_chat_activity(connection: sqlite3.Connection) -> None:
    """Back up and apply the forward-only activity migration once.

    Seed only retained evidence. This is explicitly a partial historical
    baseline, not a reconstruction of deleted messages or subscription dates.
    No existing message is queued, reanalysed or redelivered.
    """
    version = connection.execute(
        "SELECT value FROM metadata WHERE key = ?", (SCHEMA_KEY,)
    ).fetchone()
    if version is not None:
        if version["value"] != SCHEMA_VERSION:
            raise RuntimeError("Unsupported chat activity schema; downgrade is not supported")
        return
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Collector and web may start together; recheck under the write lock.
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (SCHEMA_KEY,)
        ).fetchone()
        if version is not None:
            if version["value"] != SCHEMA_VERSION:
                raise RuntimeError("Unsupported chat activity schema")
            connection.commit()
            return
        database_path = connection.execute("PRAGMA database_list").fetchone()["file"]
        has_data = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM messages) OR EXISTS(SELECT 1 FROM runtime_config)"
        ).fetchone()[0]
        if database_path and has_data:
            # Hold the write reservation across backup and migration. Use a
            # separate read-only source: backing up the writer's own active
            # transaction can stall. The online backup includes WAL contents.
            path = Path(database_path)
            backup_path = path.with_name(f"{path.name}.pre-activity-v1-{uuid4().hex}.bak")
            backup_path.touch(mode=0o600, exist_ok=False)
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
                with sqlite3.connect(backup_path) as backup:
                    source.backup(backup)
                    if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("Chat activity migration backup integrity check failed")
        connection.execute(
            "ALTER TABLE messages ADD COLUMN media_text_unavailable INTEGER NOT NULL DEFAULT 0"
        )
        connection.execute("""
            CREATE TABLE chat_activity (
                chat_id INTEGER PRIMARY KEY,
                recorded_count INTEGER NOT NULL DEFAULT 0,
                successful_push_count INTEGER NOT NULL DEFAULT 0,
                media_unread_count INTEGER NOT NULL DEFAULT 0,
                last_received_at TEXT,
                last_successful_push_at TEXT,
                statistics_started_at TEXT NOT NULL,
                includes_partial_history INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0,
                watch_started_at TEXT,
                watch_start_known INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.execute("""
            CREATE TABLE chat_watch_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('baseline', 'enabled', 'disabled')),
                occurred_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE INDEX chat_watch_events_chat ON chat_watch_events(chat_id, id)
        """)
        connection.execute("""
            CREATE TABLE chat_push_receipts (
                message_row_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                delivered_at TEXT NOT NULL
            )
        """)
        # Historical file-only image metadata was never recognized. Do not
        # infer image contents, alter its old score, or restart its analysis.
        connection.execute("""
            UPDATE messages SET media_text_unavailable = 1
            WHERE source_type = 'telegram' AND text LIKE '文件名：%'
              AND instr(text, char(10)) = 0
              AND lower(text) != '文件名：sticker.webp'
              AND (lower(text) GLOB '*.jpg' OR lower(text) GLOB '*.jpeg'
                   OR lower(text) GLOB '*.png' OR lower(text) GLOB '*.webp')
        """)
        connection.execute("""
            INSERT INTO chat_push_receipts(message_row_id, delivered_at)
            SELECT message.id,
                   COALESCE(
                       (SELECT MIN(delivered_at) FROM deliveries
                        WHERE message_row_id = message.id AND state = 'succeeded'
                          AND delivered_at IS NOT NULL),
                       message.immediate_pushed_at, message.digest_pushed_at
                   )
            FROM messages AS message
            WHERE message.source_type = 'telegram'
              AND (message.immediate_pushed_at IS NOT NULL
                   OR message.digest_pushed_at IS NOT NULL
                   OR EXISTS(SELECT 1 FROM deliveries
                             WHERE message_row_id = message.id AND state = 'succeeded'
                               AND delivered_at IS NOT NULL))
        """)
        connection.execute("""
            INSERT INTO chat_activity(
                chat_id, recorded_count, successful_push_count, media_unread_count,
                last_received_at, last_successful_push_at, statistics_started_at,
                includes_partial_history
            )
            SELECT message.chat_id, COUNT(*), COUNT(receipt.message_row_id),
                   SUM(message.media_text_unavailable), MAX(message.created_at),
                   MAX(receipt.delivered_at), ?, 1
            FROM messages AS message
            LEFT JOIN chat_push_receipts AS receipt ON receipt.message_row_id = message.id
            WHERE message.source_type = 'telegram'
            GROUP BY message.chat_id
        """, (timestamp,))
        runtime = connection.execute(
            "SELECT watch_chat_ids_json FROM runtime_config WHERE id = 1"
        ).fetchone()
        watched = json.loads(runtime["watch_chat_ids_json"] or "[]") if runtime else []
        for chat_id in watched:
            connection.execute("""
                INSERT INTO chat_activity(
                    chat_id, statistics_started_at, includes_partial_history, active
                ) VALUES(?, ?, 1, 1)
                ON CONFLICT(chat_id) DO UPDATE SET active = 1, includes_partial_history = 1
            """, (int(chat_id), timestamp))
            connection.execute(
                "INSERT INTO chat_watch_events(chat_id, action, occurred_at) VALUES(?, 'baseline', ?)",
                (int(chat_id), timestamp),
            )
        # Persistent totals remain when messages and their receipts are removed.
        # Triggers cover all inserts and delivery writers in the same transaction.
        connection.execute("""
            CREATE TRIGGER chat_activity_recorded AFTER INSERT ON messages
            WHEN NEW.source_type = 'telegram'
            BEGIN
                INSERT INTO chat_activity(
                    chat_id, recorded_count, media_unread_count,
                    last_received_at, statistics_started_at
                ) VALUES(NEW.chat_id, 1, NEW.media_text_unavailable, NEW.created_at, NEW.created_at)
                ON CONFLICT(chat_id) DO UPDATE SET
                    recorded_count = recorded_count + 1,
                    media_unread_count = media_unread_count + NEW.media_text_unavailable,
                    last_received_at = MAX(COALESCE(last_received_at, ''), NEW.created_at);
            END
        """)
        connection.execute("""
            CREATE TRIGGER chat_activity_delivery AFTER UPDATE OF state, delivered_at ON deliveries
            WHEN NEW.state = 'succeeded' AND NEW.delivered_at IS NOT NULL
                 AND EXISTS(SELECT 1 FROM messages
                            WHERE id = NEW.message_row_id AND source_type = 'telegram')
            BEGIN
                INSERT OR IGNORE INTO chat_push_receipts(message_row_id, delivered_at)
                VALUES(NEW.message_row_id, NEW.delivered_at);
            END
        """)
        connection.execute("""
            CREATE TRIGGER chat_activity_push_counted AFTER INSERT ON chat_push_receipts
            BEGIN
                UPDATE chat_activity
                SET successful_push_count = successful_push_count + 1,
                    last_successful_push_at = MAX(
                        COALESCE(last_successful_push_at, ''), NEW.delivered_at
                    )
                WHERE chat_id = (SELECT chat_id FROM messages WHERE id = NEW.message_row_id);
            END
        """)
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?)", (SCHEMA_KEY, SCHEMA_VERSION)
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def record_watch_changes(
    connection: sqlite3.Connection,
    previous: frozenset[int],
    selected: frozenset[int],
    timestamp: str,
) -> None:
    """Called in the same transaction as the persisted watch configuration."""
    for chat_id in sorted(previous ^ selected):
        enabled = chat_id in selected
        connection.execute("""
            INSERT INTO chat_activity(
                chat_id, statistics_started_at, active, watch_started_at, watch_start_known
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                active = excluded.active,
                watch_started_at = CASE WHEN excluded.active THEN excluded.watch_started_at
                                        ELSE watch_started_at END,
                watch_start_known = CASE WHEN excluded.active THEN 1 ELSE watch_start_known END
        """, (chat_id, timestamp, int(enabled), timestamp if enabled else None, int(enabled)))
        connection.execute(
            "INSERT INTO chat_watch_events(chat_id, action, occurred_at) VALUES(?, ?, ?)",
            (chat_id, "enabled" if enabled else "disabled", timestamp),
        )
