from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from app.feedback import (
    FEEDBACK_CONTENT_LABELS,
    FEEDBACK_RECORD_SCAN_LIMIT,
    FEEDBACK_WINDOW_DAYS,
    FeedbackGuidance,
    build_feedback_guidance,
    feedback_interest_tags,
    feedback_source_key,
)
from app.llm import (
    AnalysisOutcome,
    BENEFIT_CONFIDENCE_THRESHOLD,
    BenefitDealOutcome,
    ClassificationOutcome,
    COMMUNITY_CONFIDENCE_THRESHOLD,
    CommunityInsightOutcome,
    DEFAULT_BASE_URL,
    DEFAULT_CLASSIFICATION_MODEL,
    DEFAULT_CLASSIFICATION_REASONING_EFFORT,
    DEFAULT_NOTIFICATION_MODEL,
    DEFAULT_NOTIFICATION_REASONING_EFFORT,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_SEMANTIC_DEDUPE_MODEL,
    DEFAULT_SEMANTIC_DEDUPE_REASONING_EFFORT,
    LOCAL_BENEFIT_GATE_REASON,
    LOCAL_COMMUNITY_GATE_REASON,
    LOCAL_GATE_MODEL,
    MAX_INPUT_TEXT_LENGTH,
    NotificationPreparationOutcome,
    SemanticDedupeOutcome,
    category_label,
    validate_reasoning_effort,
)
from app.llm_context import (
    CommunityGateEvidence,
    COMMUNITY_CONTEXT_RELEVANCE_HOURS,
    COMMUNITY_CONTEXT_SCAN_LIMIT,
    RECENT_CONTEXT_CHAR_LIMIT,
    RECENT_CONTEXT_LIMIT,
    RECENT_CONTEXT_RELEVANCE_HOURS,
    RECENT_CONTEXT_SCAN_LIMIT,
    SESSION_NAMESPACE_BYTES,
    build_community_gate_evidence,
    build_community_context,
    build_recent_context,
    community_has_action_result,
    community_has_corroboration,
    community_has_incident_cue,
    derive_chat_session_key,
    derive_dedupe_session_key,
)
from app.prefilter import (
    PrefilterResult,
    evaluate_prefilter,
    evaluate_prequeue_prefilter,
    has_protected_signal,
)
from app.push_config import (
    DEFAULT_NTFY_BASE_URL,
    validate_ntfy_base_url,
    validate_ntfy_topic,
    validate_push_chat_id,
    validate_push_secret,
)
from app.scoring import reply_bonus
from app.semantic_update import validate_semantic_update


PUSH_SCORE_THRESHOLD = 60
# Kept as an internal compatibility alias for the existing analysis gate and
# historical schema helpers. New delivery code uses PUSH_SCORE_THRESHOLD.
DIGEST_MIN_AI_SCORE = PUSH_SCORE_THRESHOLD
SEMANTIC_DEDUPE_WINDOW_HOURS = 24
SEMANTIC_DEDUPE_QUERY_LIMIT = 200
SEMANTIC_DEDUPE_CONFIDENCE_THRESHOLD = 85
ANALYSIS_DEDUPE_WINDOW_HOURS = 72
ANALYSIS_DEDUPE_MIN_NORMALIZED_CHARS = 12
ANALYSIS_RAPID_DEDUPE_MIN_NORMALIZED_CHARS = 5
ANALYSIS_RAPID_DEDUPE_WINDOW_MINUTES = 2
INFORMATION_SOURCE_CHAT_ID_BASE = 4_000_000_000_000_000_000
NTFY_FEEDBACK_TARGET_TTL_SECONDS = 7 * 24 * 60 * 60
RECOVERY_DELIVERY_CUTOFF_KEY = "recovery_delivery_cutoff_at"
RECOVERY_HISTORY_PRESERVE_THROUGH_KEY = "recovery_history_preserve_through"
QUEUE_METRIC_SAMPLE_INTERVAL_SECONDS = 10
QUEUE_METRIC_RETENTION_DAYS = 3


def information_source_chat_id(source_id: int) -> int:
    value = INFORMATION_SOURCE_CHAT_ID_BASE + int(source_id)
    if value >= 9_000_000_000_000_000_000:
        raise ValueError("信息源数量超出安全范围")
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class MessageRecord:
    chat_id: int
    message_id: int
    chat_name: str
    chat_username: str | None
    sender_id: int | None
    sender_name: str
    sent_at: str
    text: str
    reply_to_message_id: int | None
    thread_root_id: int
    base_score: int
    reasons: tuple[str, ...]
    link: str | None
    normalized_text: str
    primary_url: str | None
    created_at: str
    is_service_message: bool = False
    source_type: str = "telegram"
    source_id: int | None = None
    source_external_id: str | None = None


class Database:
    def __init__(self, path: str, *, initialize: bool = True) -> None:
        if initialize:
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        if initialize:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self._initialize_schema()

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
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
                local_score INTEGER NOT NULL,
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
                ai_status TEXT NOT NULL DEFAULT 'not_analyzed',
                ai_model TEXT,
                ai_score INTEGER,
                ai_summary TEXT,
                ai_reason TEXT,
                ai_response_text TEXT,
                ai_started_at TEXT,
                ai_completed_at TEXT,
                ai_error_category TEXT,
                ai_error_stage TEXT,
                ai_category TEXT,
                ai_category_confidence INTEGER,
                ai_category_summary TEXT,
                ai_category_reason TEXT,
                ai_category_response_text TEXT,
                ai_classification_model TEXT,
                ai_classification_effort TEXT,
                ai_classification_batch_id INTEGER,
                ai_classification_call_id INTEGER,
                ai_scoring_effort TEXT,
                content_kind TEXT,
                community_status TEXT NOT NULL DEFAULT 'not_analyzed',
                community_signal_type TEXT,
                community_confidence INTEGER,
                community_title TEXT,
                community_summary TEXT,
                community_reason TEXT,
                community_response_text TEXT,
                community_model TEXT,
                community_effort TEXT,
                community_evidence_count INTEGER,
                community_checked_at TEXT,
                community_error_category TEXT,
                benefit_status TEXT NOT NULL DEFAULT 'not_analyzed',
                benefit_type TEXT,
                benefit_confidence INTEGER,
                benefit_title TEXT,
                benefit_summary TEXT,
                benefit_reason TEXT,
                benefit_response_text TEXT,
                benefit_model TEXT,
                benefit_effort TEXT,
                benefit_checked_at TEXT,
                benefit_error_category TEXT,
                prefilter_status TEXT NOT NULL DEFAULT 'not_evaluated',
                prefilter_reason_code TEXT,
                prefilter_reason TEXT,
                push_eligible INTEGER NOT NULL DEFAULT 0 CHECK (push_eligible IN (0, 1)),
                push_gate_reason TEXT NOT NULL DEFAULT 'historical_unreviewed',
                push_ready_at TEXT,
                semantic_dedupe_status TEXT NOT NULL DEFAULT 'historical_unreviewed',
                semantic_dedupe_model TEXT,
                semantic_dedupe_effort TEXT,
                semantic_dedupe_confidence INTEGER,
                semantic_dedupe_reason TEXT,
                semantic_dedupe_response_text TEXT,
                semantic_dedupe_matched_message_id INTEGER,
                semantic_dedupe_material_update INTEGER NOT NULL DEFAULT 0,
                semantic_dedupe_update_type TEXT,
                semantic_dedupe_update_validated INTEGER NOT NULL DEFAULT 0,
                semantic_dedupe_update_rejection_reason TEXT,
                semantic_dedupe_checked_at TEXT,
                semantic_dedupe_error_category TEXT,
                semantic_dedupe_candidate_count INTEGER NOT NULL DEFAULT 0,
                notification_prepare_status TEXT NOT NULL DEFAULT 'historical_unprepared',
                notification_prepare_model TEXT,
                notification_prepare_effort TEXT,
                notification_title TEXT,
                notification_body TEXT,
                notification_prepare_response_text TEXT,
                notification_prepare_checked_at TEXT,
                notification_prepare_error_category TEXT,
                analysis_queue_requested INTEGER NOT NULL DEFAULT 0
                    CHECK (analysis_queue_requested IN (0, 1)),
                analysis_queue_state TEXT,
                analysis_queue_attempts INTEGER NOT NULL DEFAULT 0,
                analysis_queue_available_at TEXT,
                analysis_queue_error_category TEXT,
                analysis_queue_manual INTEGER NOT NULL DEFAULT 0
                    CHECK (analysis_queue_manual IN (0, 1)),
                is_service_message INTEGER NOT NULL DEFAULT 0
                    CHECK (is_service_message IN (0, 1)),
                source_type TEXT NOT NULL DEFAULT 'telegram',
                source_id INTEGER,
                source_external_id TEXT,
                feedback_context_sample_count INTEGER NOT NULL DEFAULT 0,
                feedback_context_summary TEXT,
                feedback_context_json TEXT,
                feedback_context_applied_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(chat_id, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_created
                ON messages(created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_digest
                ON messages(digest_considered_at, immediate_pushed_at, score, created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_normalized
                ON messages(chat_id, normalized_text, created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_llm_history
                ON messages(chat_id, sent_at DESC, message_id DESC, id DESC);
            CREATE TABLE IF NOT EXISTS replies (
                chat_id INTEGER NOT NULL,
                parent_message_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                replied_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, parent_message_id, sender_id)
            );
            CREATE INDEX IF NOT EXISTS idx_replies_window
                ON replies(chat_id, parent_message_id, replied_at);

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS queue_metric_samples (
                sampled_at TEXT PRIMARY KEY,
                analysis_pending INTEGER NOT NULL DEFAULT 0,
                analysis_processing INTEGER NOT NULL DEFAULT 0,
                analysis_retry INTEGER NOT NULL DEFAULT 0,
                analysis_failed INTEGER NOT NULL DEFAULT 0,
                delivery_pending INTEGER NOT NULL DEFAULT 0,
                delivery_processing INTEGER NOT NULL DEFAULT 0,
                delivery_retry INTEGER NOT NULL DEFAULT 0,
                delivery_failed INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_queue_metric_samples_time
                ON queue_metric_samples(sampled_at);

            CREATE TABLE IF NOT EXISTS runtime_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                important_keywords_json TEXT NOT NULL,
                trusted_sender_ids_json TEXT NOT NULL,
                watch_chat_ids_json TEXT NOT NULL,
                immediate_score INTEGER NOT NULL CHECK (immediate_score >= 0),
                model_enabled INTEGER NOT NULL DEFAULT 0 CHECK (model_enabled IN (0, 1)),
                model_base_url TEXT NOT NULL DEFAULT 'https://model.example.com/v1',
                model_api_key TEXT,
                model_classification_model TEXT NOT NULL DEFAULT 'gemini-3.5-flash-extra-low',
                model_classification_reasoning_effort TEXT NOT NULL DEFAULT 'low'
                    CHECK (model_classification_reasoning_effort IN ('default', 'low', 'medium', 'high')),
                model_model TEXT,
                model_reasoning_effort TEXT NOT NULL DEFAULT 'default'
                    CHECK (model_reasoning_effort IN ('default', 'low', 'medium', 'high')),
                model_semantic_dedupe_model TEXT,
                model_semantic_dedupe_reasoning_effort TEXT NOT NULL DEFAULT 'low'
                    CHECK (model_semantic_dedupe_reasoning_effort IN ('default', 'low', 'medium', 'high')),
                model_notification_model TEXT,
                model_notification_reasoning_effort TEXT NOT NULL DEFAULT 'low'
                    CHECK (model_notification_reasoning_effort IN ('default', 'low', 'medium', 'high')),
                community_insights_enabled INTEGER NOT NULL DEFAULT 1
                    CHECK (community_insights_enabled IN (0, 1)),
                benefit_deals_enabled INTEGER NOT NULL DEFAULT 1
                    CHECK (benefit_deals_enabled IN (0, 1)),
                model_updated_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS push_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                telegram_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK (telegram_enabled IN (0, 1)),
                telegram_bot_token TEXT,
                telegram_chat_id TEXT,
                ntfy_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK (ntfy_enabled IN (0, 1)),
                ntfy_base_url TEXT NOT NULL DEFAULT 'https://ntfy.example.com',
                ntfy_topic TEXT,
                ntfy_community_topic TEXT,
                ntfy_benefit_topic TEXT,
                ntfy_access_token TEXT,
                ntfy_feedback_topic TEXT,
                ntfy_feedback_signing_key TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS available_chats (
                chat_id INTEGER PRIMARY KEY,
                chat_name TEXT NOT NULL,
                chat_type TEXT NOT NULL CHECK (chat_type IN ('group', 'channel')),
                username TEXT,
                is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_available_chats_current
                ON available_chats(is_current, chat_type, chat_name);

            CREATE TABLE IF NOT EXISTS information_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                settings_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                poll_interval_minutes INTEGER NOT NULL DEFAULT 15
                    CHECK (poll_interval_minutes BETWEEN 5 AND 1440),
                generation INTEGER NOT NULL DEFAULT 1,
                initialized INTEGER NOT NULL DEFAULT 0 CHECK (initialized IN (0, 1)),
                etag TEXT,
                last_modified TEXT,
                cursor_value TEXT,
                poll_state TEXT NOT NULL DEFAULT 'idle'
                    CHECK (poll_state IN ('idle', 'processing', 'error', 'disabled')),
                next_poll_at TEXT,
                lease_until TEXT,
                last_polled_at TEXT,
                last_success_at TEXT,
                last_item_at TEXT,
                last_http_status INTEGER,
                last_error_category TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(kind, url)
            );
            CREATE INDEX IF NOT EXISTS idx_information_sources_due
                ON information_sources(enabled, next_poll_at, poll_state);

            CREATE TABLE IF NOT EXISTS information_source_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES information_sources(id) ON DELETE CASCADE,
                generation INTEGER NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT,
                url TEXT,
                published_at TEXT,
                state TEXT NOT NULL CHECK (state IN ('baseline', 'pending', 'queued')),
                message_row_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_id, generation, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_information_source_items_source
                ON information_source_items(source_id, generation, id);

            CREATE TABLE IF NOT EXISTS source_provider_credentials (
                provider TEXT PRIMARY KEY,
                secret_value TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS information_source_credentials (
                source_id INTEGER PRIMARY KEY
                    REFERENCES information_sources(id) ON DELETE CASCADE,
                secret_value TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS analysis_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_row_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('live', 'manual')),
                generation INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('queued', 'processing', 'retry', 'succeeded', 'failed')
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                available_at TEXT NOT NULL,
                lease_until TEXT,
                error_category TEXT,
                error_stage TEXT,
                result_status TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(message_row_id, generation)
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_jobs_ready
                ON analysis_jobs(state, available_at, chat_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_jobs_one_active_message
                ON analysis_jobs(message_row_id)
                WHERE state IN ('queued', 'processing', 'retry');

            CREATE TABLE IF NOT EXISTS analysis_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('live', 'manual')),
                state TEXT NOT NULL CHECK (
                    state IN ('processing', 'succeeded', 'partial', 'failed', 'interrupted')
                ),
                item_count INTEGER NOT NULL,
                input_chars INTEGER NOT NULL,
                call_count INTEGER NOT NULL DEFAULT 0,
                split_count INTEGER NOT NULL DEFAULT 0,
                classified_count INTEGER NOT NULL DEFAULT 0,
                terminal_count INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                model TEXT,
                reasoning_effort TEXT,
                error_category TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_batches_created
                ON analysis_batches(created_at, state);

            CREATE TABLE IF NOT EXISTS analysis_batch_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL
                    REFERENCES analysis_batches(id) ON DELETE CASCADE,
                parent_call_id INTEGER
                    REFERENCES analysis_batch_calls(id) ON DELETE SET NULL,
                item_count INTEGER NOT NULL,
                input_chars INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('success', 'partial', 'error')),
                model TEXT NOT NULL,
                reasoning_effort TEXT NOT NULL,
                response_text TEXT,
                protocol_errors_json TEXT NOT NULL DEFAULT '[]',
                error_category TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                latency_ms INTEGER,
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_batch_calls_batch
                ON analysis_batch_calls(batch_id, id);

            CREATE TABLE IF NOT EXISTS analysis_batch_items (
                batch_id INTEGER NOT NULL
                    REFERENCES analysis_batches(id) ON DELETE CASCADE,
                job_id INTEGER NOT NULL REFERENCES analysis_jobs(id) ON DELETE CASCADE,
                message_row_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                item_order INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('claimed', 'local_terminal', 'classified', 'succeeded', 'retry', 'failed')
                ),
                classification_call_id INTEGER
                    REFERENCES analysis_batch_calls(id) ON DELETE SET NULL,
                classification_json TEXT,
                error_category TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(batch_id, job_id)
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_batch_items_job
                ON analysis_batch_items(job_id, state, batch_id DESC);

            CREATE TABLE IF NOT EXISTS analysis_chat_schedule (
                chat_id INTEGER PRIMARY KEY,
                last_dispatched_at TEXT NOT NULL,
                dispatch_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS semantic_dedupe_lock (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                owner_token TEXT,
                lease_until TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_row_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                channel TEXT NOT NULL CHECK (channel IN ('telegram', 'ntfy')),
                delivery_type TEXT NOT NULL CHECK (delivery_type IN ('immediate', 'digest')),
                batch_key TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL CHECK (
                    state IN ('queued', 'processing', 'retry', 'succeeded', 'failed')
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                available_at TEXT NOT NULL,
                lease_until TEXT,
                error_category TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT,
                UNIQUE(message_row_id, channel, delivery_type)
            );
            CREATE INDEX IF NOT EXISTS idx_deliveries_ready
                ON deliveries(state, available_at, delivery_type, channel, batch_key);

            CREATE TABLE IF NOT EXISTS ntfy_feedback_targets (
                message_row_id INTEGER PRIMARY KEY
                    REFERENCES messages(id) ON DELETE CASCADE,
                public_id TEXT NOT NULL UNIQUE,
                expires_at INTEGER NOT NULL,
                vote TEXT CHECK (vote IN ('up', 'down')),
                last_event_time INTEGER,
                voted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ntfy_feedback_events (
                event_id TEXT PRIMARY KEY,
                message_row_id INTEGER NOT NULL
                    REFERENCES messages(id) ON DELETE CASCADE,
                vote TEXT NOT NULL CHECK (vote IN ('up', 'down')),
                event_time INTEGER NOT NULL,
                received_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ntfy_feedback_runtime (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cursor_value TEXT,
                last_received_at TEXT,
                last_error_category TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedback_records (
                public_id TEXT PRIMARY KEY,
                message_row_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                vote TEXT NOT NULL CHECK (vote IN ('up', 'down')),
                voted_at TEXT NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 1,
                title TEXT NOT NULL,
                source_name TEXT NOT NULL,
                content_kind TEXT NOT NULL,
                source_key TEXT NOT NULL,
                interest_tags_json TEXT NOT NULL DEFAULT '[]',
                ai_score INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_records_voted
                ON feedback_records(voted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_feedback_records_profile
                ON feedback_records(content_kind, source_key, voted_at DESC);
            """
        )
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            runtime_columns = {
                str(row["name"])
                for row in self.connection.execute("PRAGMA table_info(runtime_config)")
            }
            runtime_migrations = (
                ("watch_chat_ids_json", "TEXT"),
                ("model_enabled", "INTEGER NOT NULL DEFAULT 0"),
                (
                    "model_base_url",
                    "TEXT NOT NULL DEFAULT 'https://model.example.com/v1'",
                ),
                ("model_api_key", "TEXT"),
                (
                    "model_classification_model",
                    "TEXT NOT NULL DEFAULT 'gemini-3.5-flash-extra-low'",
                ),
                (
                    "model_classification_reasoning_effort",
                    "TEXT NOT NULL DEFAULT 'low'",
                ),
                ("model_model", "TEXT"),
                ("model_reasoning_effort", "TEXT NOT NULL DEFAULT 'default'"),
                ("model_semantic_dedupe_model", "TEXT"),
                (
                    "model_semantic_dedupe_reasoning_effort",
                    "TEXT NOT NULL DEFAULT 'low'",
                ),
                ("model_notification_model", "TEXT"),
                (
                    "model_notification_reasoning_effort",
                    "TEXT NOT NULL DEFAULT 'low'",
                ),
                ("community_insights_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("benefit_deals_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("model_updated_at", "TEXT"),
            )
            for name, definition in runtime_migrations:
                if name not in runtime_columns:
                    self.connection.execute(
                        f"ALTER TABLE runtime_config ADD COLUMN {name} {definition}"
                    )

            push_columns = {
                str(row["name"])
                for row in self.connection.execute("PRAGMA table_info(push_config)")
            }
            for name, definition in (
                ("ntfy_community_topic", "TEXT"),
                ("ntfy_benefit_topic", "TEXT"),
                ("ntfy_feedback_topic", "TEXT"),
                ("ntfy_feedback_signing_key", "TEXT"),
            ):
                if name not in push_columns:
                    self.connection.execute(
                        f"ALTER TABLE push_config ADD COLUMN {name} {definition}"
                    )
            push_row = self.connection.execute(
                "SELECT ntfy_feedback_topic, ntfy_feedback_signing_key "
                "FROM push_config WHERE id = 1"
            ).fetchone()
            if push_row is not None and (
                not push_row["ntfy_feedback_topic"]
                or not push_row["ntfy_feedback_signing_key"]
            ):
                self.connection.execute(
                    """
                    UPDATE push_config
                    SET ntfy_feedback_topic = COALESCE(NULLIF(ntfy_feedback_topic, ''), ?),
                        ntfy_feedback_signing_key = COALESCE(NULLIF(ntfy_feedback_signing_key, ''), ?)
                    WHERE id = 1
                    """,
                    (
                        f"feedback-{secrets.token_urlsafe(18)}",
                        secrets.token_hex(32),
                    ),
                )
            self.connection.execute(
                """
                UPDATE push_config
                SET ntfy_community_topic = COALESCE(
                        NULLIF(ntfy_community_topic, ''), ntfy_topic
                    ),
                    ntfy_benefit_topic = COALESCE(
                        NULLIF(ntfy_benefit_topic, ''), ntfy_topic
                    )
                WHERE id = 1
                """
            )

            message_columns = {
                str(row["name"])
                for row in self.connection.execute("PRAGMA table_info(messages)")
            }
            message_migrations = (
                ("local_score", "INTEGER"),
                ("ai_status", "TEXT NOT NULL DEFAULT 'not_analyzed'"),
                ("ai_model", "TEXT"),
                ("ai_score", "INTEGER"),
                ("ai_summary", "TEXT"),
                ("ai_reason", "TEXT"),
                ("ai_response_text", "TEXT"),
                ("ai_started_at", "TEXT"),
                ("ai_completed_at", "TEXT"),
                ("ai_error_category", "TEXT"),
                ("ai_error_stage", "TEXT"),
                ("ai_category", "TEXT"),
                ("ai_category_confidence", "INTEGER"),
                ("ai_category_summary", "TEXT"),
                ("ai_category_reason", "TEXT"),
                ("ai_category_response_text", "TEXT"),
                ("ai_classification_model", "TEXT"),
                ("ai_classification_effort", "TEXT"),
                ("ai_classification_batch_id", "INTEGER"),
                ("ai_classification_call_id", "INTEGER"),
                ("ai_scoring_effort", "TEXT"),
                ("content_kind", "TEXT"),
                ("community_status", "TEXT NOT NULL DEFAULT 'not_analyzed'"),
                ("community_signal_type", "TEXT"),
                ("community_confidence", "INTEGER"),
                ("community_title", "TEXT"),
                ("community_summary", "TEXT"),
                ("community_reason", "TEXT"),
                ("community_response_text", "TEXT"),
                ("community_model", "TEXT"),
                ("community_effort", "TEXT"),
                ("community_evidence_count", "INTEGER"),
                ("community_checked_at", "TEXT"),
                ("community_error_category", "TEXT"),
                ("benefit_status", "TEXT NOT NULL DEFAULT 'not_analyzed'"),
                ("benefit_type", "TEXT"),
                ("benefit_confidence", "INTEGER"),
                ("benefit_title", "TEXT"),
                ("benefit_summary", "TEXT"),
                ("benefit_reason", "TEXT"),
                ("benefit_response_text", "TEXT"),
                ("benefit_model", "TEXT"),
                ("benefit_effort", "TEXT"),
                ("benefit_checked_at", "TEXT"),
                ("benefit_error_category", "TEXT"),
                ("prefilter_status", "TEXT NOT NULL DEFAULT 'not_evaluated'"),
                ("prefilter_reason_code", "TEXT"),
                ("prefilter_reason", "TEXT"),
                ("push_eligible", "INTEGER NOT NULL DEFAULT 0"),
                (
                    "push_gate_reason",
                    "TEXT NOT NULL DEFAULT 'historical_unreviewed'",
                ),
                ("push_ready_at", "TEXT"),
                (
                    "semantic_dedupe_status",
                    "TEXT NOT NULL DEFAULT 'historical_unreviewed'",
                ),
                ("semantic_dedupe_model", "TEXT"),
                ("semantic_dedupe_effort", "TEXT"),
                ("semantic_dedupe_confidence", "INTEGER"),
                ("semantic_dedupe_reason", "TEXT"),
                ("semantic_dedupe_response_text", "TEXT"),
                ("semantic_dedupe_matched_message_id", "INTEGER"),
                ("semantic_dedupe_material_update", "INTEGER NOT NULL DEFAULT 0"),
                ("semantic_dedupe_update_type", "TEXT"),
                (
                    "semantic_dedupe_update_validated",
                    "INTEGER NOT NULL DEFAULT 0",
                ),
                ("semantic_dedupe_update_rejection_reason", "TEXT"),
                ("semantic_dedupe_checked_at", "TEXT"),
                ("semantic_dedupe_error_category", "TEXT"),
                ("semantic_dedupe_candidate_count", "INTEGER NOT NULL DEFAULT 0"),
                (
                    "notification_prepare_status",
                    "TEXT NOT NULL DEFAULT 'historical_unprepared'",
                ),
                ("notification_prepare_model", "TEXT"),
                ("notification_prepare_effort", "TEXT"),
                ("notification_title", "TEXT"),
                ("notification_body", "TEXT"),
                ("notification_prepare_response_text", "TEXT"),
                ("notification_prepare_checked_at", "TEXT"),
                ("notification_prepare_error_category", "TEXT"),
                ("analysis_queue_requested", "INTEGER NOT NULL DEFAULT 0"),
                ("analysis_queue_state", "TEXT"),
                ("analysis_queue_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("analysis_queue_available_at", "TEXT"),
                ("analysis_queue_error_category", "TEXT"),
                ("analysis_queue_manual", "INTEGER NOT NULL DEFAULT 0"),
                ("is_service_message", "INTEGER NOT NULL DEFAULT 0"),
                ("source_type", "TEXT NOT NULL DEFAULT 'telegram'"),
                ("source_id", "INTEGER"),
                ("source_external_id", "TEXT"),
                ("feedback_context_sample_count", "INTEGER NOT NULL DEFAULT 0"),
                ("feedback_context_summary", "TEXT"),
                ("feedback_context_json", "TEXT"),
                ("feedback_context_applied_at", "TEXT"),
            )
            for name, definition in message_migrations:
                if name not in message_columns:
                    self.connection.execute(
                        f"ALTER TABLE messages ADD COLUMN {name} {definition}"
                    )
            schedule_columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(analysis_chat_schedule)"
                )
            }
            if "dispatch_order" not in schedule_columns:
                self.connection.execute(
                    "ALTER TABLE analysis_chat_schedule "
                    "ADD COLUMN dispatch_order INTEGER NOT NULL DEFAULT 0"
                )
            for statement in (
                """CREATE TABLE IF NOT EXISTS analysis_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('live', 'manual')),
                    state TEXT NOT NULL CHECK (
                        state IN ('processing', 'succeeded', 'partial', 'failed', 'interrupted')
                    ),
                    item_count INTEGER NOT NULL,
                    input_chars INTEGER NOT NULL,
                    call_count INTEGER NOT NULL DEFAULT 0,
                    split_count INTEGER NOT NULL DEFAULT 0,
                    classified_count INTEGER NOT NULL DEFAULT 0,
                    terminal_count INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    model TEXT,
                    reasoning_effort TEXT,
                    error_category TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )""",
                """CREATE INDEX IF NOT EXISTS idx_analysis_batches_created
                    ON analysis_batches(created_at, state)""",
                """CREATE TABLE IF NOT EXISTS analysis_batch_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL
                        REFERENCES analysis_batches(id) ON DELETE CASCADE,
                    parent_call_id INTEGER
                        REFERENCES analysis_batch_calls(id) ON DELETE SET NULL,
                    item_count INTEGER NOT NULL,
                    input_chars INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('success', 'partial', 'error')),
                    model TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    response_text TEXT,
                    protocol_errors_json TEXT NOT NULL DEFAULT '[]',
                    error_category TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    latency_ms INTEGER,
                    created_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                )""",
                """CREATE INDEX IF NOT EXISTS idx_analysis_batch_calls_batch
                    ON analysis_batch_calls(batch_id, id)""",
                """CREATE TABLE IF NOT EXISTS analysis_batch_items (
                    batch_id INTEGER NOT NULL
                        REFERENCES analysis_batches(id) ON DELETE CASCADE,
                    job_id INTEGER NOT NULL REFERENCES analysis_jobs(id) ON DELETE CASCADE,
                    message_row_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    item_order INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('claimed', 'local_terminal', 'classified', 'succeeded', 'retry', 'failed')
                    ),
                    classification_call_id INTEGER
                        REFERENCES analysis_batch_calls(id) ON DELETE SET NULL,
                    classification_json TEXT,
                    error_category TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, job_id)
                )""",
                """CREATE INDEX IF NOT EXISTS idx_analysis_batch_items_job
                    ON analysis_batch_items(job_id, state, batch_id DESC)""",
            ):
                self.connection.execute(statement)
            self.connection.execute("UPDATE messages SET local_score = CASE WHEN (local_score IS NULL) THEN base_score ELSE local_score END, push_eligible = CASE WHEN (push_eligible IS NULL) THEN 0 ELSE push_eligible END, analysis_queue_requested = CASE WHEN (analysis_queue_requested IS NULL) THEN 0 ELSE analysis_queue_requested END, analysis_queue_attempts = CASE WHEN (analysis_queue_attempts IS NULL) THEN 0 ELSE analysis_queue_attempts END, analysis_queue_manual = CASE WHEN (analysis_queue_manual IS NULL) THEN 0 ELSE analysis_queue_manual END, source_type = CASE WHEN (source_type IS NULL OR source_type = '') THEN 'telegram' ELSE source_type END, feedback_context_sample_count = CASE WHEN (feedback_context_sample_count IS NULL) THEN 0 ELSE feedback_context_sample_count END, content_kind = CASE WHEN (content_kind IS NULL AND ai_category = 'external_information') THEN 'news' ELSE content_kind END, community_status = CASE WHEN (community_status IS NULL OR community_status = '') THEN 'not_analyzed' ELSE community_status END, benefit_status = CASE WHEN (benefit_status IS NULL OR benefit_status = '') THEN 'not_analyzed' ELSE benefit_status END, semantic_dedupe_matched_message_id = CASE WHEN (semantic_dedupe_status = 'representative_replaced' AND semantic_dedupe_matched_message_id IS NOT NULL) THEN NULL ELSE semantic_dedupe_matched_message_id END, prefilter_status = CASE WHEN (prefilter_status IS NULL OR prefilter_status = '') THEN 'not_evaluated' ELSE prefilter_status END, push_gate_reason = CASE WHEN (push_gate_reason IS NULL OR push_gate_reason = '') THEN 'historical_unreviewed' ELSE push_gate_reason END, notification_prepare_status = CASE WHEN (notification_prepare_status IS NULL OR notification_prepare_status = '') THEN 'historical_unprepared' ELSE notification_prepare_status END WHERE (local_score IS NULL) OR (push_eligible IS NULL) OR (analysis_queue_requested IS NULL) OR (analysis_queue_attempts IS NULL) OR (analysis_queue_manual IS NULL) OR (source_type IS NULL OR source_type = '') OR (feedback_context_sample_count IS NULL) OR (content_kind IS NULL AND ai_category = 'external_information') OR (community_status IS NULL OR community_status = '') OR (benefit_status IS NULL OR benefit_status = '') OR (semantic_dedupe_status = 'representative_replaced' AND semantic_dedupe_matched_message_id IS NOT NULL) OR (prefilter_status IS NULL OR prefilter_status = '') OR (push_gate_reason IS NULL OR push_gate_reason = '') OR (notification_prepare_status IS NULL OR notification_prepare_status = '')")
            self.connection.execute(
                "UPDATE runtime_config SET immediate_score = ? "
                "WHERE immediate_score != ?",
                (PUSH_SCORE_THRESHOLD, PUSH_SCORE_THRESHOLD),
            )
            source_columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(information_sources)"
                )
            }
            if "settings_json" not in source_columns:
                self.connection.execute(
                    "ALTER TABLE information_sources "
                    "ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}'"
                )
            self.connection.execute(
                """
                UPDATE messages
                SET push_eligible = 0, push_ready_at = NULL
                WHERE push_eligible = 1
                  AND (
                       semantic_dedupe_status = 'historical_unreviewed'
                    OR notification_prepare_status = 'historical_unprepared'
                  )
                """
            )
            self.connection.execute(
                """
                UPDATE messages
                SET ai_model = NULL, ai_scoring_effort = NULL,
                    community_model = NULL, community_effort = NULL
                WHERE community_status = 'filtered'
                  AND community_response_text IS NULL
                  AND community_reason = ?
                """,
                (LOCAL_COMMUNITY_GATE_REASON,),
            )
            self.connection.execute(
                """
                UPDATE messages
                SET ai_model = NULL, ai_scoring_effort = NULL,
                    benefit_model = NULL, benefit_effort = NULL
                WHERE benefit_status = 'filtered'
                  AND benefit_response_text IS NULL
                  AND benefit_reason = ?
                """,
                (LOCAL_BENEFIT_GATE_REASON,),
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_push_eligibility
                ON messages(
                    push_eligible, immediate_pushed_at, digest_considered_at,
                    score, created_at
                )
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_semantic_dedupe
                ON messages(push_ready_at, semantic_dedupe_status, ai_score)
                """
            )
            self.connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external_source
                ON messages(source_type, source_id, source_external_id)
                WHERE source_id IS NOT NULL AND source_external_id IS NOT NULL
                """
            )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO semantic_dedupe_lock(
                    id, owner_token, lease_until, updated_at
                ) VALUES(1, NULL, NULL, ?)
                """,
                (to_iso(utc_now()),),
            )
            self.connection.execute(
                """
                UPDATE runtime_config
                SET model_reasoning_effort = 'default'
                WHERE model_reasoning_effort IS NULL
                   OR model_reasoning_effort NOT IN ('default', 'low', 'medium', 'high')
                """
            )
            self.connection.execute(
                """
                UPDATE runtime_config
                SET model_classification_model = ?
                WHERE model_classification_model IS NULL
                   OR trim(model_classification_model) = ''
                """,
                (DEFAULT_CLASSIFICATION_MODEL,),
            )
            self.connection.execute(
                """
                UPDATE runtime_config
                SET model_classification_reasoning_effort = ?
                WHERE model_classification_reasoning_effort IS NULL
                   OR model_classification_reasoning_effort NOT IN (
                       'default', 'low', 'medium', 'high'
                   )
                """,
                (DEFAULT_CLASSIFICATION_REASONING_EFFORT,),
            )
            self.connection.execute(
                """
                UPDATE runtime_config
                SET model_semantic_dedupe_model = COALESCE(
                    NULLIF(trim(model_semantic_dedupe_model), ''),
                    NULLIF(trim(model_classification_model), ''),
                    ?
                )
                """,
                (DEFAULT_SEMANTIC_DEDUPE_MODEL,),
            )
            self.connection.execute(
                """
                UPDATE runtime_config
                SET model_semantic_dedupe_reasoning_effort = ?
                WHERE model_semantic_dedupe_reasoning_effort IS NULL
                   OR model_semantic_dedupe_reasoning_effort NOT IN (
                       'default', 'low', 'medium', 'high'
                   )
                """,
                (DEFAULT_SEMANTIC_DEDUPE_REASONING_EFFORT,),
            )
            self.connection.execute(
                """
                UPDATE runtime_config
                SET model_notification_model = COALESCE(
                    NULLIF(trim(model_notification_model), ''),
                    NULLIF(trim(model_model), ''),
                    NULLIF(trim(model_classification_model), ''),
                    ?
                )
                """,
                (DEFAULT_NOTIFICATION_MODEL,),
            )
            self.connection.execute(
                """
                UPDATE runtime_config
                SET model_notification_reasoning_effort = ?
                WHERE model_notification_reasoning_effort IS NULL
                   OR model_notification_reasoning_effort NOT IN (
                       'default', 'low', 'medium', 'high'
                   )
                """,
                (DEFAULT_NOTIFICATION_REASONING_EFFORT,),
            )
            existing_feedback = self.connection.execute(
                """
                SELECT target.public_id, target.message_row_id, target.vote,
                       target.voted_at, message.chat_name, message.content_kind,
                       message.ai_category, message.source_type, message.source_id,
                       message.chat_id, message.notification_title,
                       message.ai_summary, message.text, message.ai_score
                FROM ntfy_feedback_targets AS target
                JOIN messages AS message ON message.id = target.message_row_id
                WHERE target.vote IN ('up', 'down')
                """
            ).fetchall()
            runtime = self.get_runtime_config()
            keywords = tuple(runtime.get("important_keywords") or ()) if runtime else ()
            for feedback in existing_feedback:
                combined_text = " ".join(
                    str(feedback[name] or "")
                    for name in ("notification_title", "ai_summary", "text")
                )
                content_kind = str(
                    feedback["content_kind"]
                    or (
                        "news"
                        if feedback["ai_category"] == "external_information"
                        else "unknown"
                    )
                )
                voted_at = str(feedback["voted_at"] or to_iso(utc_now()))
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO feedback_records(
                        public_id, message_row_id, vote, voted_at, event_count,
                        title, source_name, content_kind, source_key,
                        interest_tags_json, ai_score, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(feedback["public_id"]),
                        int(feedback["message_row_id"]),
                        str(feedback["vote"]),
                        voted_at,
                        str(
                            feedback["notification_title"]
                            or feedback["ai_summary"]
                            or "已推送资讯"
                        )[:160],
                        str(feedback["chat_name"] or "未知来源")[:160],
                        content_kind[:40],
                        feedback_source_key(
                            source_type=feedback["source_type"],
                            source_id=feedback["source_id"],
                            chat_id=feedback["chat_id"],
                        ),
                        json.dumps(
                            feedback_interest_tags(combined_text, keywords),
                            ensure_ascii=False,
                        ),
                        feedback["ai_score"],
                        voted_at,
                        voted_at,
                    ),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def initialize_digest_clock(self, now: datetime) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('last_digest_at', ?)",
            (to_iso(now),),
        )
        self.connection.commit()

    def digest_initial_delay(self, now: datetime, *, interval_seconds: int) -> float:
        """Return only the remaining digest interval after a process restart."""
        interval = float(max(1, int(interval_seconds)))
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'last_digest_at'"
        ).fetchone()
        if row is None:
            return 0.0
        try:
            last_run = datetime.fromisoformat(str(row["value"]).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        elapsed = (current.astimezone(timezone.utc) - last_run.astimezone(timezone.utc)).total_seconds()
        if elapsed < 0:
            return interval
        return max(0.0, interval - elapsed)

    def initialize_runtime_config(
        self,
        *,
        important_keywords: tuple[str, ...],
        trusted_sender_ids: frozenset[int],
        watch_chat_ids: frozenset[int],
        immediate_score: int,
        now: datetime,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO runtime_config(
                id, important_keywords_json, trusted_sender_ids_json, watch_chat_ids_json,
                immediate_score, updated_at
            ) VALUES(1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                watch_chat_ids_json = COALESCE(
                    runtime_config.watch_chat_ids_json,
                    excluded.watch_chat_ids_json
                )
            """,
            (
                json.dumps(important_keywords, ensure_ascii=False),
                json.dumps(sorted(trusted_sender_ids)),
                json.dumps(sorted(watch_chat_ids)),
                PUSH_SCORE_THRESHOLD,
                to_iso(now),
            ),
        )
        self.connection.commit()

    def initialize_push_config(
        self,
        *,
        telegram_bot_token: str,
        telegram_chat_id: str,
        now: datetime,
    ) -> None:
        token = validate_push_secret(telegram_bot_token, label="Telegram Bot Token")
        chat_id = validate_push_chat_id(telegram_chat_id)
        self.connection.execute(
            """
            INSERT OR IGNORE INTO push_config(
                id, telegram_enabled, telegram_bot_token, telegram_chat_id,
                ntfy_enabled, ntfy_base_url, updated_at
            ) VALUES(1, ?, ?, ?, 0, ?, ?)
            """,
            (
                int(bool(token and chat_id)),
                token or None,
                chat_id or None,
                DEFAULT_NTFY_BASE_URL,
                to_iso(now),
            ),
        )
        self._ensure_ntfy_feedback_config()
        self.connection.commit()

    def _ensure_ntfy_feedback_config(self) -> None:
        row = self.connection.execute(
            "SELECT ntfy_feedback_topic, ntfy_feedback_signing_key "
            "FROM push_config WHERE id = 1"
        ).fetchone()
        if row is None:
            return
        if row["ntfy_feedback_topic"] and row["ntfy_feedback_signing_key"]:
            return
        self.connection.execute(
            """
            UPDATE push_config
            SET ntfy_feedback_topic = COALESCE(NULLIF(ntfy_feedback_topic, ''), ?),
                ntfy_feedback_signing_key = COALESCE(NULLIF(ntfy_feedback_signing_key, ''), ?)
            WHERE id = 1
            """,
            (
                f"feedback-{secrets.token_urlsafe(18)}",
                secrets.token_hex(32),
            ),
        )

    def get_push_config(self, *, include_secrets: bool = False) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM push_config WHERE id = 1"
        ).fetchone()
        if row is None:
            value: dict[str, Any] = {
                "telegram": {
                    "enabled": False,
                    "chat_id": "",
                    "bot_token_configured": False,
                },
                "ntfy": {
                    "enabled": False,
                    "base_url": DEFAULT_NTFY_BASE_URL,
                    "topic": "",
                    "community_topic": "",
                    "benefit_topic": "",
                    "access_token_configured": False,
                    "feedback": {
                        "enabled": False,
                        "topic": None,
                        "last_received_at": None,
                        "last_error_category": None,
                        "consecutive_failures": 0,
                    },
                },
                "updated_at": None,
            }
            if include_secrets:
                value["telegram"]["bot_token"] = None
                value["ntfy"]["access_token"] = None
            return value
        bot_token = row["telegram_bot_token"]
        access_token = row["ntfy_access_token"]
        feedback_runtime = self.get_ntfy_feedback_runtime()
        feedback_configured = bool(
            row["ntfy_feedback_topic"] and row["ntfy_feedback_signing_key"]
        )
        value = {
            "telegram": {
                "enabled": bool(row["telegram_enabled"]),
                "chat_id": str(row["telegram_chat_id"] or ""),
                "bot_token_configured": bool(bot_token),
            },
            "ntfy": {
                "enabled": bool(row["ntfy_enabled"]),
                "base_url": str(row["ntfy_base_url"] or DEFAULT_NTFY_BASE_URL),
                "topic": str(row["ntfy_topic"] or ""),
                "community_topic": str(
                    row["ntfy_community_topic"] or row["ntfy_topic"] or ""
                ),
                "benefit_topic": str(
                    row["ntfy_benefit_topic"] or row["ntfy_topic"] or ""
                ),
                "access_token_configured": bool(access_token),
                "feedback": {
                    "enabled": bool(row["ntfy_enabled"] and feedback_configured),
                    "topic": (
                        str(row["ntfy_feedback_topic"])
                        if row["ntfy_feedback_topic"]
                        else None
                    ),
                    "last_received_at": feedback_runtime["last_received_at"],
                    "last_error_category": feedback_runtime["last_error_category"],
                    "consecutive_failures": feedback_runtime["consecutive_failures"],
                },
            },
            "updated_at": row["updated_at"],
        }
        if include_secrets:
            value["telegram"]["bot_token"] = str(bot_token) if bot_token else None
            value["ntfy"]["access_token"] = (
                str(access_token) if access_token else None
            )
            value["ntfy"]["feedback_topic"] = (
                str(row["ntfy_feedback_topic"])
                if row["ntfy_feedback_topic"]
                else None
            )
            value["ntfy"]["feedback_signing_key"] = (
                str(row["ntfy_feedback_signing_key"])
                if row["ntfy_feedback_signing_key"]
                else None
            )
        return value

    def update_push_config(
        self,
        *,
        telegram_enabled: bool,
        telegram_bot_token: str,
        clear_telegram_bot_token: bool,
        telegram_chat_id: str,
        ntfy_enabled: bool,
        ntfy_base_url: str,
        ntfy_topic: str,
        ntfy_access_token: str,
        clear_ntfy_access_token: bool,
        now: datetime,
        ntfy_community_topic: str | None = None,
        ntfy_benefit_topic: str | None = None,
    ) -> dict[str, Any]:
        if clear_telegram_bot_token and telegram_bot_token.strip():
            raise ValueError("清除 Telegram Bot Token 时不能同时提交新 Token")
        if clear_ntfy_access_token and ntfy_access_token.strip():
            raise ValueError("清除 ntfy Access Token 时不能同时提交新 Token")
        current = self.get_push_config(include_secrets=True)
        submitted_bot_token = validate_push_secret(
            telegram_bot_token, label="Telegram Bot Token"
        )
        submitted_ntfy_token = validate_push_secret(
            ntfy_access_token, label="ntfy Access Token"
        )
        next_bot_token = (
            None
            if clear_telegram_bot_token
            else submitted_bot_token or current["telegram"].get("bot_token")
        )
        next_ntfy_token = (
            None
            if clear_ntfy_access_token
            else submitted_ntfy_token or current["ntfy"].get("access_token")
        )
        chat_id = validate_push_chat_id(
            telegram_chat_id, required=telegram_enabled
        )
        base_url = validate_ntfy_base_url(ntfy_base_url)
        topic = validate_ntfy_topic(ntfy_topic, required=ntfy_enabled)
        community_topic = validate_ntfy_topic(
            ntfy_topic if ntfy_community_topic is None else ntfy_community_topic,
            required=ntfy_enabled,
        )
        benefit_topic = validate_ntfy_topic(
            ntfy_topic if ntfy_benefit_topic is None else ntfy_benefit_topic,
            required=ntfy_enabled,
        )
        if telegram_enabled and not next_bot_token:
            raise ValueError("启用 Telegram Push Bot 前必须配置 Bot Token")

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO push_config(
                    id, telegram_enabled, telegram_bot_token, telegram_chat_id,
                    ntfy_enabled, ntfy_base_url, ntfy_topic,
                    ntfy_community_topic, ntfy_benefit_topic, ntfy_access_token,
                    updated_at
                ) VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    telegram_enabled = excluded.telegram_enabled,
                    telegram_bot_token = excluded.telegram_bot_token,
                    telegram_chat_id = excluded.telegram_chat_id,
                    ntfy_enabled = excluded.ntfy_enabled,
                    ntfy_base_url = excluded.ntfy_base_url,
                    ntfy_topic = excluded.ntfy_topic,
                    ntfy_community_topic = excluded.ntfy_community_topic,
                    ntfy_benefit_topic = excluded.ntfy_benefit_topic,
                    ntfy_access_token = excluded.ntfy_access_token,
                    updated_at = excluded.updated_at
                """,
                (
                    int(telegram_enabled),
                    next_bot_token,
                    chat_id or None,
                    int(ntfy_enabled),
                    base_url,
                    topic or None,
                    community_topic or None,
                    benefit_topic or None,
                    next_ntfy_token,
                    to_iso(now),
                ),
            )
            self._ensure_ntfy_feedback_config()
        return self.get_push_config()

    def prepare_ntfy_feedback_target(
        self,
        message_row_id: int,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        timestamp = to_iso(now)
        with self.connection:
            message = self.connection.execute(
                "SELECT id FROM messages WHERE id = ?", (int(message_row_id),)
            ).fetchone()
            if message is None:
                raise ValueError("消息不存在")
            row = self.connection.execute(
                "SELECT public_id, expires_at FROM ntfy_feedback_targets "
                "WHERE message_row_id = ?",
                (int(message_row_id),),
            ).fetchone()
            if row is None:
                public_id = secrets.token_urlsafe(18)
                expires_at = int(now.timestamp()) + NTFY_FEEDBACK_TARGET_TTL_SECONDS
                self.connection.execute(
                    """
                    INSERT INTO ntfy_feedback_targets(
                        message_row_id, public_id, expires_at, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (int(message_row_id), public_id, expires_at, timestamp, timestamp),
                )
            else:
                public_id = str(row["public_id"])
                expires_at = int(row["expires_at"])
        return {"public_id": public_id, "expires_at": expires_at}

    def get_ntfy_feedback(self, message_row_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT target.vote, target.voted_at,
                   COALESCE(record.event_count, 1) AS event_count
            FROM ntfy_feedback_targets AS target
            LEFT JOIN feedback_records AS record
              ON record.public_id = target.public_id
            WHERE target.message_row_id = ? AND target.vote IS NOT NULL
            """,
            (int(message_row_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            "vote": str(row["vote"]),
            "voted_at": row["voted_at"],
            "event_count": int(row["event_count"] or 1),
        }

    def record_ntfy_feedback(
        self,
        *,
        event_id: str,
        public_id: str,
        vote: str,
        expires_at: int,
        event_time: int,
        now: datetime,
    ) -> bool:
        if vote not in {"up", "down"}:
            return False
        if not event_id or len(event_id) > 128 or not public_id or len(public_id) > 64:
            return False
        received_at = to_iso(now)
        with self.connection:
            target = self.connection.execute(
                """
                SELECT message_row_id, expires_at, last_event_time
                FROM ntfy_feedback_targets WHERE public_id = ?
                """,
                (public_id,),
            ).fetchone()
            if (
                target is None
                or int(target["expires_at"]) != int(expires_at)
                or int(expires_at) < int(now.timestamp())
            ):
                return False
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO ntfy_feedback_events(
                    event_id, message_row_id, vote, event_time, received_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    int(target["message_row_id"]),
                    vote,
                    int(event_time),
                    received_at,
                ),
            )
            if cursor.rowcount == 0:
                return False
            last_event_time = target["last_event_time"]
            latest = last_event_time is None or int(event_time) >= int(last_event_time)
            if latest:
                self.connection.execute(
                    """
                    UPDATE ntfy_feedback_targets
                    SET vote = ?, last_event_time = ?, voted_at = ?, updated_at = ?
                    WHERE public_id = ?
                    """,
                    (vote, int(event_time), received_at, received_at, public_id),
                )
            message = self.connection.execute(
                """
                SELECT id, chat_id, chat_name, source_type, source_id,
                       content_kind, ai_category, notification_title,
                       ai_summary, text, ai_score
                FROM messages WHERE id = ?
                """,
                (int(target["message_row_id"]),),
            ).fetchone()
            if message is not None:
                if latest:
                    runtime = self.get_runtime_config()
                    keywords = (
                        tuple(runtime.get("important_keywords") or ())
                        if runtime
                        else ()
                    )
                    combined_text = " ".join(
                        str(message[name] or "")
                        for name in ("notification_title", "ai_summary", "text")
                    )
                    content_kind = str(
                        message["content_kind"]
                        or (
                            "news"
                            if message["ai_category"] == "external_information"
                            else "unknown"
                        )
                    )
                    self.connection.execute(
                        """
                        INSERT INTO feedback_records(
                            public_id, message_row_id, vote, voted_at, event_count,
                            title, source_name, content_kind, source_key,
                            interest_tags_json, ai_score, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(public_id) DO UPDATE SET
                            message_row_id = excluded.message_row_id,
                            vote = excluded.vote,
                            voted_at = excluded.voted_at,
                            event_count = feedback_records.event_count + 1,
                            title = excluded.title,
                            source_name = excluded.source_name,
                            content_kind = excluded.content_kind,
                            source_key = excluded.source_key,
                            interest_tags_json = excluded.interest_tags_json,
                            ai_score = excluded.ai_score,
                            updated_at = excluded.updated_at
                        """,
                        (
                            public_id,
                            int(message["id"]),
                            vote,
                            received_at,
                            str(
                                message["notification_title"]
                                or message["ai_summary"]
                                or "已推送资讯"
                            )[:160],
                            str(message["chat_name"] or "未知来源")[:160],
                            content_kind[:40],
                            feedback_source_key(
                                source_type=message["source_type"],
                                source_id=message["source_id"],
                                chat_id=message["chat_id"],
                            ),
                            json.dumps(
                                feedback_interest_tags(combined_text, keywords),
                                ensure_ascii=False,
                            ),
                            message["ai_score"],
                            received_at,
                            received_at,
                        ),
                    )
                else:
                    self.connection.execute(
                        """
                        UPDATE feedback_records
                        SET event_count = event_count + 1, updated_at = ?
                        WHERE public_id = ?
                        """,
                        (received_at, public_id),
                    )
        return True

    def get_ntfy_feedback_runtime(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM ntfy_feedback_runtime WHERE id = 1"
        ).fetchone()
        if row is None:
            return {
                "cursor_value": None,
                "last_received_at": None,
                "last_error_category": None,
                "consecutive_failures": 0,
            }
        return {
            "cursor_value": row["cursor_value"],
            "last_received_at": row["last_received_at"],
            "last_error_category": row["last_error_category"],
            "consecutive_failures": int(row["consecutive_failures"] or 0),
        }

    def complete_ntfy_feedback_poll(
        self,
        *,
        cursor_value: str | None,
        received: bool,
        now: datetime,
    ) -> None:
        current = self.get_ntfy_feedback_runtime()
        if (
            cursor_value == current["cursor_value"]
            and not received
            and not current["last_error_category"]
        ):
            return
        timestamp = to_iso(now)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO ntfy_feedback_runtime(
                    id, cursor_value, last_received_at, last_error_category,
                    consecutive_failures, updated_at
                ) VALUES(1, ?, ?, NULL, 0, ?)
                ON CONFLICT(id) DO UPDATE SET
                    cursor_value = COALESCE(excluded.cursor_value, cursor_value),
                    last_received_at = CASE
                        WHEN ? THEN excluded.last_received_at ELSE last_received_at END,
                    last_error_category = NULL,
                    consecutive_failures = 0,
                    updated_at = excluded.updated_at
                """,
                (cursor_value, timestamp if received else None, timestamp, int(received)),
            )

    def fail_ntfy_feedback_poll(self, *, error_category: str, now: datetime) -> None:
        timestamp = to_iso(now)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO ntfy_feedback_runtime(
                    id, last_error_category, consecutive_failures, updated_at
                ) VALUES(1, ?, 1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_error_category = excluded.last_error_category,
                    consecutive_failures = consecutive_failures + 1,
                    updated_at = excluded.updated_at
                """,
                (error_category[:80], timestamp),
            )

    def feedback_guidance(
        self,
        row_id: int,
        *,
        content_kind: str,
        now: datetime,
    ) -> FeedbackGuidance:
        message = self.connection.execute(
            """
            SELECT chat_id, source_type, source_id, text,
                   ai_category_summary, ai_summary
            FROM messages WHERE id = ?
            """,
            (int(row_id),),
        ).fetchone()
        if message is None:
            raise ValueError("消息不存在")
        runtime = self.get_runtime_config()
        keywords = tuple(runtime.get("important_keywords") or ()) if runtime else ()
        current_text = " ".join(
            str(message[name] or "")
            for name in ("text", "ai_category_summary", "ai_summary")
        )
        records = self.connection.execute(
            """
            SELECT public_id, vote, title, content_kind, source_key,
                   interest_tags_json
            FROM feedback_records
            WHERE voted_at >= ?
            ORDER BY voted_at DESC
            LIMIT ?
            """,
            (
                to_iso(now - timedelta(days=FEEDBACK_WINDOW_DAYS)),
                FEEDBACK_RECORD_SCAN_LIMIT,
            ),
        ).fetchall()
        return build_feedback_guidance(
            tuple(dict(record) for record in records),
            content_kind=content_kind,
            source_key=feedback_source_key(
                source_type=message["source_type"],
                source_id=message["source_id"],
                chat_id=message["chat_id"],
            ),
            current_tags=feedback_interest_tags(current_text, keywords),
        )

    def record_feedback_context(
        self,
        row_id: int,
        *,
        guidance: FeedbackGuidance,
        now: datetime,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE messages
                SET feedback_context_sample_count = ?,
                    feedback_context_summary = ?,
                    feedback_context_json = ?,
                    feedback_context_applied_at = ?
                WHERE id = ?
                """,
                (
                    int(guidance.sample_count),
                    guidance.summary[:240],
                    json.dumps(guidance.payload, ensure_ascii=False),
                    to_iso(now) if guidance.sample_count >= 3 else None,
                    int(row_id),
                ),
            )

    def feedback_dashboard(
        self,
        *,
        hours: int,
        now: datetime,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        cutoff = to_iso(now - timedelta(hours=max(1, hours)))
        totals = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN vote = 'up' THEN 1 ELSE 0 END) AS up,
                   SUM(CASE WHEN vote = 'down' THEN 1 ELSE 0 END) AS down,
                   COALESCE(SUM(event_count), 0) AS event_count
            FROM feedback_records WHERE voted_at >= ?
            """,
            (cutoff,),
        ).fetchone()
        by_kind = [
            {
                "content_kind": str(row["content_kind"]),
                "label": FEEDBACK_CONTENT_LABELS.get(
                    str(row["content_kind"]), "其他"
                ),
                "total": int(row["total"]),
                "up": int(row["up"] or 0),
                "down": int(row["down"] or 0),
            }
            for row in self.connection.execute(
                """
                SELECT content_kind, COUNT(*) AS total,
                       SUM(CASE WHEN vote = 'up' THEN 1 ELSE 0 END) AS up,
                       SUM(CASE WHEN vote = 'down' THEN 1 ELSE 0 END) AS down
                FROM feedback_records WHERE voted_at >= ?
                GROUP BY content_kind ORDER BY total DESC, content_kind
                """,
                (cutoff,),
            )
        ]
        rows = self.connection.execute(
            """
            SELECT public_id, message_row_id, vote, voted_at, event_count,
                   title, source_name, content_kind, ai_score
            FROM feedback_records WHERE voted_at >= ?
            ORDER BY voted_at DESC, public_id DESC
            LIMIT ? OFFSET ?
            """,
            (cutoff, max(1, min(int(limit), 100)), max(0, int(offset))),
        ).fetchall()
        total = int(totals["total"] or 0)
        up = int(totals["up"] or 0)
        down = int(totals["down"] or 0)
        return {
            "summary": {
                "total": total,
                "up": up,
                "down": down,
                "event_count": int(totals["event_count"] or 0),
                "positive_rate": round(up * 100 / total, 1) if total else None,
                "window_hours": int(hours),
                "learning_mode": "weak_signal",
                "minimum_samples": 3,
            },
            "by_content_kind": by_kind,
            "items": [
                {
                    "message_row_id": row["message_row_id"],
                    "vote": str(row["vote"]),
                    "voted_at": row["voted_at"],
                    "event_count": int(row["event_count"] or 1),
                    "title": str(row["title"]),
                    "source_name": str(row["source_name"]),
                    "content_kind": str(row["content_kind"]),
                    "content_kind_label": FEEDBACK_CONTENT_LABELS.get(
                        str(row["content_kind"]), "其他"
                    ),
                    "ai_score": row["ai_score"],
                }
                for row in rows
            ],
        }

    def get_runtime_config(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM runtime_config WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        try:
            keywords = tuple(str(value) for value in json.loads(row["important_keywords_json"]))
            trusted = frozenset(int(value) for value in json.loads(row["trusted_sender_ids_json"]))
            watched = frozenset(int(value) for value in json.loads(row["watch_chat_ids_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return {
            "important_keywords": keywords,
            "trusted_sender_ids": trusted,
            "watch_chat_ids": watched,
            "immediate_score": int(row["immediate_score"]),
            "updated_at": str(row["updated_at"]),
        }

    def update_runtime_config(
        self,
        *,
        important_keywords: tuple[str, ...],
        trusted_sender_ids: frozenset[int],
        watch_chat_ids: frozenset[int],
        immediate_score: int,
        now: datetime,
    ) -> dict[str, Any]:
        if not important_keywords:
            raise ValueError("重要关键词不能为空")
        if immediate_score != PUSH_SCORE_THRESHOLD:
            raise ValueError("实时推送阈值固定为 60 分")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO runtime_config(
                    id, important_keywords_json, trusted_sender_ids_json, watch_chat_ids_json,
                    immediate_score, updated_at
                ) VALUES(1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    important_keywords_json = excluded.important_keywords_json,
                    trusted_sender_ids_json = excluded.trusted_sender_ids_json,
                    watch_chat_ids_json = excluded.watch_chat_ids_json,
                    immediate_score = excluded.immediate_score,
                    updated_at = excluded.updated_at
                """,
                (
                    json.dumps(important_keywords, ensure_ascii=False),
                    json.dumps(sorted(trusted_sender_ids)),
                    json.dumps(sorted(watch_chat_ids)),
                    immediate_score,
                    to_iso(now),
                ),
            )
        value = self.get_runtime_config()
        if value is None:
            raise RuntimeError("运行时配置写入失败")
        return value

    def get_model_config(self, *, include_api_key: bool = False) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM runtime_config WHERE id = 1"
        ).fetchone()
        if row is None:
            value: dict[str, Any] = {
                "enabled": False,
                "base_url": DEFAULT_BASE_URL,
                "classification_model": DEFAULT_CLASSIFICATION_MODEL,
                "classification_reasoning_effort": DEFAULT_CLASSIFICATION_REASONING_EFFORT,
                "model": None,
                "reasoning_effort": DEFAULT_REASONING_EFFORT,
                "semantic_dedupe_model": DEFAULT_SEMANTIC_DEDUPE_MODEL,
                "semantic_dedupe_reasoning_effort": DEFAULT_SEMANTIC_DEDUPE_REASONING_EFFORT,
                "notification_model": DEFAULT_NOTIFICATION_MODEL,
                "notification_reasoning_effort": DEFAULT_NOTIFICATION_REASONING_EFFORT,
                "community_insights_enabled": True,
                "benefit_deals_enabled": True,
                "api_key_configured": False,
                "updated_at": None,
            }
            if include_api_key:
                value["api_key"] = None
            return value
        api_key = row["model_api_key"]
        value = {
            "enabled": bool(row["model_enabled"]),
            "base_url": str(row["model_base_url"] or DEFAULT_BASE_URL),
            "classification_model": str(
                row["model_classification_model"] or DEFAULT_CLASSIFICATION_MODEL
            ),
            "classification_reasoning_effort": validate_reasoning_effort(
                str(
                    row["model_classification_reasoning_effort"]
                    or DEFAULT_CLASSIFICATION_REASONING_EFFORT
                )
            ),
            "model": str(row["model_model"]) if row["model_model"] else None,
            "reasoning_effort": validate_reasoning_effort(
                str(row["model_reasoning_effort"] or DEFAULT_REASONING_EFFORT)
            ),
            "semantic_dedupe_model": str(
                row["model_semantic_dedupe_model"] or DEFAULT_SEMANTIC_DEDUPE_MODEL
            ),
            "semantic_dedupe_reasoning_effort": validate_reasoning_effort(
                str(
                    row["model_semantic_dedupe_reasoning_effort"]
                    or DEFAULT_SEMANTIC_DEDUPE_REASONING_EFFORT
                )
            ),
            "notification_model": str(
                row["model_notification_model"] or DEFAULT_NOTIFICATION_MODEL
            ),
            "notification_reasoning_effort": validate_reasoning_effort(
                str(
                    row["model_notification_reasoning_effort"]
                    or DEFAULT_NOTIFICATION_REASONING_EFFORT
                )
            ),
            "community_insights_enabled": bool(row["community_insights_enabled"]),
            "benefit_deals_enabled": bool(row["benefit_deals_enabled"]),
            "api_key_configured": bool(api_key),
            "updated_at": row["model_updated_at"],
        }
        if include_api_key:
            value["api_key"] = str(api_key) if api_key else None
        return value

    def update_model_config(
        self,
        *,
        enabled: bool,
        base_url: str,
        model: str | None,
        classification_model: str = DEFAULT_CLASSIFICATION_MODEL,
        api_key: str,
        clear_api_key: bool,
        now: datetime,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        classification_reasoning_effort: str = DEFAULT_CLASSIFICATION_REASONING_EFFORT,
        semantic_dedupe_model: str | None = None,
        semantic_dedupe_reasoning_effort: str | None = None,
        notification_model: str | None = None,
        notification_reasoning_effort: str | None = None,
        community_insights_enabled: bool = True,
        benefit_deals_enabled: bool = True,
    ) -> dict[str, Any]:
        reasoning_effort = validate_reasoning_effort(reasoning_effort)
        classification_reasoning_effort = validate_reasoning_effort(
            classification_reasoning_effort
        )
        current = self.get_model_config(include_api_key=True)
        next_semantic_model = semantic_dedupe_model or str(
            current.get("semantic_dedupe_model") or classification_model
        )
        next_notification_model = notification_model or str(
            current.get("notification_model") or model or classification_model
        )
        next_semantic_effort = validate_reasoning_effort(
            semantic_dedupe_reasoning_effort
            or str(
                current.get("semantic_dedupe_reasoning_effort")
                or DEFAULT_SEMANTIC_DEDUPE_REASONING_EFFORT
            )
        )
        next_notification_effort = validate_reasoning_effort(
            notification_reasoning_effort
            or str(
                current.get("notification_reasoning_effort")
                or DEFAULT_NOTIFICATION_REASONING_EFFORT
            )
        )
        if clear_api_key:
            next_key = None
        elif api_key:
            next_key = api_key
        else:
            next_key = current.get("api_key")
        if enabled and (
            not next_key
            or not model
            or not classification_model
            or not next_semantic_model
            or not next_notification_model
        ):
            raise ValueError("启用模型分析前必须配置 API Key 和四个阶段模型")
        with self.connection:
            self.connection.execute(
                """
                UPDATE runtime_config
                SET model_enabled = ?, model_base_url = ?, model_api_key = ?,
                    model_classification_model = ?,
                    model_classification_reasoning_effort = ?,
                    model_model = ?, model_reasoning_effort = ?,
                    model_semantic_dedupe_model = ?,
                    model_semantic_dedupe_reasoning_effort = ?,
                    model_notification_model = ?,
                    model_notification_reasoning_effort = ?,
                    community_insights_enabled = ?, benefit_deals_enabled = ?,
                    model_updated_at = ?
                WHERE id = 1
                """,
                (
                    int(enabled),
                    base_url,
                    next_key,
                    classification_model,
                    classification_reasoning_effort,
                    model,
                    reasoning_effort,
                    next_semantic_model,
                    next_semantic_effort,
                    next_notification_model,
                    next_notification_effort,
                    int(community_insights_enabled),
                    int(benefit_deals_enabled),
                    to_iso(now),
                ),
            )
        return self.get_model_config()

    def replace_available_chats(
        self,
        chats: Iterable[dict[str, Any]],
        *,
        now: datetime,
    ) -> int:
        rows = [
            (
                int(chat["chat_id"]),
                str(chat["chat_name"]),
                str(chat["chat_type"]),
                str(chat["username"]) if chat.get("username") else None,
                to_iso(now),
            )
            for chat in chats
        ]
        with self.connection:
            self.connection.execute("UPDATE available_chats SET is_current = 0")
            self.connection.executemany(
                """
                INSERT INTO available_chats(
                    chat_id, chat_name, chat_type, username, is_current, updated_at
                ) VALUES(?, ?, ?, ?, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_name = excluded.chat_name,
                    chat_type = excluded.chat_type,
                    username = excluded.username,
                    is_current = 1,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def current_available_chat_ids(self) -> frozenset[int]:
        rows = self.connection.execute(
            "SELECT chat_id FROM available_chats WHERE is_current = 1"
        ).fetchall()
        return frozenset(int(row["chat_id"]) for row in rows)

    def message_chat_type(self, row_id: int) -> str:
        """Return Telegram's recorded dialog type, failing closed for unknown sources."""
        row = self.connection.execute(
            """
            SELECT chats.chat_type
            FROM messages AS message
            JOIN available_chats AS chats
              ON chats.chat_id = message.chat_id AND chats.is_current = 1
            WHERE message.id = ? AND message.source_type = 'telegram'
            """,
            (row_id,),
        ).fetchone()
        return str(row["chat_type"]) if row is not None else "unknown"

    def create_information_source(
        self,
        *,
        kind: str,
        name: str,
        url: str,
        settings: dict[str, Any] | None = None,
        enabled: bool,
        poll_interval_minutes: int,
        now: datetime,
        secret_value: str = "",
    ) -> dict[str, Any]:
        if kind not in {"rss", "github_releases", "github_advisories", "cisa_kev", "nvd_cve", "vendor_status", "hacker_news", "bluesky", "mastodon", "newsletter_imap"}:
            raise ValueError("不支持的信息源类型")
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 120:
            raise ValueError("信息源名称长度无效")
        if not 5 <= poll_interval_minutes <= 1440:
            raise ValueError("采集间隔必须在 5 到 1440 分钟之间")
        if len(secret_value) > 2_048:
            raise ValueError("信息源凭据长度无效")
        timestamp = to_iso(now)
        try:
            with self.connection:
                cursor = self.connection.execute(
                    """
                    INSERT INTO information_sources(
                        kind, name, url, settings_json, enabled, poll_interval_minutes,
                        poll_state, next_poll_at, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kind,
                        clean_name,
                        url,
                        json.dumps(settings or {}, ensure_ascii=False, sort_keys=True),
                        int(enabled),
                        poll_interval_minutes,
                        "idle" if enabled else "disabled",
                        timestamp if enabled else None,
                        timestamp,
                        timestamp,
                    ),
                )
                if secret_value:
                    self.connection.execute(
                        """
                        INSERT INTO information_source_credentials(source_id, secret_value, updated_at)
                        VALUES(?, ?, ?)
                        """,
                        (int(cursor.lastrowid), secret_value, timestamp),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError("这个信息源已经存在") from exc
        value = self.get_information_source(int(cursor.lastrowid))
        assert value is not None
        return value

    def get_information_source(self, source_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM information_sources WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        value["initialized"] = bool(value["initialized"])
        try:
            value["settings"] = json.loads(str(value.get("settings_json") or "{}"))
        except (TypeError, ValueError):
            value["settings"] = {}
        value.pop("settings_json", None)
        value["chat_id"] = information_source_chat_id(source_id)
        credential = self.connection.execute(
            "SELECT secret_value FROM information_source_credentials WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        value["secret_configured"] = bool(
            credential is not None and str(credential["secret_value"] or "")
        )
        return value

    def list_information_sources(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT
                sources.*,
                COUNT(DISTINCT items.id) AS item_count,
                COUNT(DISTINCT messages.id) AS message_count,
                CASE WHEN LENGTH(COALESCE(credentials.secret_value, '')) > 0 THEN 1 ELSE 0 END
                    AS secret_configured
            FROM information_sources AS sources
            LEFT JOIN information_source_items AS items
              ON items.source_id = sources.id
             AND items.generation = sources.generation
            LEFT JOIN messages
              ON messages.source_type = sources.kind
             AND messages.source_id = sources.id
            LEFT JOIN information_source_credentials AS credentials
              ON credentials.source_id = sources.id
            GROUP BY sources.id
            ORDER BY sources.name COLLATE NOCASE, sources.id
            """
        ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["enabled"] = bool(value["enabled"])
            value["initialized"] = bool(value["initialized"])
            value["secret_configured"] = bool(value["secret_configured"])
            try:
                value["settings"] = json.loads(str(value.get("settings_json") or "{}"))
            except (TypeError, ValueError):
                value["settings"] = {}
            value.pop("settings_json", None)
            value["chat_id"] = information_source_chat_id(int(value["id"]))
            values.append(value)
        return values

    def update_information_source(
        self,
        source_id: int,
        *,
        kind: str | None = None,
        name: str,
        url: str,
        settings: dict[str, Any] | None = None,
        enabled: bool,
        poll_interval_minutes: int,
        now: datetime,
        secret_value: str = "",
        clear_secret: bool = False,
    ) -> dict[str, Any]:
        current = self.get_information_source(source_id)
        if current is None:
            raise ValueError("信息源不存在")
        next_kind = kind or str(current["kind"])
        if next_kind not in {"rss", "github_releases", "github_advisories", "cisa_kev", "nvd_cve", "vendor_status", "hacker_news", "bluesky", "mastodon", "newsletter_imap"}:
            raise ValueError("不支持的信息源类型")
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 120:
            raise ValueError("信息源名称长度无效")
        if not 5 <= poll_interval_minutes <= 1440:
            raise ValueError("采集间隔必须在 5 到 1440 分钟之间")
        if len(secret_value) > 2_048:
            raise ValueError("信息源凭据长度无效")
        next_settings = settings or {}
        changed_url = (
            str(current["kind"]) != next_kind
            or str(current["url"]) != url
            or current.get("settings", {}) != next_settings
        )
        timestamp = to_iso(now)
        old_host = urlsplit(str(current["url"])).hostname
        new_host = urlsplit(url).hostname
        credential_scope_changed = (
            str(current["kind"]) != next_kind or old_host != new_host
        )
        if next_kind == "newsletter_imap" and (
            (credential_scope_changed and not secret_value)
            or (clear_secret and not secret_value)
        ):
            raise ValueError("修改邮件服务器或清除密码时必须填写新的邮箱密码")
        try:
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE information_sources
                    SET kind = ?, name = ?, url = ?, settings_json = ?, enabled = ?, poll_interval_minutes = ?,
                        generation = generation + ?,
                        initialized = CASE WHEN ? THEN 0 ELSE initialized END,
                        etag = CASE WHEN ? THEN NULL ELSE etag END,
                        last_modified = CASE WHEN ? THEN NULL ELSE last_modified END,
                        cursor_value = CASE WHEN ? THEN NULL ELSE cursor_value END,
                        last_item_at = CASE WHEN ? THEN NULL ELSE last_item_at END,
                        poll_state = ?, next_poll_at = ?, lease_until = NULL,
                        last_error_category = NULL, consecutive_failures = 0,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_kind,
                        clean_name,
                        url,
                        json.dumps(next_settings, ensure_ascii=False, sort_keys=True),
                        int(enabled),
                        poll_interval_minutes,
                        int(changed_url),
                        int(changed_url),
                        int(changed_url),
                        int(changed_url),
                        int(changed_url),
                        int(changed_url),
                        "idle" if enabled else "disabled",
                        timestamp if enabled else None,
                        timestamp,
                        source_id,
                    ),
                )
                if clear_secret or credential_scope_changed:
                    self.connection.execute(
                        "DELETE FROM information_source_credentials WHERE source_id = ?",
                        (source_id,),
                    )
                if secret_value:
                    self.connection.execute(
                        """
                        INSERT INTO information_source_credentials(source_id, secret_value, updated_at)
                        VALUES(?, ?, ?)
                        ON CONFLICT(source_id) DO UPDATE SET
                            secret_value = excluded.secret_value,
                            updated_at = excluded.updated_at
                        """,
                        (source_id, secret_value, timestamp),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError("这个信息源已经存在") from exc
        value = self.get_information_source(source_id)
        assert value is not None
        return value

    def request_information_source_refresh(
        self, source_id: int, *, now: datetime
    ) -> dict[str, Any]:
        source = self.get_information_source(source_id)
        if source is None:
            raise ValueError("信息源不存在")
        if not source["enabled"]:
            raise ValueError("请先启用信息源")
        with self.connection:
            self.connection.execute(
                """
                UPDATE information_sources
                SET poll_state = 'idle', next_poll_at = ?, lease_until = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (to_iso(now), to_iso(now), source_id),
            )
        value = self.get_information_source(source_id)
        assert value is not None
        return value

    def recover_information_source_polls(
        self, *, now: datetime, kind: str = "rss"
    ) -> int:
        timestamp = to_iso(now)
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE information_sources
                SET poll_state = 'idle', next_poll_at = ?, lease_until = NULL,
                    last_error_category = 'interrupted', updated_at = ?
                WHERE kind = ? AND enabled = 1 AND poll_state = 'processing'
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (timestamp, timestamp, kind, timestamp),
            )
        return max(0, cursor.rowcount)

    def release_information_source_polls(
        self, *, now: datetime, kind: str = "rss"
    ) -> int:
        """Release this single service instance's in-flight feed leases on shutdown."""
        timestamp = to_iso(now)
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE information_sources
                SET poll_state = 'idle', next_poll_at = ?, lease_until = NULL,
                    last_error_category = 'interrupted', updated_at = ?
                WHERE kind = ? AND enabled = 1 AND poll_state = 'processing'
                """,
                (timestamp, timestamp, kind),
            )
        return max(0, cursor.rowcount)

    def claim_due_information_source(
        self, *, now: datetime, lease_seconds: int, kind: str = "rss"
    ) -> dict[str, Any] | None:
        timestamp = to_iso(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                UPDATE information_sources
                SET poll_state = 'idle', next_poll_at = ?, lease_until = NULL,
                    last_error_category = 'interrupted', updated_at = ?
                WHERE kind = ? AND enabled = 1 AND poll_state = 'processing'
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (timestamp, timestamp, kind, timestamp),
            )
            row = self.connection.execute(
                """
                SELECT * FROM information_sources
                WHERE kind = ? AND enabled = 1 AND poll_state IN ('idle', 'error')
                  AND (next_poll_at IS NULL OR next_poll_at <= ?)
                ORDER BY COALESCE(next_poll_at, created_at), id
                LIMIT 1
                """,
                (kind, timestamp),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            lease = to_iso(now + timedelta(seconds=max(30, lease_seconds)))
            self.connection.execute(
                """
                UPDATE information_sources
                SET poll_state = 'processing', lease_until = ?,
                    last_polled_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (lease, timestamp, timestamp, int(row["id"])),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get_information_source(int(row["id"]))

    def baseline_information_source(
        self,
        source_id: int,
        *,
        generation: int,
        entries: Iterable[Any],
        now: datetime,
        http_status: int,
        etag: str | None,
        last_modified: str | None,
        cursor_value: str | None = None,
    ) -> int:
        timestamp = to_iso(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            source = self.connection.execute(
                "SELECT * FROM information_sources WHERE id = ?", (source_id,)
            ).fetchone()
            if source is None or int(source["generation"]) != generation:
                self.connection.rollback()
                return 0
            rows = [
                (
                    source_id,
                    generation,
                    str(entry.external_id),
                    str(entry.title),
                    entry.url,
                    entry.published_at,
                    timestamp,
                    timestamp,
                )
                for entry in entries
            ]
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO information_source_items(
                    source_id, generation, external_id, title, url,
                    published_at, state, first_seen_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'baseline', ?, ?)
                """,
                rows,
            )
            newest = max((row[5] or timestamp for row in rows), default=None)
            cursor = cursor_value or (rows[-1][2] if rows else source["cursor_value"])
            self.connection.execute(
                """
                UPDATE information_sources
                SET initialized = 1, poll_state = 'idle', lease_until = NULL,
                    next_poll_at = ?, last_success_at = ?, last_http_status = ?,
                    last_error_category = NULL, consecutive_failures = 0,
                    etag = ?, last_modified = ?, cursor_value = ?,
                    last_item_at = ?, updated_at = ?
                WHERE id = ? AND generation = ?
                """,
                (
                    to_iso(now + timedelta(minutes=int(source["poll_interval_minutes"]))),
                    timestamp,
                    http_status,
                    etag,
                    last_modified,
                    cursor,
                    newest,
                    timestamp,
                    source_id,
                    generation,
                ),
            )
            self.connection.commit()
            return len(rows)
        except Exception:
            self.connection.rollback()
            raise

    def reserve_information_source_item(
        self,
        source_id: int,
        *,
        generation: int,
        external_id: str,
        title: str,
        url: str | None,
        published_at: str | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        timestamp = to_iso(now)
        with self.connection:
            source = self.connection.execute(
                "SELECT enabled, generation FROM information_sources WHERE id = ?",
                (source_id,),
            ).fetchone()
            if (
                source is None
                or not bool(source["enabled"])
                or int(source["generation"]) != generation
            ):
                return None
            self.connection.execute(
                """
                INSERT OR IGNORE INTO information_source_items(
                    source_id, generation, external_id, title, url,
                    published_at, state, first_seen_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    source_id,
                    generation,
                    external_id,
                    title,
                    url,
                    published_at,
                    timestamp,
                    timestamp,
                ),
            )
        row = self.connection.execute(
            """
            SELECT * FROM information_source_items
            WHERE source_id = ? AND generation = ? AND external_id = ?
            """,
            (source_id, generation, external_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def link_information_source_item(
        self,
        item_id: int,
        *,
        chat_id: int,
        message_id: int,
        now: datetime,
    ) -> bool:
        message = self.connection.execute(
            "SELECT id FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        if message is None:
            return False
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE information_source_items
                SET state = 'queued', message_row_id = ?, updated_at = ?
                WHERE id = ? AND state = 'pending'
                """,
                (int(message["id"]), to_iso(now), item_id),
            )
        return cursor.rowcount == 1

    def complete_information_source_poll(
        self,
        source_id: int,
        *,
        generation: int,
        now: datetime,
        http_status: int,
        etag: str | None,
        last_modified: str | None,
        new_items: int,
        cursor_value: str | None,
        last_item_at: str | None,
    ) -> bool:
        source = self.connection.execute(
            "SELECT poll_interval_minutes FROM information_sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if source is None:
            return False
        timestamp = to_iso(now)
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE information_sources
                SET poll_state = CASE WHEN enabled = 1 THEN 'idle' ELSE 'disabled' END,
                    lease_until = NULL, next_poll_at = CASE WHEN enabled = 1 THEN ? ELSE NULL END,
                    last_success_at = ?, last_http_status = ?,
                    last_error_category = NULL, consecutive_failures = 0,
                    etag = ?, last_modified = ?,
                    cursor_value = COALESCE(?, cursor_value),
                    last_item_at = COALESCE(?, last_item_at), updated_at = ?
                WHERE id = ? AND generation = ?
                """,
                (
                    to_iso(now + timedelta(minutes=int(source["poll_interval_minutes"]))),
                    timestamp,
                    http_status,
                    etag,
                    last_modified,
                    cursor_value,
                    last_item_at,
                    timestamp,
                    source_id,
                    generation,
                ),
            )
        _ = new_items
        return cursor.rowcount == 1

    def fail_information_source_poll(
        self,
        source_id: int,
        *,
        generation: int,
        now: datetime,
        error_category: str,
    ) -> bool:
        source = self.connection.execute(
            "SELECT consecutive_failures FROM information_sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if source is None:
            return False
        failures = int(source["consecutive_failures"]) + 1
        delay = min(3600, 60 * (2 ** min(6, failures - 1)))
        timestamp = to_iso(now)
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE information_sources
                SET poll_state = CASE WHEN enabled = 1 THEN 'error' ELSE 'disabled' END,
                    lease_until = NULL,
                    next_poll_at = CASE WHEN enabled = 1 THEN ? ELSE NULL END,
                    last_error_category = ?, consecutive_failures = ?, updated_at = ?
                WHERE id = ? AND generation = ?
                """,
                (
                    to_iso(now + timedelta(seconds=delay)),
                    error_category[:64],
                    failures,
                    timestamp,
                    source_id,
                    generation,
                ),
            )
        return cursor.rowcount == 1

    def get_information_source_credential(
        self, source_id: int, *, include_secret: bool = False
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT secret_value, updated_at FROM information_source_credentials WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        secret = str(row["secret_value"] or "") if row is not None else ""
        value: dict[str, Any] = {
            "source_id": source_id,
            "configured": bool(secret),
            "updated_at": row["updated_at"] if row is not None else None,
        }
        if include_secret:
            value["secret_value"] = secret
        return value

    def get_source_provider_credential(
        self, provider: str, *, include_secret: bool = False
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT secret_value, updated_at FROM source_provider_credentials WHERE provider = ?",
            (provider,),
        ).fetchone()
        secret = str(row["secret_value"] or "") if row is not None else ""
        value: dict[str, Any] = {
            "provider": provider,
            "configured": bool(secret),
            "updated_at": row["updated_at"] if row is not None else None,
        }
        if include_secret:
            value["secret_value"] = secret
        return value

    def update_source_provider_credential(
        self,
        provider: str,
        *,
        secret_value: str,
        clear_secret: bool,
        now: datetime,
    ) -> dict[str, Any]:
        if provider not in {"github", "nvd"}:
            raise ValueError("不支持的凭据类型")
        current = self.get_source_provider_credential(provider, include_secret=True)
        next_secret = "" if clear_secret else (secret_value.strip() or current["secret_value"])
        if len(next_secret) > 2_048:
            raise ValueError("凭据长度无效")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO source_provider_credentials(provider, secret_value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    secret_value = excluded.secret_value,
                    updated_at = excluded.updated_at
                """,
                (provider, next_secret or None, to_iso(now)),
            )
        return self.get_source_provider_credential(provider)

    def set_listener_heartbeat(
        self,
        *,
        connected: bool,
        self_id: int,
        watch_count: int,
        now: datetime,
    ) -> None:
        value = json.dumps(
            {
                "connected": connected,
                "self_id": self_id,
                "watch_count": watch_count,
                "updated_at": to_iso(now),
            },
            ensure_ascii=False,
        )
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES('listener_heartbeat', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (value,),
        )
        self.connection.commit()

    def get_listener_heartbeat(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'listener_heartbeat'"
        ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def record_queue_metric_sample(self, *, now: datetime) -> dict[str, Any]:
        """Persist one bounded queue snapshot for server-side chart history."""
        analysis_states = {
            str(row["state"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT state, COUNT(*) AS count FROM analysis_jobs GROUP BY state"
            )
        }
        delivery_states = {
            str(row["state"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT state, COUNT(*) AS count FROM deliveries GROUP BY state"
            )
        }
        sampled_at = to_iso(now)
        values = {
            "sampled_at": sampled_at,
            "analysis_pending": analysis_states.get("queued", 0),
            "analysis_processing": analysis_states.get("processing", 0),
            "analysis_retry": analysis_states.get("retry", 0),
            "analysis_failed": analysis_states.get("failed", 0),
            "delivery_pending": delivery_states.get("queued", 0),
            "delivery_processing": delivery_states.get("processing", 0),
            "delivery_retry": delivery_states.get("retry", 0),
            "delivery_failed": delivery_states.get("failed", 0),
        }
        cutoff = to_iso(now - timedelta(days=QUEUE_METRIC_RETENTION_DAYS))
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO queue_metric_samples(
                    sampled_at,
                    analysis_pending, analysis_processing,
                    analysis_retry, analysis_failed,
                    delivery_pending, delivery_processing,
                    delivery_retry, delivery_failed
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sampled_at) DO UPDATE SET
                    analysis_pending = excluded.analysis_pending,
                    analysis_processing = excluded.analysis_processing,
                    analysis_retry = excluded.analysis_retry,
                    analysis_failed = excluded.analysis_failed,
                    delivery_pending = excluded.delivery_pending,
                    delivery_processing = excluded.delivery_processing,
                    delivery_retry = excluded.delivery_retry,
                    delivery_failed = excluded.delivery_failed
                """,
                tuple(values.values()),
            )
            self.connection.execute(
                "DELETE FROM queue_metric_samples WHERE sampled_at < ?",
                (cutoff,),
            )
        return values

    def queue_metric_history(
        self,
        *,
        minutes: int,
        now: datetime,
    ) -> list[dict[str, Any]]:
        cutoff = to_iso(now - timedelta(minutes=max(1, minutes)))
        rows = self.connection.execute(
            """
            SELECT sampled_at,
                   analysis_pending, analysis_processing,
                   analysis_retry, analysis_failed,
                   delivery_pending, delivery_processing,
                   delivery_retry, delivery_failed
            FROM queue_metric_samples
            WHERE sampled_at >= ?
            ORDER BY sampled_at ASC
            LIMIT 21601
            """,
            (cutoff,),
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.connection.close()

    def insert_message(
        self,
        record: MessageRecord,
        *,
        enqueue_analysis: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Persist a message and, for live traffic, its analysis job atomically."""
        queued_at = to_iso(now or utc_now())
        prequeue_result = (
            evaluate_prequeue_prefilter(
                record.text,
                is_service_message=record.is_service_message,
            )
            if enqueue_analysis
            else PrefilterResult(filtered=False)
        )
        should_enqueue = bool(enqueue_analysis and not prequeue_result.filtered)
        if prequeue_result.filtered and (
            not prequeue_result.reason_code or not prequeue_result.reason
        ):
            raise ValueError("过滤结果缺少稳定原因")
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO messages(
                    chat_id, message_id, chat_name, chat_username, sender_id,
                    sender_name, sent_at, text, reply_to_message_id, thread_root_id,
                    base_score, local_score, reply_bonus, score, reasons_json, reply_count, link,
                    normalized_text, primary_url, is_service_message,
                    source_type, source_id, source_external_id, created_at,
                    prefilter_status, prefilter_reason_code, prefilter_reason,
                    ai_status, ai_completed_at, push_gate_reason,
                    semantic_dedupe_status, notification_prepare_status,
                    analysis_queue_requested, analysis_queue_state,
                    analysis_queue_available_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    record.chat_id,
                    record.message_id,
                    record.chat_name,
                    record.chat_username,
                    record.sender_id,
                    record.sender_name,
                    record.sent_at,
                    record.text,
                    record.reply_to_message_id,
                    record.thread_root_id,
                    record.base_score,
                    record.base_score,
                    record.base_score,
                    json.dumps(record.reasons, ensure_ascii=False),
                    record.link,
                    record.normalized_text,
                    record.primary_url,
                    int(record.is_service_message),
                    record.source_type,
                    record.source_id,
                    record.source_external_id,
                    record.created_at,
                    "filtered" if prequeue_result.filtered else "not_evaluated",
                    prequeue_result.reason_code,
                    prequeue_result.reason,
                    "prefiltered"
                    if prequeue_result.filtered
                    else ("queued" if should_enqueue else "not_analyzed"),
                    queued_at if prequeue_result.filtered else None,
                    "prefiltered"
                    if prequeue_result.filtered
                    else ("analysis_queued" if should_enqueue else "historical_unreviewed"),
                    "not_required_prefilter"
                    if prequeue_result.filtered
                    else ("awaiting_analysis" if should_enqueue else "historical_unreviewed"),
                    "not_required_prefilter"
                    if prequeue_result.filtered
                    else ("awaiting_analysis" if should_enqueue else "historical_unprepared"),
                    int(should_enqueue),
                    "succeeded"
                    if prequeue_result.filtered
                    else ("queued" if should_enqueue else None),
                    queued_at if should_enqueue else None,
                ),
            )
            if cursor.rowcount == 1 and should_enqueue:
                row_id = int(cursor.lastrowid)
                self.connection.execute(
                    """
                    INSERT INTO analysis_jobs(
                        message_row_id, chat_id, kind, generation, state,
                        available_at, created_at, updated_at
                    ) VALUES(?, ?, 'live', 1, 'queued', ?, ?, ?)
                    """,
                    (row_id, record.chat_id, queued_at, queued_at, queued_at),
                )
        return cursor.rowcount == 1

    def enqueue_manual_analysis(
        self,
        row_id: int,
        *,
        now: datetime,
        max_attempts: int = 5,
    ) -> tuple[dict[str, Any], bool]:
        """Queue a manual full reanalysis without allowing duplicate active work."""
        timestamp = to_iso(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            message = self.connection.execute(
                "SELECT id, chat_id FROM messages WHERE id = ?", (row_id,)
            ).fetchone()
            if message is None:
                raise ValueError("消息不存在")
            active = self.connection.execute(
                """
                SELECT * FROM analysis_jobs
                WHERE message_row_id = ? AND state IN ('queued', 'processing', 'retry')
                ORDER BY id DESC LIMIT 1
                """,
                (row_id,),
            ).fetchone()
            if active is not None:
                self.connection.commit()
                return dict(active), False
            generation_row = self.connection.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 AS value "
                "FROM analysis_jobs WHERE message_row_id = ?",
                (row_id,),
            ).fetchone()
            generation = int(generation_row["value"])
            cursor = self.connection.execute(
                """
                INSERT INTO analysis_jobs(
                    message_row_id, chat_id, kind, generation, state, attempts,
                    max_attempts, available_at, created_at, updated_at
                ) VALUES(?, ?, 'manual', ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    int(message["chat_id"]),
                    generation,
                    max(1, max_attempts),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute(
                """
                UPDATE messages
                SET analysis_queue_requested = 1, analysis_queue_state = 'queued',
                    analysis_queue_attempts = 0,
                    analysis_queue_available_at = ?,
                    analysis_queue_error_category = NULL,
                    analysis_queue_manual = 1,
                    ai_status = 'queued', push_eligible = 0,
                    push_gate_reason = 'manual_reanalysis_queued',
                    push_ready_at = NULL,
                    semantic_dedupe_status = 'awaiting_analysis',
                    semantic_dedupe_model = NULL,
                    semantic_dedupe_effort = NULL,
                    semantic_dedupe_confidence = NULL,
                    semantic_dedupe_reason = NULL,
                    semantic_dedupe_response_text = NULL,
                    semantic_dedupe_matched_message_id = NULL,
                    semantic_dedupe_material_update = 0,
                    semantic_dedupe_update_type = NULL,
                    semantic_dedupe_update_validated = 0,
                    semantic_dedupe_update_rejection_reason = NULL,
                    semantic_dedupe_checked_at = NULL,
                    semantic_dedupe_error_category = NULL,
                    semantic_dedupe_candidate_count = 0,
                    notification_prepare_status = 'awaiting_analysis',
                    notification_prepare_model = NULL,
                    notification_prepare_effort = NULL,
                    notification_title = NULL, notification_body = NULL,
                    notification_prepare_response_text = NULL,
                    notification_prepare_checked_at = NULL,
                    notification_prepare_error_category = NULL,
                    feedback_context_sample_count = 0,
                    feedback_context_summary = NULL,
                    feedback_context_json = NULL,
                    feedback_context_applied_at = NULL
                WHERE id = ?
                """,
                (timestamp, row_id),
            )
            job = self.connection.execute(
                "SELECT * FROM analysis_jobs WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
            self.connection.commit()
            return dict(job), True
        except Exception:
            self.connection.rollback()
            raise

    def recover_analysis_jobs(self, *, now: datetime) -> dict[str, int]:
        """Recover leased work and adopt legacy in-progress rows after restart."""
        timestamp = to_iso(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            interrupted = self.connection.execute(
                """
                UPDATE analysis_jobs
                SET state = 'retry', available_at = ?, lease_until = NULL,
                    error_category = 'interrupted', updated_at = ?
                WHERE state = 'processing'
                """,
                (timestamp, timestamp),
            ).rowcount
            interrupted_batches = self.connection.execute(
                """
                UPDATE analysis_batches
                SET state = 'interrupted', error_category = 'interrupted',
                    updated_at = ?, completed_at = ?
                WHERE state = 'processing'
                """,
                (timestamp, timestamp),
            ).rowcount
            self.connection.execute(
                """
                UPDATE analysis_batch_items
                SET state = 'retry', error_category = 'interrupted', updated_at = ?
                WHERE state IN ('claimed', 'classified')
                  AND job_id IN (
                      SELECT id FROM analysis_jobs
                      WHERE state = 'retry' AND error_category = 'interrupted'
                  )
                """,
                (timestamp,),
            )
            legacy_rows = self.connection.execute(
                """
                SELECT m.id, m.chat_id
                FROM messages AS m
                WHERE m.ai_status IN ('pending', 'queued', 'processing', 'retry')
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis_jobs AS j
                      WHERE j.message_row_id = m.id
                        AND j.state IN ('queued', 'processing', 'retry')
                  )
                ORDER BY m.sent_at, m.message_id, m.id
                """
            ).fetchall()
            adopted = 0
            for row in legacy_rows:
                generation = self.connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 AS value "
                    "FROM analysis_jobs WHERE message_row_id = ?",
                    (int(row["id"]),),
                ).fetchone()
                self.connection.execute(
                    """
                    INSERT INTO analysis_jobs(
                        message_row_id, chat_id, kind, generation, state,
                        available_at, error_category, created_at, updated_at
                    ) VALUES(?, ?, 'live', ?, 'retry', ?, 'interrupted', ?, ?)
                    """,
                    (
                        int(row["id"]),
                        int(row["chat_id"]),
                        int(generation["value"]),
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                adopted += 1
            self.connection.execute(
                """
                UPDATE messages
                SET ai_status = 'retry', analysis_queue_state = 'retry',
                    analysis_queue_available_at = ?,
                    analysis_queue_error_category = 'interrupted',
                    push_eligible = 0, push_gate_reason = 'analysis_retry'
                WHERE id IN (
                    SELECT message_row_id FROM analysis_jobs
                    WHERE state = 'retry' AND error_category = 'interrupted'
                )
                """,
                (timestamp,),
            )
            self.connection.commit()
            result = {"interrupted": max(0, interrupted), "adopted": adopted}
            if interrupted_batches:
                result["interrupted_batches"] = max(0, interrupted_batches)
            return result
        except Exception:
            self.connection.rollback()
            raise

    def claim_next_analysis_job(
        self,
        *,
        now: datetime,
        lease_seconds: int = 240,
    ) -> dict[str, Any] | None:
        """Claim the oldest job from the least-recently served chat."""
        timestamp = to_iso(now)
        lease_until = to_iso(now + timedelta(seconds=max(30, lease_seconds)))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            # Expired leases are safe to retry; the message/job unique keys make the work idempotent.
            expired = self.connection.execute(
                """
                UPDATE analysis_jobs
                SET state = 'retry', available_at = ?, lease_until = NULL,
                    error_category = 'interrupted', updated_at = ?
                WHERE state = 'processing' AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (timestamp, timestamp, timestamp),
            )
            if expired.rowcount:
                self.connection.execute(
                    """
                    UPDATE messages
                    SET ai_status = 'retry', analysis_queue_state = 'retry',
                        analysis_queue_available_at = ?,
                        analysis_queue_error_category = 'interrupted',
                        ai_error_category = 'interrupted', ai_error_stage = 'queue',
                        push_eligible = 0, push_gate_reason = 'analysis_retry'
                    WHERE id IN (
                        SELECT message_row_id FROM analysis_jobs
                        WHERE state = 'retry' AND error_category = 'interrupted'
                    )
                    """,
                    (timestamp,),
                )
            row = self.connection.execute(
                """
                SELECT j.*
                FROM analysis_jobs AS j
                JOIN messages AS m ON m.id = j.message_row_id
                LEFT JOIN analysis_chat_schedule AS schedule ON schedule.chat_id = j.chat_id
                WHERE j.state IN ('queued', 'retry')
                  AND j.available_at <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM analysis_jobs AS earlier
                      JOIN messages AS earlier_message
                        ON earlier_message.id = earlier.message_row_id
                      WHERE earlier.chat_id = j.chat_id
                        AND earlier.state IN ('queued', 'processing', 'retry')
                        AND (
                              earlier_message.sent_at < m.sent_at
                           OR (earlier_message.sent_at = m.sent_at
                               AND earlier_message.message_id < m.message_id)
                           OR (earlier_message.sent_at = m.sent_at
                               AND earlier_message.message_id = m.message_id
                               AND earlier.id < j.id)
                        )
                  )
                ORDER BY COALESCE(schedule.dispatch_order, 0) ASC,
                         j.available_at ASC, m.sent_at ASC, m.message_id ASC, j.id ASC
                LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            job_id = int(row["id"])
            attempts = int(row["attempts"]) + 1
            self.connection.execute(
                """
                UPDATE analysis_jobs
                SET state = 'processing', attempts = ?, lease_until = ?,
                    error_category = NULL, error_stage = NULL, updated_at = ?
                WHERE id = ?
                """,
                (attempts, lease_until, timestamp, job_id),
            )
            next_order = self.connection.execute(
                "SELECT COALESCE(MAX(dispatch_order), 0) + 1 AS value "
                "FROM analysis_chat_schedule"
            ).fetchone()
            self.connection.execute(
                """
                INSERT INTO analysis_chat_schedule(
                    chat_id, last_dispatched_at, dispatch_order
                ) VALUES(?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE
                SET last_dispatched_at = excluded.last_dispatched_at,
                    dispatch_order = excluded.dispatch_order
                """,
                (int(row["chat_id"]), timestamp, int(next_order["value"])),
            )
            self.connection.execute(
                """
                UPDATE messages
                SET ai_status = 'processing', analysis_queue_state = 'processing',
                    analysis_queue_attempts = ?, analysis_queue_available_at = NULL,
                    analysis_queue_error_category = NULL,
                    analysis_queue_manual = ?, push_eligible = 0,
                    push_gate_reason = 'analysis_processing', ai_started_at = NULL,
                    push_ready_at = CASE
                        WHEN notification_prepare_status IN (
                            'preparing', 'success', 'failed_fallback'
                        )
                            THEN push_ready_at
                        ELSE NULL
                    END,
                    semantic_dedupe_status = CASE
                        WHEN notification_prepare_status IN (
                            'preparing', 'success', 'failed_fallback'
                        )
                            THEN semantic_dedupe_status
                        ELSE 'awaiting_analysis'
                    END,
                    notification_prepare_status = CASE
                        WHEN notification_prepare_status IN (
                            'preparing', 'success', 'failed_fallback'
                        )
                            THEN notification_prepare_status
                        ELSE 'awaiting_analysis'
                    END
                WHERE id = ?
                """,
                (attempts, int(row["kind"] == "manual"), int(row["message_row_id"])),
            )
            claimed = self.connection.execute(
                "SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            self.connection.commit()
            return dict(claimed)
        except Exception:
            self.connection.rollback()
            raise

    def claim_next_analysis_batch(
        self,
        *,
        now: datetime,
        lease_seconds: int = 1500,
        target_items: int = 20,
        max_items: int = 50,
        max_chars: int = 12_000,
        max_wait_seconds: int = 2,
    ) -> dict[str, Any] | None:
        """Claim one chronological same-chat batch using the fair chat head."""
        target_items = max(1, int(target_items))
        max_items = max(target_items, int(max_items))
        max_chars = max(1_000, int(max_chars))
        max_wait_seconds = max(0, int(max_wait_seconds))
        timestamp = to_iso(now)
        lease_until = to_iso(now + timedelta(seconds=max(30, lease_seconds)))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            expired = self.connection.execute(
                """
                UPDATE analysis_jobs
                SET state = 'retry', available_at = ?, lease_until = NULL,
                    error_category = 'interrupted', updated_at = ?
                WHERE state = 'processing' AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (timestamp, timestamp, timestamp),
            )
            if expired.rowcount:
                self.connection.execute(
                    """
                    UPDATE messages
                    SET ai_status = 'retry', analysis_queue_state = 'retry',
                        analysis_queue_available_at = ?,
                        analysis_queue_error_category = 'interrupted',
                        ai_error_category = 'interrupted', ai_error_stage = 'queue',
                        push_eligible = 0, push_gate_reason = 'analysis_retry'
                    WHERE id IN (
                        SELECT message_row_id FROM analysis_jobs
                        WHERE state = 'retry' AND error_category = 'interrupted'
                    )
                    """,
                    (timestamp,),
                )
                self.connection.execute(
                    """
                    UPDATE analysis_batches
                    SET state = 'interrupted', error_category = 'interrupted',
                        updated_at = ?, completed_at = ?
                    WHERE state = 'processing' AND EXISTS (
                        SELECT 1 FROM analysis_batch_items AS item
                        JOIN analysis_jobs AS job ON job.id = item.job_id
                        WHERE item.batch_id = analysis_batches.id
                          AND job.state = 'retry'
                          AND job.error_category = 'interrupted'
                    )
                    """,
                    (timestamp, timestamp),
                )

            heads = self.connection.execute(
                """
                SELECT j.*, m.sent_at, m.message_id, m.text, m.created_at AS message_created_at
                FROM analysis_jobs AS j
                JOIN messages AS m ON m.id = j.message_row_id
                LEFT JOIN analysis_chat_schedule AS schedule ON schedule.chat_id = j.chat_id
                WHERE j.state IN ('queued', 'retry')
                  AND j.available_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis_jobs AS active
                      WHERE active.chat_id = j.chat_id AND active.state = 'processing'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM analysis_jobs AS earlier
                      JOIN messages AS earlier_message
                        ON earlier_message.id = earlier.message_row_id
                      WHERE earlier.chat_id = j.chat_id
                        AND earlier.state IN ('queued', 'processing', 'retry')
                        AND (
                              earlier_message.sent_at < m.sent_at
                           OR (earlier_message.sent_at = m.sent_at
                               AND earlier_message.message_id < m.message_id)
                           OR (earlier_message.sent_at = m.sent_at
                               AND earlier_message.message_id = m.message_id
                               AND earlier.id < j.id)
                        )
                  )
                ORDER BY COALESCE(schedule.dispatch_order, 0), j.available_at,
                         m.sent_at, m.message_id, j.id
                LIMIT 256
                """,
                (timestamp,),
            ).fetchall()
            if not heads:
                self.connection.commit()
                return None
            selected_head: sqlite3.Row | None = None
            candidates: list[sqlite3.Row] = []
            for possible_head in heads:
                if str(possible_head["kind"]) == "manual":
                    selected_head = possible_head
                    candidates = [possible_head]
                    break
                active_rows = self.connection.execute(
                    """
                    SELECT j.*, m.sent_at, m.message_id, m.text,
                           m.created_at AS message_created_at
                    FROM analysis_jobs AS j
                    JOIN messages AS m ON m.id = j.message_row_id
                    WHERE j.chat_id = ?
                      AND j.state IN ('queued', 'retry')
                    ORDER BY m.sent_at, m.message_id, j.id
                    LIMIT ?
                    """,
                    (int(possible_head["chat_id"]), max_items + 1),
                ).fetchall()
                available_rows: list[sqlite3.Row] = []
                for candidate in active_rows:
                    if str(candidate["kind"]) != str(possible_head["kind"]):
                        break
                    if str(candidate["available_at"]) > timestamp:
                        break
                    available_rows.append(candidate)
                    if len(available_rows) >= max_items:
                        break
                try:
                    created_at = datetime.fromisoformat(
                        str(possible_head["created_at"]).replace("Z", "+00:00")
                    )
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    wait_elapsed = (now - created_at).total_seconds()
                except (TypeError, ValueError):
                    wait_elapsed = float(max_wait_seconds)
                if len(available_rows) < target_items and wait_elapsed < max_wait_seconds:
                    continue
                selected: list[sqlite3.Row] = []
                used_chars = 0
                for candidate in available_rows:
                    item_chars = len(str(candidate["sent_at"])[:64]) + len(
                        str(candidate["text"] or "")[:MAX_INPUT_TEXT_LENGTH]
                    )
                    if selected and used_chars + item_chars > max_chars:
                        break
                    selected.append(candidate)
                    used_chars += item_chars
                    if len(selected) >= max_items:
                        break
                if selected:
                    selected_head = possible_head
                    candidates = selected
                    break
            if selected_head is None or not candidates:
                self.connection.commit()
                return None
            head = selected_head

            job_ids = [int(candidate["id"]) for candidate in candidates]
            placeholders = ",".join("?" for _ in job_ids)
            self.connection.execute(
                f"""
                UPDATE analysis_jobs
                SET state = 'processing', attempts = attempts + 1,
                    lease_until = ?, error_category = NULL, error_stage = NULL,
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,  # noqa: S608 -- placeholders are generated, not user input
                (lease_until, timestamp, *job_ids),
            )
            next_order = self.connection.execute(
                "SELECT COALESCE(MAX(dispatch_order), 0) + 1 AS value "
                "FROM analysis_chat_schedule"
            ).fetchone()
            self.connection.execute(
                """
                INSERT INTO analysis_chat_schedule(chat_id, last_dispatched_at, dispatch_order)
                VALUES(?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE
                SET last_dispatched_at = excluded.last_dispatched_at,
                    dispatch_order = excluded.dispatch_order
                """,
                (int(head["chat_id"]), timestamp, int(next_order["value"])),
            )
            claimed = self.connection.execute(
                f"SELECT * FROM analysis_jobs WHERE id IN ({placeholders}) ORDER BY id",  # noqa: S608
                job_ids,
            ).fetchall()
            claimed_by_id = {int(row["id"]): row for row in claimed}
            ordered_jobs = [dict(claimed_by_id[job_id]) for job_id in job_ids]
            message_ids = [int(job["message_row_id"]) for job in ordered_jobs]
            message_placeholders = ",".join("?" for _ in message_ids)
            self.connection.execute(
                f"""
                UPDATE messages
                SET ai_status = 'processing', analysis_queue_state = 'processing',
                    analysis_queue_attempts = (
                        SELECT attempts FROM analysis_jobs
                        WHERE analysis_jobs.message_row_id = messages.id
                          AND analysis_jobs.state = 'processing'
                        LIMIT 1
                    ),
                    analysis_queue_available_at = NULL,
                    analysis_queue_error_category = NULL,
                    analysis_queue_manual = ?, push_eligible = 0,
                    push_gate_reason = 'analysis_processing', ai_started_at = NULL,
                    push_ready_at = CASE
                        WHEN notification_prepare_status IN ('preparing', 'success', 'failed_fallback')
                            THEN push_ready_at ELSE NULL END,
                    semantic_dedupe_status = CASE
                        WHEN notification_prepare_status IN ('preparing', 'success', 'failed_fallback')
                            THEN semantic_dedupe_status ELSE 'awaiting_analysis' END,
                    notification_prepare_status = CASE
                        WHEN notification_prepare_status IN ('preparing', 'success', 'failed_fallback')
                            THEN notification_prepare_status ELSE 'awaiting_analysis' END
                WHERE id IN ({message_placeholders})
                """,  # noqa: S608
                (int(str(head["kind"]) == "manual"), *message_ids),
            )
            input_chars = sum(
                len(str(candidate["sent_at"])[:64])
                + len(str(candidate["text"] or "")[:MAX_INPUT_TEXT_LENGTH])
                for candidate in candidates
            )
            batch_cursor = self.connection.execute(
                """
                INSERT INTO analysis_batches(
                    chat_id, kind, state, item_count, input_chars, created_at, updated_at
                ) VALUES(?, ?, 'processing', ?, ?, ?, ?)
                """,
                (
                    int(head["chat_id"]),
                    str(head["kind"]),
                    len(ordered_jobs),
                    input_chars,
                    timestamp,
                    timestamp,
                ),
            )
            batch_id = int(batch_cursor.lastrowid)
            self.connection.executemany(
                """
                INSERT INTO analysis_batch_items(
                    batch_id, job_id, message_row_id, item_order, state, updated_at
                ) VALUES(?, ?, ?, ?, 'claimed', ?)
                """,
                (
                    (
                        batch_id,
                        int(job["id"]),
                        int(job["message_row_id"]),
                        index,
                        timestamp,
                    )
                    for index, job in enumerate(ordered_jobs)
                ),
            )
            self.connection.commit()
            return {
                "id": batch_id,
                "chat_id": int(head["chat_id"]),
                "kind": str(head["kind"]),
                "item_count": len(ordered_jobs),
                "input_chars": input_chars,
                "jobs": ordered_jobs,
            }
        except Exception:
            self.connection.rollback()
            raise

    def retry_analysis_job(
        self,
        job_id: int,
        *,
        now: datetime,
        delay_seconds: int,
        error_category: str,
        error_stage: str | None,
    ) -> bool:
        available = now + timedelta(seconds=max(1, delay_seconds))
        timestamp = to_iso(now)
        available_at = to_iso(available)
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE analysis_jobs
                SET state = 'retry', available_at = ?, lease_until = NULL,
                    error_category = ?, error_stage = ?, updated_at = ?
                WHERE id = ? AND state = 'processing' AND attempts < max_attempts
                """,
                (available_at, error_category, error_stage, timestamp, job_id),
            )
            if cursor.rowcount == 1:
                self.connection.execute(
                    """
                    UPDATE messages
                    SET ai_status = 'retry', analysis_queue_state = 'retry',
                        analysis_queue_available_at = ?,
                        analysis_queue_error_category = ?,
                        ai_error_category = ?, ai_error_stage = ?,
                        push_eligible = 0, push_gate_reason = 'analysis_retry'
                    WHERE id = (SELECT message_row_id FROM analysis_jobs WHERE id = ?)
                    """,
                    (available_at, error_category, error_category, error_stage, job_id),
                )
        return cursor.rowcount == 1

    def finish_analysis_job(
        self,
        job_id: int,
        *,
        now: datetime,
        succeeded: bool,
        result_status: str,
        error_category: str | None = None,
        error_stage: str | None = None,
    ) -> int:
        """Finish a job and atomically persist any live immediate deliveries."""
        state = "succeeded" if succeeded else "failed"
        timestamp = to_iso(now)
        created_deliveries = 0
        with self.connection:
            job = self.connection.execute(
                "SELECT message_row_id, kind FROM analysis_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            finished = self.connection.execute(
                """
                UPDATE analysis_jobs
                SET state = ?, lease_until = NULL, error_category = ?,
                    error_stage = ?, result_status = ?, updated_at = ?, completed_at = ?
                WHERE id = ? AND state = 'processing'
                """,
                (
                    state,
                    error_category,
                    error_stage,
                    result_status,
                    timestamp,
                    timestamp,
                    job_id,
                ),
            )
            self.connection.execute(
                """
                UPDATE messages
                SET analysis_queue_state = ?, analysis_queue_available_at = NULL,
                    analysis_queue_error_category = ?
                WHERE id = (SELECT message_row_id FROM analysis_jobs WHERE id = ?)
                """,
                (state, error_category, job_id),
            )
            if (
                job is not None
                and finished.rowcount == 1
                and str(job["kind"]) == "live"
                and succeeded
                and result_status == "success"
            ):
                eligible = self.connection.execute(
                    """
                    SELECT id FROM messages
                    WHERE id = ? AND push_eligible = 1
                      AND notification_prepare_status IN ('success', 'failed_fallback')
                      AND ai_status = 'success'
                      AND (content_kind IN ('news', 'community_signal', 'benefit_deal')
                           OR (content_kind IS NULL AND ai_category = 'external_information'))
                      AND ai_score IS NOT NULL AND ai_score >= ?
                    """,
                    (int(job["message_row_id"]), PUSH_SCORE_THRESHOLD),
                ).fetchone()
                if eligible is not None:
                    for channel in self.configured_push_channels():
                        cursor = self.connection.execute(
                            """
                            INSERT OR IGNORE INTO deliveries(
                                message_row_id, channel, delivery_type, batch_key,
                                state, available_at, created_at, updated_at
                            ) VALUES(?, ?, 'immediate', '', 'queued', ?, ?, ?)
                            """,
                            (
                                int(job["message_row_id"]),
                                channel,
                                timestamp,
                                timestamp,
                                timestamp,
                            ),
                        )
                        created_deliveries += max(0, cursor.rowcount)
        return created_deliveries

    def record_analysis_batch_call(
        self,
        batch_id: int,
        *,
        parent_call_id: int | None,
        item_count: int,
        input_chars: int,
        outcome: Any,
        now: datetime,
    ) -> int:
        timestamp = to_iso(now)
        state = "success" if outcome.status == "success" else (
            "partial" if outcome.status == "partial" else "error"
        )
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO analysis_batch_calls(
                    batch_id, parent_call_id, item_count, input_chars, state,
                    model, reasoning_effort, response_text, protocol_errors_json,
                    error_category, prompt_tokens, completion_tokens, total_tokens,
                    latency_ms, created_at, completed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(batch_id),
                    int(parent_call_id) if parent_call_id is not None else None,
                    max(0, int(item_count)),
                    max(0, int(input_chars)),
                    state,
                    str(outcome.model),
                    str(outcome.effort),
                    outcome.response_text,
                    json.dumps(
                        tuple(outcome.protocol_errors),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    outcome.error_category,
                    outcome.prompt_tokens,
                    outcome.completion_tokens,
                    outcome.total_tokens,
                    outcome.latency_ms,
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute(
                """
                UPDATE analysis_batches
                SET call_count = call_count + 1, model = ?, reasoning_effort = ?,
                    updated_at = ?, error_category = CASE
                        WHEN ? IS NULL THEN error_category ELSE ? END
                WHERE id = ?
                """,
                (
                    str(outcome.model),
                    str(outcome.effort),
                    timestamp,
                    outcome.error_category,
                    outcome.error_category,
                    int(batch_id),
                ),
            )
        return int(cursor.lastrowid)

    def increment_analysis_batch_split(self, batch_id: int, *, now: datetime) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE analysis_batches
                SET split_count = split_count + 1, updated_at = ? WHERE id = ?
                """,
                (to_iso(now), int(batch_id)),
            )

    def renew_analysis_batch_leases(
        self,
        batch_id: int,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> int:
        lease_until = to_iso(now + timedelta(seconds=max(30, int(lease_seconds))))
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE analysis_jobs
                SET lease_until = ?, updated_at = ?
                WHERE state = 'processing' AND id IN (
                    SELECT job_id FROM analysis_batch_items WHERE batch_id = ?
                )
                """,
                (lease_until, to_iso(now), int(batch_id)),
            )
        return max(0, cursor.rowcount)

    def store_analysis_batch_classification(
        self,
        batch_id: int,
        job_id: int,
        *,
        call_id: int,
        classification: ClassificationOutcome,
        now: datetime,
    ) -> None:
        if classification.status != "success" or classification.category is None:
            raise ValueError("只能缓存成功的批量分类")
        payload = json.dumps(
            {
                "model": classification.model,
                "category": classification.category,
                "confidence": classification.confidence,
                "summary": classification.summary,
                "reason": classification.reason,
                "response_text": classification.response_text,
                "effort": classification.effort,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE analysis_batch_items
                SET state = 'classified', classification_call_id = ?,
                    classification_json = ?, error_category = NULL, updated_at = ?
                WHERE batch_id = ? AND job_id = ?
                """,
                (int(call_id), payload, to_iso(now), int(batch_id), int(job_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("批量分类条目不存在")

    def mark_analysis_batch_item_call(
        self,
        batch_id: int,
        job_id: int,
        *,
        call_id: int,
        error_category: str | None,
        now: datetime,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE analysis_batch_items
                SET classification_call_id = ?, error_category = ?, updated_at = ?
                WHERE batch_id = ? AND job_id = ?
                """,
                (
                    int(call_id),
                    error_category,
                    to_iso(now),
                    int(batch_id),
                    int(job_id),
                ),
            )

    def cached_analysis_batch_classification(
        self,
        job_id: int,
    ) -> tuple[ClassificationOutcome, int, int] | None:
        row = self.connection.execute(
            """
            SELECT item.batch_id, item.classification_call_id, item.classification_json
            FROM analysis_batch_items AS item
            WHERE item.job_id = ? AND item.classification_json IS NOT NULL
            ORDER BY item.batch_id DESC LIMIT 1
            """,
            (int(job_id),),
        ).fetchone()
        if row is None or row["classification_call_id"] is None:
            return None
        try:
            value = json.loads(str(row["classification_json"]))
            category = str(value["category"])
            if category not in {
                "internal_governance",
                "internal_coordination",
                "external_information",
                "discussion",
                "promotion_spam",
                "unknown",
            }:
                return None
            confidence = int(value["confidence"])
            if not 0 <= confidence <= 100:
                return None
            classification = ClassificationOutcome(
                status="success",
                model=str(value["model"]),
                category=category,
                confidence=confidence,
                summary=str(value["summary"]),
                reason=str(value["reason"]),
                response_text=str(value["response_text"]),
                effort=validate_reasoning_effort(str(value["effort"])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return classification, int(row["batch_id"]), int(row["classification_call_id"])

    def link_message_classification_batch(
        self,
        row_id: int,
        *,
        batch_id: int,
        call_id: int,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE messages
                SET ai_classification_batch_id = ?, ai_classification_call_id = ?
                WHERE id = ?
                """,
                (int(batch_id), int(call_id), int(row_id)),
            )

    def update_analysis_batch_item(
        self,
        batch_id: int,
        job_id: int,
        *,
        state: str,
        error_category: str | None,
        now: datetime,
    ) -> None:
        if state not in {"local_terminal", "classified", "succeeded", "retry", "failed"}:
            raise ValueError("批量任务条目状态无效")
        with self.connection:
            self.connection.execute(
                """
                UPDATE analysis_batch_items
                SET state = ?, error_category = ?, updated_at = ?
                WHERE batch_id = ? AND job_id = ?
                """,
                (state, error_category, to_iso(now), int(batch_id), int(job_id)),
            )

    def finish_analysis_batch(self, batch_id: int, *, now: datetime) -> dict[str, int]:
        counts = {
            str(row["state"]): int(row["count"])
            for row in self.connection.execute(
                """
                SELECT state, COUNT(*) AS count FROM analysis_batch_items
                WHERE batch_id = ? GROUP BY state
                """,
                (int(batch_id),),
            )
        }
        retry_count = counts.get("retry", 0)
        failed_count = counts.get("failed", 0)
        claimed_count = counts.get("claimed", 0) + counts.get("classified", 0)
        if claimed_count:
            state = "interrupted"
        elif retry_count or failed_count:
            state = "partial" if counts.get("succeeded", 0) or counts.get("local_terminal", 0) else "failed"
        else:
            state = "succeeded"
        timestamp = to_iso(now)
        with self.connection:
            self.connection.execute(
                """
                UPDATE analysis_batches
                SET state = ?, classified_count = ?, terminal_count = ?,
                    retry_count = ?, failed_count = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    counts.get("succeeded", 0),
                    counts.get("local_terminal", 0),
                    retry_count,
                    failed_count,
                    timestamp,
                    timestamp,
                    int(batch_id),
                ),
            )
        return counts

    def get_analysis_job(self, job_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def analysis_queue_stats(self, *, hours: int, now: datetime) -> dict[str, Any]:
        cutoff = to_iso(now - timedelta(hours=hours))
        states = {
            str(row["state"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT state, COUNT(*) AS count FROM analysis_jobs GROUP BY state"
            )
        }
        terminal = self.connection.execute(
            """
            SELECT
                SUM(CASE WHEN state = 'succeeded' THEN 1 ELSE 0 END) AS successes,
                SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS failures
            FROM analysis_jobs
            WHERE completed_at >= ?
            """,
            (cutoff,),
        ).fetchone()
        successes = int(terminal["successes"] or 0)
        failures = int(terminal["failures"] or 0)
        total = successes + failures
        errors = [
            {"category": str(row["error_category"]), "count": int(row["count"])}
            for row in self.connection.execute(
                """
                SELECT error_category, COUNT(*) AS count
                FROM analysis_jobs
                WHERE updated_at >= ? AND error_category IS NOT NULL
                GROUP BY error_category ORDER BY count DESC, error_category
                """,
                (cutoff,),
            )
        ]
        retry = states.get("retry", 0)
        degraded = retry > 0 or any(
            item["category"] in {"busy", "network_error", "timeout", "rate_limited", "upstream_error"}
            for item in errors
        )
        batch = self.connection.execute(
            """
            SELECT COUNT(*) AS batches,
                   COALESCE(SUM(call_count), 0) AS calls,
                   COALESCE(SUM(item_count), 0) AS claimed_items,
                   COALESCE(SUM(split_count), 0) AS splits,
                   COALESCE(SUM(input_chars), 0) AS input_chars,
                   COALESCE(SUM(CASE WHEN state = 'interrupted' THEN 1 ELSE 0 END), 0)
                       AS interrupted
            FROM analysis_batches WHERE created_at >= ?
            """,
            (cutoff,),
        ).fetchone()
        batch_calls = int(batch["calls"] or 0)
        batch_completed_items = int(
            self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM analysis_batch_items AS item
                JOIN analysis_batches AS batch ON batch.id = item.batch_id
                WHERE batch.created_at >= ? AND item.classification_json IS NOT NULL
                """,
                (cutoff,),
            ).fetchone()["count"]
        )
        batch_errors = [
            {"category": str(row["error_category"]), "count": int(row["count"])}
            for row in self.connection.execute(
                """
                SELECT call.error_category, COUNT(*) AS count
                FROM analysis_batch_calls AS call
                JOIN analysis_batches AS batch ON batch.id = call.batch_id
                WHERE batch.created_at >= ? AND call.error_category IS NOT NULL
                GROUP BY call.error_category ORDER BY count DESC, call.error_category
                """,
                (cutoff,),
            )
        ]
        return {
            "pending": states.get("queued", 0),
            "processing": states.get("processing", 0),
            "retry": retry,
            "failed": states.get("failed", 0),
            "rolling_successes": successes,
            "rolling_failures": failures,
            "success_rate": round(successes * 100 / total, 1) if total else None,
            "error_rate": round(failures * 100 / total, 1) if total else None,
            "error_categories": errors,
            "health": "degraded" if degraded else "normal",
            "classification_batches": int(batch["batches"] or 0),
            "classification_batch_calls": batch_calls,
            "classification_batch_claimed_items": int(batch["claimed_items"] or 0),
            "classification_batch_items": batch_completed_items,
            "classification_items_per_call": (
                round(batch_completed_items / batch_calls, 2) if batch_calls else None
            ),
            "classification_calls_saved": max(0, batch_completed_items - batch_calls),
            "classification_batch_splits": int(batch["splits"] or 0),
            "classification_batch_input_chars": int(batch["input_chars"] or 0),
            "classification_batches_interrupted": int(batch["interrupted"] or 0),
            "classification_batch_error_categories": batch_errors,
        }

    def get_message(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        value = self._decode_row(row)
        if value is not None:
            value["deliveries"] = self.get_deliveries(int(value["id"]))
            value["ntfy_feedback"] = self.get_ntfy_feedback(int(value["id"]))
        return value

    def get_message_by_id(
        self,
        row_id: int,
        *,
        include_ai_response: bool = True,
        include_related: bool = False,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM messages WHERE id = ?",
            (row_id,),
        ).fetchone()
        value = self._decode_row(row, include_ai_response=include_ai_response)
        if value is not None:
            if include_ai_response and value.get("ai_classification_call_id") is not None:
                call = self.connection.execute(
                    """
                    SELECT batch_id, id, state, item_count, model, reasoning_effort,
                           response_text, protocol_errors_json, error_category,
                           prompt_tokens, completion_tokens, total_tokens, latency_ms
                    FROM analysis_batch_calls WHERE id = ?
                    """,
                    (int(value["ai_classification_call_id"]),),
                ).fetchone()
                if call is not None:
                    value["ai_classification_batch"] = dict(call)
            value["deliveries"] = self.get_deliveries(int(value["id"]))
            value["ntfy_feedback"] = self.get_ntfy_feedback(int(value["id"]))
            if include_related:
                value["similar_cluster"] = self.semantic_message_cluster(
                    int(value["id"])
                )
        return value

    def _semantic_cluster_root_id(self, row_id: int) -> int | None:
        current_id = int(row_id)
        seen: set[int] = set()
        for _ in range(32):
            if current_id in seen:
                return None
            seen.add(current_id)
            row = self.connection.execute(
                "SELECT semantic_dedupe_matched_message_id FROM messages WHERE id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                return None
            parent_id = row["semantic_dedupe_matched_message_id"]
            if parent_id is None:
                return current_id
            current_id = int(parent_id)
        return None

    def semantic_message_cluster(self, row_id: int) -> dict[str, Any]:
        """Return the bounded semantic duplicate tree for an authenticated detail view."""
        root_id = self._semantic_cluster_root_id(row_id)
        if root_id is None:
            return {"representative": None, "similar_count": 0, "items": []}
        rows = self.connection.execute(
            """
            WITH RECURSIVE cluster(id) AS (
                VALUES(?)
                UNION
                SELECT messages.id
                FROM messages JOIN cluster
                  ON messages.semantic_dedupe_matched_message_id = cluster.id
            )
            SELECT messages.* FROM messages JOIN cluster ON cluster.id = messages.id
            ORDER BY messages.created_at DESC, messages.id DESC
            LIMIT 101
            """,
            (root_id,),
        ).fetchall()
        values = [
            value
            for row in rows
            if (value := self._decode_row(row, include_ai_response=False)) is not None
        ]
        representative = next(
            (value for value in values if int(value["id"]) == root_id),
            None,
        )
        items = [value for value in values if int(value["id"]) != root_id][:100]
        return {
            "representative": representative,
            "similar_count": self.semantic_message_cluster_count(root_id),
            "items": items,
        }

    def semantic_message_cluster_count(self, row_id: int) -> int:
        root_id = self._semantic_cluster_root_id(row_id)
        if root_id is None:
            return 0
        row = self.connection.execute(
            """
            WITH RECURSIVE cluster(id) AS (
                VALUES(?)
                UNION
                SELECT messages.id
                FROM messages JOIN cluster
                  ON messages.semantic_dedupe_matched_message_id = cluster.id
            )
            SELECT CASE WHEN COUNT(*) > 0 THEN COUNT(*) - 1 ELSE 0 END AS similar_count
            FROM cluster
            """,
            (root_id,),
        ).fetchone()
        return int(row["similar_count"] or 0)

    def llm_session_key(self, chat_id: int) -> str:
        return derive_chat_session_key(self._llm_session_namespace(), chat_id)

    def llm_dedupe_session_key(self) -> str:
        return derive_dedupe_session_key(self._llm_session_namespace())

    def _llm_session_namespace(self) -> bytes:
        metadata_key = "llm_session_namespace_v1"
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (metadata_key,)
        ).fetchone()
        if row is None:
            candidate = secrets.token_hex(SESSION_NAMESPACE_BYTES)
            with self.connection:
                self.connection.execute(
                    "INSERT OR IGNORE INTO metadata(key, value) VALUES(?, ?)",
                    (metadata_key, candidate),
                )
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (metadata_key,)
            ).fetchone()
        try:
            namespace = bytes.fromhex(str(row["value"])) if row is not None else b""
        except ValueError as exc:
            raise RuntimeError("模型会话命名空间损坏") from exc
        if len(namespace) != SESSION_NAMESPACE_BYTES:
            raise RuntimeError("模型会话命名空间损坏")
        return namespace

    def recover_semantic_dedupe(self, *, now: datetime) -> int:
        timestamp = to_iso(now)
        with self.connection:
            self.connection.execute(
                """
                UPDATE semantic_dedupe_lock
                SET owner_token = NULL, lease_until = NULL, updated_at = ?
                WHERE id = 1
                """,
                (timestamp,),
            )
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET semantic_dedupe_status = 'pending',
                    semantic_dedupe_error_category = 'interrupted',
                    push_eligible = 0, push_ready_at = NULL,
                    push_gate_reason = 'semantic_dedupe_pending'
                WHERE semantic_dedupe_status = 'checking'
                  AND ai_status = 'success'
                  AND (content_kind IN ('news', 'community_signal', 'benefit_deal')
                       OR (content_kind IS NULL AND ai_category = 'external_information'))
                  AND ai_score >= ?
                """,
                (PUSH_SCORE_THRESHOLD,),
            )
        return max(0, cursor.rowcount)

    def acquire_semantic_dedupe_lock(
        self,
        owner_token: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        timestamp = to_iso(now)
        lease_until = to_iso(now + timedelta(seconds=max(30, lease_seconds)))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                UPDATE semantic_dedupe_lock
                SET owner_token = ?, lease_until = ?, updated_at = ?
                WHERE id = 1
                  AND (
                       owner_token IS NULL
                    OR lease_until IS NULL
                    OR lease_until <= ?
                    OR owner_token = ?
                  )
                """,
                (owner_token, lease_until, timestamp, timestamp, owner_token),
            )
            self.connection.commit()
            return cursor.rowcount == 1
        except Exception:
            self.connection.rollback()
            raise

    def release_semantic_dedupe_lock(self, owner_token: str, *, now: datetime) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE semantic_dedupe_lock
                SET owner_token = NULL, lease_until = NULL, updated_at = ?
                WHERE id = 1 AND owner_token = ?
                """,
                (to_iso(now), owner_token),
            )

    def begin_semantic_dedupe(
        self,
        row_id: int,
        *,
        now: datetime,
        window_hours: int = SEMANTIC_DEDUPE_WINDOW_HOURS,
        limit: int = SEMANTIC_DEDUPE_QUERY_LIMIT,
    ) -> dict[str, Any] | None:
        current = self.connection.execute(
            """
            SELECT * FROM messages
            WHERE id = ? AND ai_status = 'success'
              AND (content_kind IN ('news', 'community_signal', 'benefit_deal')
                   OR (content_kind IS NULL AND ai_category = 'external_information'))
              AND ai_score IS NOT NULL AND ai_score >= ?
              AND semantic_dedupe_status IN ('pending', 'checking')
            """,
            (row_id, PUSH_SCORE_THRESHOLD),
        ).fetchone()
        if current is None:
            return None
        since = to_iso(now - timedelta(hours=max(1, window_hours)))
        rows = self.connection.execute(
            """
            SELECT candidate.*,
                   EXISTS(
                       SELECT 1 FROM deliveries AS delivered
                       WHERE delivered.message_row_id = candidate.id
                         AND delivered.state = 'succeeded'
                   ) AS has_succeeded_delivery,
                   EXISTS(
                       SELECT 1 FROM deliveries AS active
                       WHERE active.message_row_id = candidate.id
                         AND active.state IN ('queued', 'processing', 'retry')
                   ) AS has_active_delivery
            FROM messages AS candidate
            WHERE candidate.id != ?
              AND candidate.ai_status = 'success'
              AND (candidate.content_kind IN ('news', 'community_signal', 'benefit_deal')
                   OR (candidate.content_kind IS NULL
                       AND candidate.ai_category = 'external_information'))
              AND candidate.ai_score IS NOT NULL
              AND candidate.ai_score >= ?
              AND (
                    EXISTS(
                        SELECT 1 FROM deliveries AS delivery
                        WHERE delivery.message_row_id = candidate.id
                          AND delivery.state IN ('queued', 'processing', 'retry', 'succeeded')
                    )
                    OR (
                        candidate.push_eligible = 1
                        AND candidate.analysis_queue_state = 'processing'
                        AND candidate.push_ready_at IS NOT NULL
                        AND candidate.semantic_dedupe_status IN (
                            'unique', 'unique_no_candidates', 'material_update',
                            'failed_open', 'low_confidence_pass',
                            'representative_replaced'
                        )
                    )
              )
              AND COALESCE(
                    (
                        SELECT MAX(COALESCE(delivery.delivered_at, delivery.created_at))
                        FROM deliveries AS delivery
                        WHERE delivery.message_row_id = candidate.id
                          AND delivery.state IN ('queued', 'processing', 'retry', 'succeeded')
                    ),
                    candidate.push_ready_at,
                    candidate.ai_completed_at
                  ) >= ?
            ORDER BY COALESCE(candidate.push_ready_at, candidate.ai_completed_at) DESC,
                     candidate.ai_score DESC, candidate.id DESC
            LIMIT ?
            """,
            (
                row_id,
                PUSH_SCORE_THRESHOLD,
                since,
                max(1, min(int(limit), SEMANTIC_DEDUPE_QUERY_LIMIT)),
            ),
        ).fetchall()
        with self.connection:
            self.connection.execute(
                """
                UPDATE messages
                SET semantic_dedupe_status = 'checking',
                    semantic_dedupe_candidate_count = ?,
                    semantic_dedupe_error_category = NULL,
                    semantic_dedupe_update_type = NULL,
                    semantic_dedupe_update_validated = 0,
                    semantic_dedupe_update_rejection_reason = NULL,
                    push_eligible = 0, push_ready_at = NULL,
                    push_gate_reason = 'semantic_dedupe_checking'
                WHERE id = ? AND semantic_dedupe_status IN ('pending', 'checking')
                """,
                (len(rows), row_id),
            )
        return {
            "current": self._decode_row(current),
            "candidates": tuple(self._decode_row(row) for row in rows),
        }

    def complete_semantic_dedupe(
        self,
        row_id: int,
        *,
        outcome: SemanticDedupeOutcome | None,
        candidates: tuple[dict[str, Any], ...],
        model: str | None,
        now: datetime,
        allow_push: bool,
    ) -> str:
        timestamp = to_iso(now)
        candidate_count = len(candidates)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                """
                SELECT *
                FROM messages WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
            if current is None:
                raise ValueError("消息不存在")
            if str(current["semantic_dedupe_status"]) != "checking":
                self.connection.commit()
                return str(current["semantic_dedupe_status"])

            status = "unique_no_candidates"
            gate_reason = "eligible_unique"
            eligible = False
            matched_row_id: int | None = None
            material_update = False
            update_type: str | None = None
            update_validated = False
            update_rejection_reason: str | None = None
            confidence: int | None = None
            reason = "最近 24 小时没有可比较的已接受资讯"
            response_text: str | None = None
            error_category: str | None = None

            if outcome is not None:
                confidence = outcome.confidence
                reason = outcome.reason or "语义去重检查未返回理由"
                response_text = outcome.response_text
                error_category = outcome.error_category
                if outcome.status != "success":
                    status = "failed_open"
                    gate_reason = "semantic_dedupe_failed_open"
                    reason = "去重检查失败，为避免漏掉重要资讯已安全放行"
                elif outcome.same_event and outcome.match_index is not None:
                    match_position = int(outcome.match_index) - 1
                    if not 0 <= match_position < candidate_count:
                        status = "failed_open"
                        gate_reason = "semantic_dedupe_failed_open"
                        error_category = "invalid_response"
                        reason = "去重候选引用无效，为避免漏报已安全放行"
                    else:
                        matched_row_id = int(candidates[match_position]["id"])
                        update_type = outcome.update_type
                        claimed_material_update = bool(outcome.material_update)
                        if claimed_material_update:
                            validation = validate_semantic_update(
                                update_type,
                                current=self._decode_row(current),
                                candidate=candidates[match_position],
                            )
                            update_validated = validation.valid
                            update_rejection_reason = validation.rejection_reason
                            material_update = validation.valid
                        if claimed_material_update and update_validated:
                            status = "material_update"
                            gate_reason = "semantic_material_update"
                        elif claimed_material_update:
                            status = "suppressed_unverified_update"
                            gate_reason = "semantic_unverified_update_suppressed"
                            reason = "模型识别到同一事件，但所称更新缺少可验证状态变化，已按重复资讯抑制"
                            eligible = False
                        elif int(confidence or 0) < SEMANTIC_DEDUPE_CONFIDENCE_THRESHOLD:
                            status = "low_confidence_pass"
                            gate_reason = "semantic_low_confidence_pass"
                        else:
                            candidate = self.connection.execute(
                                """
                                SELECT id, ai_score, push_eligible
                                FROM messages WHERE id = ?
                                """,
                                (matched_row_id,),
                            ).fetchone()
                            has_delivery = self.connection.execute(
                                """
                                SELECT 1 FROM deliveries
                                WHERE message_row_id = ?
                                  AND state IN ('queued', 'processing', 'retry', 'succeeded')
                                LIMIT 1
                                """,
                                (matched_row_id,),
                            ).fetchone()
                            can_replace = bool(
                                allow_push
                                and candidate is not None
                                and has_delivery is None
                                and bool(candidate["push_eligible"])
                                and int(current["ai_score"] or 0)
                                > int(candidate["ai_score"] or 0)
                            )
                            if can_replace:
                                self.connection.execute(
                                    """
                                    UPDATE messages
                                    SET push_eligible = 0, push_ready_at = NULL,
                                        push_gate_reason = 'semantic_superseded',
                                        semantic_dedupe_status = 'superseded',
                                        semantic_dedupe_matched_message_id = ?,
                                        semantic_dedupe_checked_at = ?
                                    WHERE id = ? AND push_eligible = 1
                                    """,
                                    (row_id, timestamp, matched_row_id),
                                )
                                status = "representative_replaced"
                                gate_reason = "eligible_better_representative"
                            else:
                                status = "suppressed"
                                gate_reason = "semantic_duplicate_suppressed"
                                eligible = False
                else:
                    status = "unique"
                    gate_reason = "eligible_unique"

            preparation_required = status not in {
                "suppressed",
                "suppressed_unverified_update",
            }
            if preparation_required:
                gate_reason = (
                    "manual_notification_prepare_pending"
                    if not allow_push
                    else "notification_prepare_pending"
                )
            elif not allow_push:
                gate_reason = "manual_reanalysis"
            ready_at = None
            prepare_status = (
                "pending" if preparation_required else "not_required_semantic_duplicate"
            )
            stored_matched_row_id = (
                None if status == "representative_replaced" else matched_row_id
            )
            self.connection.execute(
                """
                UPDATE messages
                SET semantic_dedupe_status = ?, semantic_dedupe_model = ?,
                    semantic_dedupe_effort = ?,
                    semantic_dedupe_confidence = ?, semantic_dedupe_reason = ?,
                    semantic_dedupe_response_text = ?,
                    semantic_dedupe_matched_message_id = ?,
                    semantic_dedupe_material_update = ?,
                    semantic_dedupe_update_type = ?,
                    semantic_dedupe_update_validated = ?,
                    semantic_dedupe_update_rejection_reason = ?,
                    semantic_dedupe_checked_at = ?,
                    semantic_dedupe_error_category = ?,
                    semantic_dedupe_candidate_count = ?,
                    push_eligible = ?, push_ready_at = ?, push_gate_reason = ?,
                    notification_prepare_status = ?,
                    notification_prepare_model = NULL,
                    notification_prepare_effort = NULL,
                    notification_title = NULL, notification_body = NULL,
                    notification_prepare_response_text = NULL,
                    notification_prepare_checked_at = NULL,
                    notification_prepare_error_category = NULL
                WHERE id = ? AND semantic_dedupe_status = 'checking'
                """,
                (
                    status,
                    model,
                    outcome.effort if outcome is not None else None,
                    confidence,
                    reason[:240],
                    response_text,
                    stored_matched_row_id,
                    int(material_update),
                    update_type,
                    int(update_validated),
                    update_rejection_reason[:240]
                    if update_rejection_reason
                    else None,
                    timestamp,
                    error_category,
                    candidate_count,
                    int(eligible),
                    ready_at,
                    gate_reason,
                    prepare_status,
                    row_id,
                ),
            )
            self.connection.commit()
            return status
        except Exception:
            self.connection.rollback()
            raise

    def begin_notification_preparation(
        self,
        row_id: int,
        *,
        model: str,
        effort: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        """Claim one persisted notification preparation without duplicating calls."""
        timestamp = to_iso(now)
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET notification_prepare_status = 'preparing',
                    notification_prepare_model = ?,
                    notification_prepare_effort = ?,
                    notification_prepare_error_category = NULL,
                    push_eligible = 0, push_ready_at = NULL,
                    push_gate_reason = 'notification_preparing'
                WHERE id = ?
                  AND ai_status = 'success'
                  AND (content_kind IN ('news', 'community_signal', 'benefit_deal')
                       OR (content_kind IS NULL AND ai_category = 'external_information'))
                  AND ai_score IS NOT NULL AND ai_score >= ?
                  AND semantic_dedupe_status IN (
                      'unique', 'unique_no_candidates', 'material_update',
                      'failed_open', 'low_confidence_pass',
                      'representative_replaced'
                  )
                  AND notification_prepare_status = 'pending'
                """,
                (model, effort, row_id, PUSH_SCORE_THRESHOLD),
            )
        if cursor.rowcount != 1:
            return None
        return self.get_message_by_id(row_id)

    def complete_notification_preparation(
        self,
        row_id: int,
        *,
        outcome: NotificationPreparationOutcome,
        fallback_title: str,
        fallback_body: str,
        now: datetime,
        allow_push: bool,
    ) -> str:
        """Freeze a model result or deterministic fallback before any delivery exists."""
        timestamp = to_iso(now)
        success = (
            outcome.status == "success"
            and bool(outcome.title)
            and bool(outcome.body)
        )
        status = "success" if success else "failed_fallback"
        title = str(outcome.title if success else fallback_title)
        body = str(outcome.body if success else fallback_body)
        error_category = None if success else (outcome.error_category or "internal_error")
        eligible = bool(allow_push)
        gate_reason = (
            "manual_reanalysis"
            if not allow_push
            else (
                "eligible_notification_prepared"
                if success
                else "eligible_notification_fallback"
            )
        )
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET ai_status = 'success', notification_prepare_status = ?,
                    notification_prepare_model = ?,
                    notification_prepare_effort = ?,
                    notification_title = ?, notification_body = ?,
                    notification_prepare_response_text = ?,
                    notification_prepare_checked_at = ?,
                    notification_prepare_error_category = ?,
                    push_eligible = ?, push_ready_at = ?, push_gate_reason = ?
                WHERE id = ? AND notification_prepare_status = 'preparing'
                """,
                (
                    status,
                    outcome.model,
                    outcome.effort,
                    title,
                    body,
                    outcome.response_text,
                    timestamp,
                    error_category,
                    int(eligible),
                    timestamp if eligible else None,
                    gate_reason,
                    row_id,
                ),
            )
        if cursor.rowcount != 1:
            current = self.get_message_by_id(row_id)
            return str(current.get("notification_prepare_status")) if current else "missing"
        return status

    def resume_prepared_notification(self, row_id: int, *, now: datetime) -> bool:
        """Resume the tiny crash window after preparation but before job completion."""
        timestamp = to_iso(now)
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET ai_status = 'success', push_eligible = 1,
                    push_ready_at = COALESCE(push_ready_at, ?),
                    push_gate_reason = CASE notification_prepare_status
                        WHEN 'success' THEN 'eligible_notification_prepared'
                        ELSE 'eligible_notification_fallback'
                    END
                WHERE id = ?
                  AND notification_prepare_status IN ('success', 'failed_fallback')
                  AND (content_kind IN ('news', 'community_signal', 'benefit_deal')
                       OR (content_kind IS NULL AND ai_category = 'external_information'))
                  AND ai_score IS NOT NULL AND ai_score >= ?
                  AND semantic_dedupe_status IN (
                      'unique', 'unique_no_candidates', 'material_update',
                      'failed_open', 'low_confidence_pass',
                      'representative_replaced'
                  )
                """,
                (timestamp, row_id, PUSH_SCORE_THRESHOLD),
            )
        return cursor.rowcount == 1

    def recent_llm_context(
        self,
        row_id: int,
        *,
        limit: int = RECENT_CONTEXT_LIMIT,
        character_limit: int = RECENT_CONTEXT_CHAR_LIMIT,
    ) -> tuple[dict[str, str], ...]:
        target = self.connection.execute(
            "SELECT id, chat_id, message_id, sent_at, text FROM messages WHERE id = ?",
            (row_id,),
        ).fetchone()
        if target is None:
            raise ValueError("消息不存在")
        rows = self.connection.execute(
            """
            SELECT id, message_id, sent_at, created_at, text, is_service_message, ai_status,
                   ai_category, prefilter_status
            FROM messages
            WHERE chat_id = ?
              AND (
                    sent_at < ?
                 OR (sent_at = ? AND message_id < ?)
                 OR (sent_at = ? AND message_id = ? AND id < ?)
              )
              AND is_service_message = 0
              AND prefilter_status = 'passed'
              AND ai_status = 'success'
              AND ai_category = 'external_information'
            ORDER BY sent_at DESC, message_id DESC, id DESC
            LIMIT ?
            """,
            (
                int(target["chat_id"]),
                str(target["sent_at"]),
                str(target["sent_at"]),
                int(target["message_id"]),
                str(target["sent_at"]),
                int(target["message_id"]),
                int(target["id"]),
                RECENT_CONTEXT_SCAN_LIMIT,
            ),
        ).fetchall()
        runtime = self.get_runtime_config()
        protected_keywords = (
            tuple(runtime.get("important_keywords") or ()) if runtime else ()
        )
        try:
            target_time = datetime.fromisoformat(
                str(target["sent_at"]).replace("Z", "+00:00")
            )
            if target_time.tzinfo is None:
                target_time = target_time.replace(tzinfo=timezone.utc)
            relevant_since = target_time - timedelta(hours=RECENT_CONTEXT_RELEVANCE_HOURS)
        except ValueError:
            relevant_since = datetime.min.replace(tzinfo=timezone.utc)

        def within_relevance_window(candidate: sqlite3.Row) -> bool:
            try:
                value = datetime.fromisoformat(
                    str(candidate["sent_at"]).replace("Z", "+00:00")
                )
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
            except ValueError:
                return False
            return value >= relevant_since

        return build_recent_context(
            tuple(dict(candidate) for candidate in rows),
            current_text=str(target["text"] or ""),
            relevant_rows_newest_first=tuple(
                dict(candidate) for candidate in rows if within_relevance_window(candidate)
            ),
            limit=limit,
            character_limit=character_limit,
            protected_keywords=protected_keywords,
        )

    def record_prefilter_result(
        self,
        row_id: int,
        *,
        result: PrefilterResult,
        now: datetime,
    ) -> None:
        if result.filtered:
            if not result.reason_code or not result.reason:
                raise ValueError("过滤结果缺少稳定原因")
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE messages
                    SET base_score = local_score, score = local_score + reply_bonus,
                        prefilter_status = 'filtered', prefilter_reason_code = ?,
                        prefilter_reason = ?, push_eligible = 0,
                        push_gate_reason = 'prefiltered', ai_status = 'prefiltered',
                        push_ready_at = NULL,
                        semantic_dedupe_status = 'not_required_prefilter',
                        semantic_dedupe_model = NULL,
                        semantic_dedupe_effort = NULL,
                        semantic_dedupe_confidence = NULL,
                        semantic_dedupe_reason = NULL,
                        semantic_dedupe_response_text = NULL,
                        semantic_dedupe_matched_message_id = NULL,
                        semantic_dedupe_material_update = 0,
                        semantic_dedupe_update_type = NULL,
                        semantic_dedupe_update_validated = 0,
                        semantic_dedupe_update_rejection_reason = NULL,
                        semantic_dedupe_checked_at = NULL,
                        semantic_dedupe_error_category = NULL,
                        semantic_dedupe_candidate_count = 0,
                        notification_prepare_status = 'not_required_prefilter',
                        notification_prepare_model = NULL,
                        notification_prepare_effort = NULL,
                        notification_title = NULL, notification_body = NULL,
                        notification_prepare_response_text = NULL,
                        notification_prepare_checked_at = NULL,
                        notification_prepare_error_category = NULL,
                        ai_model = NULL, ai_score = NULL, ai_summary = NULL,
                        ai_reason = NULL, ai_response_text = NULL,
                        ai_started_at = NULL, ai_completed_at = ?,
                        ai_error_category = NULL, ai_error_stage = NULL,
                        ai_category = NULL, ai_category_confidence = NULL,
                        ai_category_summary = NULL, ai_category_reason = NULL,
                        ai_category_response_text = NULL,
                        ai_classification_model = NULL,
                        ai_classification_effort = NULL,
                        ai_classification_batch_id = NULL,
                        ai_classification_call_id = NULL,
                        ai_scoring_effort = NULL,
                        content_kind = NULL, community_status = 'not_analyzed',
                        community_signal_type = NULL, community_confidence = NULL,
                        community_title = NULL, community_summary = NULL,
                        community_reason = NULL, community_response_text = NULL,
                        community_model = NULL, community_effort = NULL,
                        community_evidence_count = NULL,
                        community_checked_at = NULL, community_error_category = NULL,
                        benefit_status = 'not_analyzed', benefit_type = NULL,
                        benefit_confidence = NULL, benefit_title = NULL,
                        benefit_summary = NULL, benefit_reason = NULL,
                        benefit_response_text = NULL, benefit_model = NULL,
                        benefit_effort = NULL, benefit_checked_at = NULL,
                        benefit_error_category = NULL
                    WHERE id = ?
                    """,
                    (result.reason_code, result.reason, to_iso(now), row_id),
                )
            return
        with self.connection:
            self.connection.execute(
                """
                UPDATE messages
                SET prefilter_status = 'passed', prefilter_reason_code = NULL,
                    prefilter_reason = NULL, push_eligible = 0,
                    push_gate_reason = CASE
                        WHEN ai_status IN ('queued', 'pending', 'processing', 'retry')
                            THEN 'analysis_processing'
                        ELSE 'awaiting_analysis'
                    END
                WHERE id = ?
                """,
                (row_id,),
            )

    def mark_ai_unavailable(
        self,
        row_id: int,
        *,
        status: str,
        error_category: str | None,
    ) -> None:
        if status not in {"disabled", "unavailable"}:
            raise ValueError("无效的模型分析状态")
        with self.connection:
            self.connection.execute(
                """
                UPDATE messages
                SET base_score = local_score,
                    score = local_score + reply_bonus,
                    push_eligible = 0, push_gate_reason = ?,
                    push_ready_at = NULL,
                    semantic_dedupe_status = 'not_required_model',
                    semantic_dedupe_model = NULL,
                    semantic_dedupe_effort = NULL,
                    semantic_dedupe_confidence = NULL,
                    semantic_dedupe_reason = NULL,
                    semantic_dedupe_response_text = NULL,
                    semantic_dedupe_matched_message_id = NULL,
                    semantic_dedupe_material_update = 0,
                    semantic_dedupe_update_type = NULL,
                    semantic_dedupe_update_validated = 0,
                    semantic_dedupe_update_rejection_reason = NULL,
                    semantic_dedupe_checked_at = NULL,
                    semantic_dedupe_error_category = NULL,
                    semantic_dedupe_candidate_count = 0,
                    notification_prepare_status = 'not_required_model',
                    notification_prepare_model = NULL,
                    notification_prepare_effort = NULL,
                    notification_title = NULL, notification_body = NULL,
                    notification_prepare_response_text = NULL,
                    notification_prepare_checked_at = NULL,
                    notification_prepare_error_category = NULL,
                    ai_status = ?, ai_model = NULL, ai_score = NULL,
                    ai_summary = NULL, ai_reason = NULL,
                    ai_response_text = NULL, ai_started_at = NULL,
                    ai_completed_at = NULL, ai_error_category = ?,
                    ai_error_stage = NULL,
                    ai_category = NULL, ai_category_confidence = NULL,
                    ai_category_summary = NULL, ai_category_reason = NULL,
                    ai_category_response_text = NULL,
                    ai_classification_model = NULL,
                    ai_classification_effort = NULL,
                    ai_classification_batch_id = NULL,
                    ai_classification_call_id = NULL,
                    ai_scoring_effort = NULL,
                    content_kind = NULL, community_status = 'not_analyzed',
                    community_signal_type = NULL, community_confidence = NULL,
                    community_title = NULL, community_summary = NULL,
                    community_reason = NULL, community_response_text = NULL,
                    community_model = NULL, community_effort = NULL,
                    community_evidence_count = NULL,
                    community_checked_at = NULL, community_error_category = NULL,
                    benefit_status = 'not_analyzed', benefit_type = NULL,
                    benefit_confidence = NULL, benefit_title = NULL,
                    benefit_summary = NULL, benefit_reason = NULL,
                    benefit_response_text = NULL, benefit_model = NULL,
                    benefit_effort = NULL, benefit_checked_at = NULL,
                    benefit_error_category = NULL
                WHERE id = ?
                """,
                (
                    "model_disabled" if status == "disabled" else "model_unavailable",
                    status,
                    error_category,
                    row_id,
                ),
            )

    def begin_ai_analysis(
        self,
        row_id: int,
        *,
        model: str,
        classification_model: str = DEFAULT_CLASSIFICATION_MODEL,
        now: datetime,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        classification_reasoning_effort: str = DEFAULT_CLASSIFICATION_REASONING_EFFORT,
    ) -> bool:
        reasoning_effort = validate_reasoning_effort(reasoning_effort)
        classification_reasoning_effort = validate_reasoning_effort(
            classification_reasoning_effort
        )
        started_at = to_iso(now)
        stale_before = to_iso(now - timedelta(minutes=5))
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET ai_status = 'processing', ai_model = ?, ai_score = NULL,
                    ai_summary = NULL, ai_reason = NULL, ai_response_text = NULL,
                    ai_started_at = ?, ai_completed_at = NULL,
                    ai_error_category = NULL, ai_error_stage = NULL,
                    ai_category = NULL, ai_category_confidence = NULL,
                    ai_category_summary = NULL, ai_category_reason = NULL,
                    ai_category_response_text = NULL,
                    ai_classification_model = ?,
                    ai_classification_effort = ?,
                    ai_classification_batch_id = NULL,
                    ai_classification_call_id = NULL,
                    ai_scoring_effort = ?,
                    content_kind = NULL, community_status = 'not_analyzed',
                    community_signal_type = NULL, community_confidence = NULL,
                    community_title = NULL, community_summary = NULL,
                    community_reason = NULL, community_response_text = NULL,
                    community_model = NULL, community_effort = NULL,
                    community_evidence_count = NULL, community_checked_at = NULL,
                    community_error_category = NULL,
                    benefit_status = 'not_analyzed', benefit_type = NULL,
                    benefit_confidence = NULL, benefit_title = NULL,
                    benefit_summary = NULL, benefit_reason = NULL,
                    benefit_response_text = NULL, benefit_model = NULL,
                    benefit_effort = NULL, benefit_checked_at = NULL,
                    benefit_error_category = NULL,
                    push_eligible = 0, push_gate_reason = 'analysis_processing',
                    push_ready_at = NULL,
                    semantic_dedupe_status = 'awaiting_analysis',
                    semantic_dedupe_model = NULL,
                    semantic_dedupe_effort = NULL,
                    semantic_dedupe_confidence = NULL,
                    semantic_dedupe_reason = NULL,
                    semantic_dedupe_response_text = NULL,
                    semantic_dedupe_matched_message_id = NULL,
                    semantic_dedupe_material_update = 0,
                    semantic_dedupe_update_type = NULL,
                    semantic_dedupe_update_validated = 0,
                    semantic_dedupe_update_rejection_reason = NULL,
                    semantic_dedupe_checked_at = NULL,
                    semantic_dedupe_error_category = NULL,
                    semantic_dedupe_candidate_count = 0,
                    notification_prepare_status = 'awaiting_analysis',
                    notification_prepare_model = NULL,
                    notification_prepare_effort = NULL,
                    notification_title = NULL, notification_body = NULL,
                    notification_prepare_response_text = NULL,
                    notification_prepare_checked_at = NULL,
                    notification_prepare_error_category = NULL,
                    feedback_context_sample_count = 0,
                    feedback_context_summary = NULL,
                    feedback_context_json = NULL,
                    feedback_context_applied_at = NULL
                WHERE id = ?
                  AND (ai_status NOT IN ('pending', 'processing')
                       OR ai_started_at IS NULL OR ai_started_at < ?)
                """,
                (
                    model,
                    started_at,
                    classification_model,
                    classification_reasoning_effort,
                    reasoning_effort,
                    row_id,
                    stale_before,
                ),
            )
        return cursor.rowcount == 1

    def complete_non_information(
        self,
        row_id: int,
        *,
        now: datetime,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE messages
                SET base_score = local_score, score = local_score + reply_bonus,
                    ai_status = 'filtered_non_information', ai_score = NULL,
                    ai_summary = NULL, ai_reason = NULL,
                    ai_response_text = NULL, ai_scoring_effort = NULL,
                    content_kind = NULL, community_status = 'not_applicable',
                    community_signal_type = NULL, community_confidence = NULL,
                    community_title = NULL, community_summary = NULL,
                    community_reason = NULL, community_response_text = NULL,
                    community_model = NULL, community_effort = NULL,
                    community_evidence_count = NULL, community_checked_at = NULL,
                    community_error_category = NULL,
                    benefit_status = 'not_applicable', benefit_type = NULL,
                    benefit_confidence = NULL, benefit_title = NULL,
                    benefit_summary = NULL, benefit_reason = NULL,
                    benefit_response_text = NULL, benefit_model = NULL,
                    benefit_effort = NULL, benefit_checked_at = NULL,
                    benefit_error_category = NULL,
                    ai_completed_at = ?, ai_error_category = NULL,
                    ai_error_stage = NULL, push_eligible = 0,
                    push_gate_reason = 'non_information', push_ready_at = NULL,
                    semantic_dedupe_status = 'not_required_non_information',
                    semantic_dedupe_model = NULL,
                    semantic_dedupe_effort = NULL,
                    semantic_dedupe_confidence = NULL,
                    semantic_dedupe_reason = NULL,
                    semantic_dedupe_response_text = NULL,
                    semantic_dedupe_matched_message_id = NULL,
                    semantic_dedupe_material_update = 0,
                    semantic_dedupe_update_type = NULL,
                    semantic_dedupe_update_validated = 0,
                    semantic_dedupe_update_rejection_reason = NULL,
                    semantic_dedupe_checked_at = NULL,
                    semantic_dedupe_error_category = NULL,
                    semantic_dedupe_candidate_count = 0,
                    notification_prepare_status = 'not_required_non_information',
                    notification_prepare_model = NULL,
                    notification_prepare_effort = NULL,
                    notification_title = NULL, notification_body = NULL,
                    notification_prepare_response_text = NULL,
                    notification_prepare_checked_at = NULL,
                    notification_prepare_error_category = NULL
                WHERE id = ? AND ai_status = 'processing'
                  AND ai_category IS NOT NULL
                  AND ai_category != 'external_information'
                """,
                (to_iso(now), row_id),
            )

    def save_ai_classification(
        self,
        row_id: int,
        *,
        classification: ClassificationOutcome,
    ) -> bool:
        if classification.status != "success" or classification.category is None:
            raise ValueError("只能保存成功且完整的分类结果")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET ai_category = ?, ai_category_confidence = ?,
                    ai_category_summary = ?, ai_category_reason = ?,
                    ai_category_response_text = ?, ai_classification_model = ?,
                    ai_classification_effort = ?
                WHERE id = ? AND ai_status = 'processing'
                """,
                (
                    classification.category,
                    classification.confidence,
                    classification.summary,
                    classification.reason,
                    classification.response_text,
                    classification.model,
                    classification.effort,
                    row_id,
                ),
            )
        return cursor.rowcount == 1

    def benefit_context(self, row_id: int) -> tuple[dict[str, str], ...]:
        """Bounded same-chat evidence, including already received later replies.

        Do not filter on classification: a short correction may be discussion.
        Sender IDs stay local; neither display names nor repetitions prove trust.
        """
        target = self.connection.execute(
            "SELECT * FROM messages WHERE id = ?", (row_id,)
        ).fetchone()
        if target is None:
            return ()
        rows = self.connection.execute(
            """SELECT sent_at, text, sender_id, message_id, reply_to_message_id,
                      thread_root_id
               FROM messages
               WHERE chat_id = ? AND id != ? AND is_service_message = 0
                 AND ABS(julianday(sent_at) - julianday(?)) <= (15.0 / 1440)
               ORDER BY (reply_to_message_id = ? OR message_id = ?
                         OR thread_root_id = ?) DESC,
                        ABS(julianday(sent_at) - julianday(?)), id
               LIMIT 24""",
            (target["chat_id"], row_id, target["sent_at"], target["message_id"],
             target["reply_to_message_id"], target["thread_root_id"], target["sent_at"]),
        ).fetchall()
        result = []
        remaining = 12000
        speakers: dict[int, int] = {}
        for row in rows:
            if remaining <= 0:
                break
            speaker = row["sender_id"]
            if speaker is None:
                label = "身份未知，不能算独立佐证"
            elif speaker == target["sender_id"]:
                label = "原发布者，不能算独立佐证"
            else:
                label = f"其他参与者{speakers.setdefault(speaker, len(speakers) + 1)}"
            reply = row["reply_to_message_id"] == target["message_id"]
            relation = "直接回复原消息" if reply else "邻近消息，必须核对是否同一话题"
            value = f"[{label}；{relation}] {row['text']}"[:min(1500, remaining)]
            result.append({"time": str(row["sent_at"]), "text": value})
            remaining -= len(value)
        return tuple(result)

    def recent_community_context(
        self,
        row_id: int,
    ) -> tuple[dict[str, str], ...]:
        """Return thread-first, recent, then relevant prior same-chat discussion."""
        context, _ = self.community_analysis_context(row_id)
        return context

    def community_analysis_context(
        self,
        row_id: int,
    ) -> tuple[tuple[dict[str, str], ...], CommunityGateEvidence]:
        """Return privacy-bounded context plus local corroboration aggregates."""
        target = self.connection.execute(
            """
            SELECT id, chat_id, message_id, sent_at, text, thread_root_id,
                   sender_id, sender_name
            FROM messages WHERE id = ?
            """,
            (row_id,),
        ).fetchone()
        if target is None:
            return (), CommunityGateEvidence()
        try:
            target_time = datetime.fromisoformat(
                str(target["sent_at"]).replace("Z", "+00:00")
            )
            if target_time.tzinfo is None:
                target_time = target_time.replace(tzinfo=timezone.utc)
        except ValueError:
            return (), CommunityGateEvidence()
        since = to_iso(
            target_time - timedelta(hours=COMMUNITY_CONTEXT_RELEVANCE_HOURS)
        )
        rows = self.connection.execute(
            """
            SELECT id, message_id, sent_at, created_at, text, thread_root_id,
                   sender_id, sender_name,
                   is_service_message, prefilter_status, ai_category,
                   community_status
            FROM messages
            WHERE chat_id = ? AND sent_at >= ?
              AND (
                    sent_at < ?
                 OR (sent_at = ? AND message_id < ?)
                 OR (sent_at = ? AND message_id = ? AND id < ?)
              )
              AND is_service_message = 0
              AND prefilter_status = 'passed'
              AND ai_category = 'discussion'
              AND community_status IN ('filtered', 'valuable')
            ORDER BY sent_at DESC, message_id DESC, id DESC
            LIMIT ?
            """,
            (
                int(target["chat_id"]),
                since,
                str(target["sent_at"]),
                str(target["sent_at"]),
                int(target["message_id"]),
                str(target["sent_at"]),
                int(target["message_id"]),
                int(target["id"]),
                COMMUNITY_CONTEXT_SCAN_LIMIT,
            ),
        ).fetchall()
        runtime = self.get_runtime_config()
        protected_keywords = (
            tuple(runtime.get("important_keywords") or ()) if runtime else ()
        )
        candidates = tuple(dict(candidate) for candidate in rows)
        target_value = dict(target)
        context = build_community_context(
            candidates,
            current_text=str(target["text"] or ""),
            target_time=target_time,
            target_thread_root_id=int(target["thread_root_id"]),
            protected_keywords=protected_keywords,
        )
        evidence = build_community_gate_evidence(
            candidates,
            current_row=target_value,
            target_time=target_time,
            protected_keywords=protected_keywords,
        )
        return context, evidence

    def complete_community_analysis(
        self,
        row_id: int,
        *,
        outcome: CommunityInsightOutcome,
        now: datetime,
        allow_push: bool = True,
    ) -> None:
        """Persist the optional discussion gate; failures and weak chat fail closed."""
        completed_at = to_iso(now)
        used_model = None if outcome.model == LOCAL_GATE_MODEL else outcome.model
        used_effort = None if outcome.model == LOCAL_GATE_MODEL else outcome.effort
        succeeded = outcome.status == "success"
        valuable = bool(
            succeeded
            and outcome.valuable
            and outcome.score is not None
            and outcome.confidence is not None
            and int(outcome.confidence) >= COMMUNITY_CONFIDENCE_THRESHOLD
        )
        score = int(outcome.score or 0)
        requires_dedupe = valuable and score >= PUSH_SCORE_THRESHOLD
        if not succeeded:
            ai_status = "error"
            community_status = "error"
            gate_reason = "community_analysis_error"
            semantic_status = "not_required_community_error"
            notification_status = "not_required_community_error"
            error_category = outcome.error_category or "invalid_response"
        elif not outcome.valuable:
            ai_status = "filtered_non_information"
            community_status = "filtered"
            gate_reason = "community_not_valuable"
            semantic_status = "not_required_community_filtered"
            notification_status = "not_required_community_filtered"
            error_category = None
        elif not valuable:
            ai_status = "filtered_non_information"
            community_status = "filtered_low_confidence"
            gate_reason = "community_low_confidence"
            semantic_status = "not_required_community_filtered"
            notification_status = "not_required_community_filtered"
            error_category = None
        else:
            ai_status = "success"
            community_status = "valuable"
            gate_reason = (
                "semantic_dedupe_pending"
                if requires_dedupe
                else ("manual_reanalysis" if not allow_push else "below_push_threshold")
            )
            semantic_status = "pending" if requires_dedupe else "not_required_below_threshold"
            notification_status = (
                "awaiting_semantic_dedupe"
                if requires_dedupe
                else "not_required_below_threshold"
            )
            error_category = None
        with self.connection:
            self.connection.execute(
                """
                UPDATE messages
                SET base_score = CASE WHEN ? THEN ? ELSE local_score END,
                    score = CASE WHEN ? THEN ? ELSE local_score + reply_bonus END,
                    ai_status = ?, ai_model = ?,
                    ai_score = CASE WHEN ? THEN ? ELSE NULL END,
                    ai_summary = CASE WHEN ? THEN ? ELSE NULL END,
                    ai_reason = CASE WHEN ? THEN ? ELSE NULL END,
                    ai_response_text = ?, ai_scoring_effort = ?,
                    ai_completed_at = ?, ai_error_category = ?,
                    ai_error_stage = CASE WHEN ? THEN 'community' ELSE NULL END,
                    content_kind = CASE WHEN ? THEN 'community_signal' ELSE NULL END,
                    community_status = ?, community_signal_type = ?,
                    community_confidence = ?, community_title = ?,
                    community_summary = ?, community_reason = ?,
                    community_response_text = ?, community_model = ?,
                    community_effort = ?, community_evidence_count = ?,
                    community_checked_at = ?, community_error_category = ?,
                    benefit_status = 'not_applicable', benefit_type = NULL,
                    benefit_confidence = NULL, benefit_title = NULL,
                    benefit_summary = NULL, benefit_reason = NULL,
                    benefit_response_text = NULL, benefit_model = NULL,
                    benefit_effort = NULL, benefit_checked_at = NULL,
                    benefit_error_category = NULL,
                    push_eligible = 0, push_ready_at = NULL, push_gate_reason = ?,
                    semantic_dedupe_status = ?, notification_prepare_status = ?,
                    semantic_dedupe_model = NULL, semantic_dedupe_effort = NULL,
                    semantic_dedupe_confidence = NULL, semantic_dedupe_reason = NULL,
                    semantic_dedupe_response_text = NULL,
                    semantic_dedupe_matched_message_id = NULL,
                    semantic_dedupe_material_update = 0,
                    semantic_dedupe_update_type = NULL,
                    semantic_dedupe_update_validated = 0,
                    semantic_dedupe_update_rejection_reason = NULL,
                    semantic_dedupe_checked_at = NULL,
                    semantic_dedupe_error_category = NULL,
                    semantic_dedupe_candidate_count = 0,
                    notification_prepare_model = NULL,
                    notification_prepare_effort = NULL,
                    notification_title = NULL, notification_body = NULL,
                    notification_prepare_response_text = NULL,
                    notification_prepare_checked_at = NULL,
                    notification_prepare_error_category = NULL
                WHERE id = ? AND ai_status = 'processing' AND ai_category = 'discussion'
                """,
                (
                    int(valuable), score,
                    int(valuable), score,
                    ai_status, used_model,
                    int(valuable), score,
                    int(valuable), outcome.title,
                    int(valuable), outcome.reason,
                    outcome.response_text, used_effort,
                    completed_at, error_category,
                    int(not succeeded),
                    int(valuable),
                    community_status, outcome.signal_type,
                    outcome.confidence, outcome.title,
                    outcome.summary, outcome.reason,
                    outcome.response_text, used_model,
                    used_effort, outcome.evidence_count,
                    completed_at, error_category,
                    gate_reason, semantic_status, notification_status,
                    row_id,
                ),
            )

    def complete_benefit_analysis(
        self,
        row_id: int,
        *,
        outcome: BenefitDealOutcome,
        now: datetime,
        allow_push: bool = True,
    ) -> None:
        """Persist the high-confidence benefit gate; unsafe or weak ads fail closed."""
        completed_at = to_iso(now)
        used_model = None if outcome.model == LOCAL_GATE_MODEL else outcome.model
        used_effort = None if outcome.model == LOCAL_GATE_MODEL else outcome.effort
        succeeded = outcome.status == "success"
        valuable = bool(
            succeeded
            and outcome.valuable
            and outcome.score is not None
            and outcome.confidence is not None
            and int(outcome.confidence) >= BENEFIT_CONFIDENCE_THRESHOLD
        )
        score = int(outcome.score or 0)
        requires_dedupe = valuable and score >= PUSH_SCORE_THRESHOLD
        if not succeeded:
            ai_status = "error"
            benefit_status = "error"
            gate_reason = "benefit_analysis_error"
            semantic_status = "not_required_benefit_error"
            notification_status = "not_required_benefit_error"
            error_category = outcome.error_category or "invalid_response"
        elif not outcome.valuable:
            ai_status = "filtered_non_information"
            benefit_status = "filtered"
            gate_reason = "benefit_not_valuable"
            semantic_status = "not_required_benefit_filtered"
            notification_status = "not_required_benefit_filtered"
            error_category = None
        elif not valuable:
            ai_status = "filtered_non_information"
            benefit_status = "filtered_low_confidence"
            gate_reason = "benefit_low_confidence"
            semantic_status = "not_required_benefit_filtered"
            notification_status = "not_required_benefit_filtered"
            error_category = None
        else:
            ai_status = "success"
            benefit_status = "valuable"
            gate_reason = (
                "semantic_dedupe_pending"
                if requires_dedupe
                else ("manual_reanalysis" if not allow_push else "below_push_threshold")
            )
            semantic_status = "pending" if requires_dedupe else "not_required_below_threshold"
            notification_status = (
                "awaiting_semantic_dedupe" if requires_dedupe else "not_required_below_threshold"
            )
            error_category = None
        with self.connection:
            self.connection.execute(
                """
                UPDATE messages
                SET base_score = CASE WHEN ? THEN ? ELSE local_score END,
                    score = CASE WHEN ? THEN ? ELSE local_score + reply_bonus END,
                    ai_status = ?, ai_model = ?,
                    ai_score = CASE WHEN ? THEN ? ELSE NULL END,
                    ai_summary = CASE WHEN ? THEN ? ELSE NULL END,
                    ai_reason = CASE WHEN ? THEN ? ELSE NULL END,
                    ai_response_text = ?, ai_scoring_effort = ?,
                    ai_completed_at = ?, ai_error_category = ?,
                    ai_error_stage = CASE WHEN ? THEN 'benefit' ELSE NULL END,
                    content_kind = CASE WHEN ? THEN 'benefit_deal' ELSE NULL END,
                    community_status = 'not_applicable',
                    community_signal_type = NULL, community_confidence = NULL,
                    community_title = NULL, community_summary = NULL,
                    community_reason = NULL, community_response_text = NULL,
                    community_model = NULL, community_effort = NULL,
                    community_evidence_count = NULL, community_checked_at = NULL,
                    community_error_category = NULL,
                    benefit_status = ?, benefit_type = ?, benefit_confidence = ?,
                    benefit_title = ?, benefit_summary = ?, benefit_reason = ?,
                    benefit_response_text = ?, benefit_model = ?, benefit_effort = ?,
                    benefit_checked_at = ?, benefit_error_category = ?,
                    push_eligible = 0, push_ready_at = NULL, push_gate_reason = ?,
                    semantic_dedupe_status = ?, notification_prepare_status = ?,
                    semantic_dedupe_model = NULL, semantic_dedupe_effort = NULL,
                    semantic_dedupe_confidence = NULL, semantic_dedupe_reason = NULL,
                    semantic_dedupe_response_text = NULL,
                    semantic_dedupe_matched_message_id = NULL,
                    semantic_dedupe_material_update = 0,
                    semantic_dedupe_update_type = NULL,
                    semantic_dedupe_update_validated = 0,
                    semantic_dedupe_update_rejection_reason = NULL,
                    semantic_dedupe_checked_at = NULL,
                    semantic_dedupe_error_category = NULL,
                    semantic_dedupe_candidate_count = 0,
                    notification_prepare_model = NULL,
                    notification_prepare_effort = NULL,
                    notification_title = NULL, notification_body = NULL,
                    notification_prepare_response_text = NULL,
                    notification_prepare_checked_at = NULL,
                    notification_prepare_error_category = NULL
                WHERE id = ? AND ai_status = 'processing'
                  AND ai_category = 'promotion_spam'
                """,
                (
                    int(valuable), score,
                    int(valuable), score,
                    ai_status, used_model,
                    int(valuable), score,
                    int(valuable), outcome.title,
                    int(valuable), outcome.reason,
                    outcome.response_text, used_effort,
                    completed_at, error_category,
                    int(not succeeded), int(valuable),
                    benefit_status, outcome.benefit_type, outcome.confidence,
                    outcome.title, outcome.summary, outcome.reason,
                    outcome.response_text, used_model, used_effort,
                    completed_at, error_category,
                    gate_reason, semantic_status, notification_status,
                    row_id,
                ),
            )

    def complete_ai_analysis(
        self,
        row_id: int,
        *,
        outcome: AnalysisOutcome,
        now: datetime,
        allow_push: bool = True,
    ) -> None:
        completed_at = to_iso(now)
        if outcome.status == "success" and outcome.score is not None:
            effective_score = int(outcome.score)
            requires_dedupe = bool(
                outcome.classification_category == "external_information"
                and effective_score >= PUSH_SCORE_THRESHOLD
            )
            values = (
                effective_score,
                effective_score,
                outcome.model,
                effective_score,
                outcome.summary,
                outcome.reason,
                outcome.response_text,
                outcome.classification_category,
                outcome.classification_confidence,
                outcome.classification_summary,
                outcome.classification_reason,
                outcome.classification_response_text,
                outcome.classification_model,
                outcome.classification_effort,
                outcome.scoring_effort,
                completed_at,
                "pending" if requires_dedupe else "not_required_below_threshold",
                "awaiting_semantic_dedupe"
                if requires_dedupe
                else "not_required_below_threshold",
                "semantic_dedupe_pending"
                if requires_dedupe
                else (
                    "manual_reanalysis"
                    if not allow_push
                    else "below_push_threshold"
                ),
                row_id,
            )
            sql = """
                UPDATE messages
                SET base_score = ?, score = ?,
                    ai_status = 'success', ai_model = ?, ai_score = ?,
                    ai_summary = ?, ai_reason = ?, ai_response_text = ?,
                    ai_category = ?, ai_category_confidence = ?,
                    ai_category_summary = ?, ai_category_reason = ?,
                    ai_category_response_text = ?, ai_classification_model = ?,
                    ai_classification_effort = ?,
                    ai_scoring_effort = ?, ai_completed_at = ?,
                    content_kind = 'news', community_status = 'not_applicable',
                    community_signal_type = NULL, community_confidence = NULL,
                    community_title = NULL, community_summary = NULL,
                    community_reason = NULL, community_response_text = NULL,
                    community_model = NULL, community_effort = NULL,
                    community_evidence_count = NULL, community_checked_at = NULL,
                    community_error_category = NULL,
                    benefit_status = 'not_applicable', benefit_type = NULL,
                    benefit_confidence = NULL, benefit_title = NULL,
                    benefit_summary = NULL, benefit_reason = NULL,
                    benefit_response_text = NULL, benefit_model = NULL,
                    benefit_effort = NULL, benefit_checked_at = NULL,
                    benefit_error_category = NULL,
                    ai_error_category = NULL, ai_error_stage = NULL,
                    push_eligible = 0, semantic_dedupe_status = ?,
                    notification_prepare_status = ?,
                    push_gate_reason = ?, push_ready_at = NULL,
                    semantic_dedupe_model = NULL,
                    semantic_dedupe_effort = NULL,
                    semantic_dedupe_confidence = NULL,
                    semantic_dedupe_reason = NULL,
                    semantic_dedupe_response_text = NULL,
                    semantic_dedupe_matched_message_id = NULL,
                    semantic_dedupe_material_update = 0,
                    semantic_dedupe_update_type = NULL,
                    semantic_dedupe_update_validated = 0,
                    semantic_dedupe_update_rejection_reason = NULL,
                    semantic_dedupe_checked_at = NULL,
                    semantic_dedupe_error_category = NULL,
                    semantic_dedupe_candidate_count = 0,
                    notification_prepare_model = NULL,
                    notification_prepare_effort = NULL,
                    notification_title = NULL, notification_body = NULL,
                    notification_prepare_response_text = NULL,
                    notification_prepare_checked_at = NULL,
                    notification_prepare_error_category = NULL
                WHERE id = ? AND ai_status = 'processing'
            """
        else:
            values = (
                outcome.model,
                outcome.response_text,
                outcome.classification_category,
                outcome.classification_confidence,
                outcome.classification_summary,
                outcome.classification_reason,
                outcome.classification_response_text,
                outcome.classification_model,
                outcome.classification_effort,
                outcome.scoring_effort if outcome.error_stage == "scoring" else None,
                completed_at,
                outcome.error_category or "invalid_response",
                outcome.error_stage,
                "classification_error"
                if outcome.error_stage == "classification"
                else "scoring_error",
                row_id,
            )
            sql = """
                UPDATE messages
                SET base_score = local_score, score = local_score + reply_bonus,
                    ai_status = 'error', ai_model = ?, ai_score = NULL,
                    ai_summary = NULL, ai_reason = NULL, ai_response_text = ?,
                    ai_category = ?, ai_category_confidence = ?,
                    ai_category_summary = ?, ai_category_reason = ?,
                    ai_category_response_text = ?, ai_classification_model = ?,
                    ai_classification_effort = ?,
                    ai_scoring_effort = ?, ai_completed_at = ?,
                    content_kind = NULL, community_status = 'error',
                    ai_error_category = ?, ai_error_stage = ?,
                    push_eligible = 0, push_gate_reason = ?, push_ready_at = NULL,
                    semantic_dedupe_status = 'not_required_analysis_error',
                    semantic_dedupe_model = NULL,
                    semantic_dedupe_effort = NULL,
                    semantic_dedupe_confidence = NULL,
                    semantic_dedupe_reason = NULL,
                    semantic_dedupe_response_text = NULL,
                    semantic_dedupe_matched_message_id = NULL,
                    semantic_dedupe_material_update = 0,
                    semantic_dedupe_update_type = NULL,
                    semantic_dedupe_update_validated = 0,
                    semantic_dedupe_update_rejection_reason = NULL,
                    semantic_dedupe_checked_at = NULL,
                    semantic_dedupe_error_category = NULL,
                    semantic_dedupe_candidate_count = 0,
                    notification_prepare_status = 'not_required_analysis_error',
                    notification_prepare_model = NULL,
                    notification_prepare_effort = NULL,
                    notification_title = NULL, notification_body = NULL,
                    notification_prepare_response_text = NULL,
                    notification_prepare_checked_at = NULL,
                    notification_prepare_error_category = NULL
                WHERE id = ? AND ai_status = 'processing'
            """
        with self.connection:
            self.connection.execute(sql, values)

    def recover_pending_analyses(self, *, now: datetime) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET base_score = local_score, score = local_score + reply_bonus,
                    ai_status = 'error', ai_score = NULL, ai_summary = NULL,
                    ai_reason = NULL, ai_response_text = NULL,
                    ai_completed_at = ?, ai_error_category = 'interrupted',
                    ai_error_stage = CASE
                        WHEN ai_category IS NULL THEN 'classification'
                        ELSE 'scoring'
                    END,
                    ai_scoring_effort = CASE
                        WHEN ai_category IS NULL THEN NULL
                        ELSE ai_scoring_effort
                    END,
                    push_eligible = 0, push_gate_reason = 'analysis_interrupted'
                WHERE ai_status IN ('pending', 'processing')
                """,
                (to_iso(now),),
            )
        return max(0, cursor.rowcount)

    def count_recent_normalized(
        self,
        chat_id: int,
        normalized_text: str,
        since: datetime,
    ) -> int:
        if len(normalized_text) < 12:
            return 0
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM messages
            WHERE chat_id = ? AND normalized_text = ? AND created_at >= ?
            """,
            (chat_id, normalized_text, to_iso(since)),
        ).fetchone()
        return int(row["count"])

    def find_recent_terminal_duplicate(
        self,
        row_id: int,
        *,
        window_hours: int = ANALYSIS_DEDUPE_WINDOW_HOURS,
        protected_keywords: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Find an earlier, reliably processed exact duplicate in the same chat."""
        if window_hours <= 0:
            return None
        target = self.connection.execute(
            """
            SELECT id, chat_id, message_id, sent_at, text, normalized_text,
                   primary_url, sender_id, sender_name
            FROM messages WHERE id = ?
            """,
            (row_id,),
        ).fetchone()
        if target is None:
            return None
        normalized_length = len(str(target["normalized_text"] or ""))
        if normalized_length < ANALYSIS_RAPID_DEDUPE_MIN_NORMALIZED_CHARS:
            return None
        if normalized_length < ANALYSIS_DEDUPE_MIN_NORMALIZED_CHARS:
            text = str(target["text"] or "").strip()
            if (
                has_protected_signal(text, protected_keywords=protected_keywords)
                or community_has_incident_cue(text)
                or community_has_corroboration(text)
                or community_has_action_result(text)
            ):
                return None
            sender_name = str(target["sender_name"] or "").strip()
            previous = self.connection.execute(
                """
                SELECT previous.id, previous.message_id, previous.sent_at,
                       previous.ai_status, previous.ai_category,
                       previous.prefilter_reason_code
                FROM messages AS previous
                WHERE previous.chat_id = ?
                  AND previous.text = ?
                  AND (
                        (? IS NOT NULL AND previous.sender_id = ?)
                     OR (
                            ? IS NULL AND ? != ''
                        AND previous.sender_id IS NULL
                        AND previous.sender_name = ?
                     )
                  )
                  AND (
                        previous.sent_at < ?
                     OR (previous.sent_at = ? AND previous.message_id < ?)
                     OR (
                            previous.sent_at = ? AND previous.message_id = ?
                        AND previous.id < ?
                     )
                  )
                  AND julianday(previous.sent_at) >= julianday(?) - ?
                  AND (
                        previous.prefilter_status = 'filtered'
                     OR (
                            previous.ai_status = 'filtered_non_information'
                        AND previous.ai_category IN (
                            'internal_governance', 'internal_coordination',
                            'discussion', 'promotion_spam', 'unknown'
                        )
                     )
                  )
                ORDER BY previous.sent_at DESC, previous.message_id DESC,
                         previous.id DESC
                LIMIT 1
                """,
                (
                    int(target["chat_id"]),
                    str(target["text"]),
                    target["sender_id"],
                    target["sender_id"],
                    target["sender_id"],
                    sender_name,
                    sender_name,
                    str(target["sent_at"]),
                    str(target["sent_at"]),
                    int(target["message_id"]),
                    str(target["sent_at"]),
                    int(target["message_id"]),
                    int(target["id"]),
                    str(target["sent_at"]),
                    ANALYSIS_RAPID_DEDUPE_WINDOW_MINUTES / 1440.0,
                ),
            ).fetchone()
            if previous is None:
                return None
            result = dict(previous)
            result["dedupe_scope"] = "rapid_short"
            return result
        previous = self.connection.execute(
            """
            SELECT previous.id, previous.message_id, previous.sent_at,
                   previous.ai_status, previous.ai_category,
                   previous.prefilter_reason_code
            FROM messages AS previous
            WHERE previous.chat_id = ?
              AND previous.normalized_text = ?
              AND COALESCE(previous.primary_url, '') = COALESCE(?, '')
              AND (
                    previous.sent_at < ?
                 OR (previous.sent_at = ? AND previous.message_id < ?)
                 OR (
                        previous.sent_at = ? AND previous.message_id = ?
                    AND previous.id < ?
                 )
              )
              AND julianday(previous.sent_at) >= julianday(?) - ?
              AND (
                    previous.prefilter_status = 'filtered'
                 OR (
                        previous.ai_status = 'filtered_non_information'
                    AND previous.ai_category IN (
                        'internal_governance', 'internal_coordination',
                        'discussion', 'promotion_spam', 'unknown'
                    )
                 )
                 OR (
                        previous.ai_status = 'success'
                    AND (
                           previous.content_kind IN ('news', 'community_signal', 'benefit_deal')
                        OR (previous.content_kind IS NULL
                            AND previous.ai_category = 'external_information')
                    )
                    AND previous.ai_score IS NOT NULL
                 )
              )
            ORDER BY previous.sent_at DESC, previous.message_id DESC, previous.id DESC
            LIMIT 1
            """,
            (
                int(target["chat_id"]),
                str(target["normalized_text"]),
                target["primary_url"],
                str(target["sent_at"]),
                str(target["sent_at"]),
                int(target["message_id"]),
                str(target["sent_at"]),
                int(target["message_id"]),
                int(target["id"]),
                str(target["sent_at"]),
                window_hours / 24.0,
            ),
        ).fetchone()
        if previous is None:
            return None
        result = dict(previous)
        result["dedupe_scope"] = "long_exact"
        return result

    def thread_root_for(self, chat_id: int, reply_to_message_id: int | None) -> int | None:
        if reply_to_message_id is None:
            return None
        row = self.connection.execute(
            """
            SELECT thread_root_id FROM messages
            WHERE chat_id = ? AND message_id = ?
            """,
            (chat_id, reply_to_message_id),
        ).fetchone()
        return int(row["thread_root_id"]) if row else reply_to_message_id

    def record_reply_and_update_parent(
        self,
        *,
        chat_id: int,
        parent_message_id: int,
        sender_id: int | None,
        replied_at: datetime,
        window_minutes: int,
    ) -> dict[str, Any] | None:
        if sender_id is None:
            return None
        cutoff = replied_at - timedelta(minutes=window_minutes)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO replies(chat_id, parent_message_id, sender_id, replied_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, parent_message_id, sender_id)
                DO UPDATE SET replied_at = excluded.replied_at
                """,
                (chat_id, parent_message_id, sender_id, to_iso(replied_at)),
            )
            row = self.connection.execute(
                """
                SELECT COUNT(DISTINCT sender_id) AS count
                FROM replies
                WHERE chat_id = ? AND parent_message_id = ? AND replied_at >= ?
                """,
                (chat_id, parent_message_id, to_iso(cutoff)),
            ).fetchone()
            count = int(row["count"])
            bonus = reply_bonus(count)
            self.connection.execute(
                """
                UPDATE messages
                SET reply_count = ?, reply_bonus = ?,
                    score = CASE
                        WHEN ai_status = 'success' AND ai_score IS NOT NULL
                            THEN ai_score
                        ELSE base_score + ?
                    END
                WHERE chat_id = ? AND message_id = ?
                """,
                (count, bonus, bonus, chat_id, parent_message_id),
            )
        return self.get_message(chat_id, parent_message_id)

    def configured_push_channels(self) -> tuple[str, ...]:
        config = self.get_push_config(include_secrets=False)
        channels: list[str] = []
        telegram = config["telegram"]
        ntfy = config["ntfy"]
        if telegram["enabled"] and telegram["bot_token_configured"] and telegram["chat_id"]:
            channels.append("telegram")
        if (
            ntfy["enabled"]
            and ntfy["topic"]
            and ntfy["community_topic"]
            and ntfy["benefit_topic"]
        ):
            channels.append("ntfy")
        return tuple(channels)

    def enqueue_immediate_deliveries(
        self,
        chat_id: int,
        message_id: int,
        *,
        now: datetime,
    ) -> int:
        """Create one durable real-time delivery per enabled channel at 60+."""
        timestamp = to_iso(now)
        channels = self.configured_push_channels()
        if not channels:
            return 0
        with self.connection:
            row = self.connection.execute(
                """
                SELECT id FROM messages
                WHERE chat_id = ? AND message_id = ?
                  AND created_at > COALESCE(
                      (SELECT value FROM metadata WHERE key = 'recovery_delivery_cutoff_at'),
                      ''
                  )
                  AND push_eligible = 1
                  AND notification_prepare_status IN ('success', 'failed_fallback')
                  AND ai_status = 'success'
                  AND (content_kind IN ('news', 'community_signal', 'benefit_deal')
                       OR (content_kind IS NULL AND ai_category = 'external_information'))
                  AND ai_score IS NOT NULL AND ai_score >= ?
                """,
                (chat_id, message_id, PUSH_SCORE_THRESHOLD),
            ).fetchone()
            if row is None:
                return 0
            created = 0
            for channel in channels:
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO deliveries(
                        message_row_id, channel, delivery_type, batch_key,
                        state, available_at, created_at, updated_at
                    ) VALUES(?, ?, 'immediate', '', 'queued', ?, ?, ?)
                    """,
                    (int(row["id"]), channel, timestamp, timestamp, timestamp),
                )
                created += max(0, cursor.rowcount)
        return created

    def retire_pending_digest_deliveries(self) -> int:
        """Remove unsent legacy digest jobs after switching to real-time delivery."""
        with self.connection:
            cursor = self.connection.execute(
                """
                DELETE FROM deliveries
                WHERE delivery_type = 'digest'
                  AND state IN ('queued', 'processing', 'retry')
                """
            )
        return max(0, cursor.rowcount)

    def digest_window_candidates(self, cutoff: datetime) -> list[dict[str, Any]]:
        """Read newly eligible digest rows without consuming late analysis results."""
        cutoff_iso = to_iso(cutoff)
        rows = self.connection.execute(
            """
            SELECT * FROM messages
            WHERE push_ready_at IS NOT NULL AND push_ready_at <= ?
              AND created_at > COALESCE(
                  (SELECT value FROM metadata WHERE key = 'recovery_delivery_cutoff_at'),
                  ''
              )
              AND push_eligible = 1
              AND notification_prepare_status IN ('success', 'failed_fallback')
              AND ai_status = 'success'
              AND (content_kind IN ('news', 'community_signal', 'benefit_deal')
                   OR (content_kind IS NULL AND ai_category = 'external_information'))
              AND ai_score IS NOT NULL AND ai_score >= ?
              AND (analysis_queue_state = 'succeeded' OR analysis_queue_requested = 0)
              AND digest_considered_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM deliveries AS immediate
                  WHERE immediate.message_row_id = messages.id
                    AND immediate.delivery_type = 'immediate'
              )
            ORDER BY ai_score DESC, push_ready_at DESC, created_at DESC
            LIMIT 500
            """,
            (cutoff_iso, DIGEST_MIN_AI_SCORE),
        ).fetchall()
        return [self._decode_row(row) for row in rows]

    def enqueue_digest_deliveries(
        self,
        candidates: Iterable[dict[str, Any]],
        selected: Iterable[dict[str, Any]],
        *,
        cutoff: datetime,
    ) -> int:
        """Atomically advance the digest window and persist every channel unit."""
        candidate_rows = tuple(candidates)
        selected_rows = tuple(selected)
        timestamp = to_iso(cutoff)
        channels = self.configured_push_channels()
        ids = tuple(int(row["id"]) for row in selected_rows)
        # Do not consume a non-empty digest while every destination is disabled
        # or incomplete. Once a channel is configured, the same rows can be
        # persisted as independently retryable delivery units.
        if ids and not channels:
            return 0
        batch_material = ",".join(str(value) for value in sorted(ids))
        batch_key = hashlib.sha256(
            f"digest-v1:{timestamp}:{batch_material}".encode("ascii")
        ).hexdigest()[:40]
        created = 0
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('last_digest_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (timestamp,),
            )
            self.connection.executemany(
                "UPDATE messages SET digest_considered_at = ? WHERE id = ?",
                ((timestamp, int(row["id"])) for row in candidate_rows),
            )
            for row_id in ids:
                for channel in channels:
                    cursor = self.connection.execute(
                        """
                        INSERT OR IGNORE INTO deliveries(
                            message_row_id, channel, delivery_type, batch_key,
                            state, available_at, created_at, updated_at
                        ) VALUES(?, ?, 'digest', ?, 'queued', ?, ?, ?)
                        """,
                        (row_id, channel, batch_key, timestamp, timestamp, timestamp),
                    )
                    created += max(0, cursor.rowcount)
        return created

    def recover_delivery_jobs(self, *, now: datetime) -> int:
        timestamp = to_iso(now)
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE deliveries
                SET state = 'retry', available_at = ?, lease_until = NULL,
                    error_category = 'interrupted', updated_at = ?
                WHERE state = 'processing'
                """,
                (timestamp, timestamp),
            )
        return max(0, cursor.rowcount)

    def claim_next_delivery_unit(
        self,
        *,
        now: datetime,
        lease_seconds: int = 120,
    ) -> dict[str, Any] | None:
        timestamp = to_iso(now)
        lease_until = to_iso(now + timedelta(seconds=max(30, lease_seconds)))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                UPDATE deliveries
                SET state = 'retry', available_at = ?, lease_until = NULL,
                    error_category = 'interrupted', updated_at = ?
                WHERE state = 'processing' AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (timestamp, timestamp, timestamp),
            )
            first = self.connection.execute(
                """
                SELECT * FROM deliveries
                WHERE state IN ('queued', 'retry') AND available_at <= ?
                ORDER BY available_at, id LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if first is None:
                self.connection.commit()
                return None
            if (
                str(first["delivery_type"]) == "digest"
                and str(first["channel"]) == "telegram"
            ):
                rows = self.connection.execute(
                    """
                    SELECT * FROM deliveries
                    WHERE channel = ? AND delivery_type = 'digest' AND batch_key = ?
                      AND state IN ('queued', 'retry') AND available_at <= ?
                    ORDER BY id
                    """,
                    (str(first["channel"]), str(first["batch_key"]), timestamp),
                ).fetchall()
            else:
                rows = [first]
            ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            self.connection.execute(
                f"""
                UPDATE deliveries
                SET state = 'processing', attempts = attempts + 1,
                    lease_until = ?, error_category = NULL, updated_at = ?
                WHERE id IN ({placeholders})
                """,  # noqa: S608 -- placeholders are generated, not user input
                (lease_until, timestamp, *ids),
            )
            claimed = self.connection.execute(
                f"SELECT * FROM deliveries WHERE id IN ({placeholders}) ORDER BY id",  # noqa: S608
                ids,
            ).fetchall()
            message_ids = [int(row["message_row_id"]) for row in claimed]
            message_placeholders = ",".join("?" for _ in message_ids)
            messages = self.connection.execute(
                f"SELECT * FROM messages WHERE id IN ({message_placeholders})",  # noqa: S608
                message_ids,
            ).fetchall()
            decoded_by_id = {
                int(row["id"]): self._decode_row(row) for row in messages
            }
            self.connection.commit()
            return {
                "ids": ids,
                "channel": str(first["channel"]),
                "delivery_type": str(first["delivery_type"]),
                "batch_key": str(first["batch_key"]),
                "attempts": max(int(row["attempts"]) for row in claimed),
                "max_attempts": min(int(row["max_attempts"]) for row in claimed),
                "rows": [decoded_by_id[int(row["message_row_id"])] for row in claimed],
            }
        except Exception:
            self.connection.rollback()
            raise

    def retry_delivery_unit(
        self,
        delivery_ids: Iterable[int],
        *,
        now: datetime,
        delay_seconds: int,
        error_category: str,
    ) -> bool:
        ids = tuple(int(value) for value in delivery_ids)
        if not ids:
            return False
        available_at = to_iso(now + timedelta(seconds=max(1, delay_seconds)))
        timestamp = to_iso(now)
        placeholders = ",".join("?" for _ in ids)
        with self.connection:
            cursor = self.connection.execute(
                f"""
                UPDATE deliveries
                SET state = 'retry', available_at = ?, lease_until = NULL,
                    error_category = ?, updated_at = ?
                WHERE id IN ({placeholders}) AND state = 'processing'
                  AND attempts < max_attempts
                """,  # noqa: S608
                (available_at, error_category, timestamp, *ids),
            )
        return cursor.rowcount == len(ids)

    def finish_delivery_unit(
        self,
        delivery_ids: Iterable[int],
        *,
        now: datetime,
        succeeded: bool,
        error_category: str | None = None,
    ) -> None:
        ids = tuple(int(value) for value in delivery_ids)
        if not ids:
            return
        state = "succeeded" if succeeded else "failed"
        timestamp = to_iso(now)
        placeholders = ",".join("?" for _ in ids)
        with self.connection:
            affected = self.connection.execute(
                f"SELECT DISTINCT message_row_id, delivery_type FROM deliveries "
                f"WHERE id IN ({placeholders})",  # noqa: S608
                ids,
            ).fetchall()
            self.connection.execute(
                f"""
                UPDATE deliveries
                SET state = ?, lease_until = NULL, error_category = ?,
                    updated_at = ?, delivered_at = CASE WHEN ? THEN ? ELSE NULL END
                WHERE id IN ({placeholders}) AND state = 'processing'
                """,  # noqa: S608
                (state, error_category, timestamp, int(succeeded), timestamp, *ids),
            )
            for row in affected:
                message_row_id = int(row["message_row_id"])
                delivery_type = str(row["delivery_type"])
                incomplete = self.connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM deliveries
                    WHERE message_row_id = ? AND delivery_type = ? AND state != 'succeeded'
                    """,
                    (message_row_id, delivery_type),
                ).fetchone()
                if int(incomplete["count"]) == 0:
                    column = (
                        "immediate_pushed_at"
                        if delivery_type == "immediate"
                        else "digest_pushed_at"
                    )
                    self.connection.execute(
                        f"UPDATE messages SET {column} = ? WHERE id = ?",  # noqa: S608
                        (timestamp, message_row_id),
                    )

    def get_deliveries(self, row_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT channel, delivery_type, state, attempts, max_attempts,
                   available_at, error_category, delivered_at, updated_at
            FROM deliveries WHERE message_row_id = ?
            ORDER BY delivery_type, channel
            """,
            (row_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delivery_queue_stats(self, *, hours: int, now: datetime) -> dict[str, Any]:
        cutoff = to_iso(now - timedelta(hours=hours))
        result: dict[str, Any] = {
            "pending": 0,
            "processing": 0,
            "retry": 0,
            "failed": 0,
        }
        for row in self.connection.execute(
            "SELECT state, COUNT(*) AS count FROM deliveries GROUP BY state"
        ):
            state = str(row["state"])
            if state == "queued":
                result["pending"] = int(row["count"])
            elif state in result:
                result[state] = int(row["count"])
        terminal = self.connection.execute(
            """
            SELECT
                SUM(CASE WHEN state = 'succeeded' THEN 1 ELSE 0 END) AS successes,
                SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS failures
            FROM deliveries
            WHERE updated_at >= ? AND state IN ('succeeded', 'failed')
            """,
            (cutoff,),
        ).fetchone()
        successes = int(terminal["successes"] or 0)
        failures = int(terminal["failures"] or 0)
        total = successes + failures
        errors = [
            {"category": str(row["error_category"]), "count": int(row["count"])}
            for row in self.connection.execute(
                """
                SELECT error_category, COUNT(*) AS count
                FROM deliveries
                WHERE updated_at >= ? AND error_category IS NOT NULL
                GROUP BY error_category ORDER BY count DESC, error_category
                """,
                (cutoff,),
            )
        ]
        stalled = self.connection.execute(
            """
            SELECT COUNT(*) FROM deliveries
            WHERE (state IN ('queued', 'retry') AND available_at <= ?)
               OR (state = 'processing' AND lease_until IS NOT NULL AND lease_until <= ?)
            """,
            (to_iso(now - timedelta(minutes=5)), to_iso(now)),
        ).fetchone()[0]
        result["stalled"] = int(stalled)
        result.update(
            {
                "rolling_successes": successes,
                "rolling_failures": failures,
                "success_rate": round(successes * 100 / total, 1) if total else None,
                "error_rate": round(failures * 100 / total, 1) if total else None,
                "error_categories": errors,
                "health": "degraded"
                if result["retry"] > 0 or result["failed"] > 0 or stalled > 0 or errors
                else "normal",
            }
        )
        return result

    def reserve_immediate(
        self,
        chat_id: int,
        message_id: int,
        threshold: int,
        now: datetime,
    ) -> dict[str, Any] | None:
        del threshold
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE messages
                SET immediate_pushed_at = ?
                WHERE chat_id = ? AND message_id = ?
                  AND created_at > COALESCE(
                      (SELECT value FROM metadata WHERE key = 'recovery_delivery_cutoff_at'),
                      ''
                  )
                  AND push_eligible = 1
                  AND notification_prepare_status IN ('success', 'failed_fallback')
                  AND ai_status = 'success'
                  AND (content_kind IN ('news', 'community_signal', 'benefit_deal')
                       OR (content_kind IS NULL AND ai_category = 'external_information'))
                  AND ai_score IS NOT NULL AND ai_score >= ?
                  AND (analysis_queue_state = 'succeeded' OR analysis_queue_requested = 0)
                  AND immediate_pushed_at IS NULL
                """,
                (to_iso(now), chat_id, message_id, PUSH_SCORE_THRESHOLD),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_message(chat_id, message_id)

    def release_immediate(self, chat_id: int, message_id: int) -> None:
        self.connection.execute(
            """
            UPDATE messages SET immediate_pushed_at = NULL
            WHERE chat_id = ? AND message_id = ?
            """,
            (chat_id, message_id),
        )
        self.connection.commit()

    def digest_candidates(self, cutoff: datetime) -> list[dict[str, Any]]:
        cutoff_iso = to_iso(cutoff)
        with self.connection:
            metadata = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'last_digest_at'"
            ).fetchone()
            if metadata is None:
                self.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('last_digest_at', ?)",
                    (cutoff_iso,),
                )
                return []
            rows = self.connection.execute(
                """
                SELECT * FROM messages
                WHERE push_ready_at IS NOT NULL AND push_ready_at <= ?
                  AND created_at > COALESCE(
                      (SELECT value FROM metadata WHERE key = 'recovery_delivery_cutoff_at'),
                      ''
                  )
                  AND push_eligible = 1
                  AND notification_prepare_status IN ('success', 'failed_fallback')
                  AND ai_status = 'success'
                  AND (content_kind IN ('news', 'community_signal', 'benefit_deal')
                       OR (content_kind IS NULL AND ai_category = 'external_information'))
                  AND ai_score IS NOT NULL AND ai_score >= ?
                  AND immediate_pushed_at IS NULL
                  AND digest_considered_at IS NULL
                ORDER BY ai_score DESC, push_ready_at DESC, created_at DESC
                LIMIT 500
                """,
                (cutoff_iso, DIGEST_MIN_AI_SCORE),
            ).fetchall()
            self.connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'last_digest_at'",
                (cutoff_iso,),
            )
        return [self._decode_row(row) for row in rows if row is not None]

    def mark_digest_considered(
        self,
        candidates: Iterable[dict[str, Any]],
        selected: Iterable[dict[str, Any]],
        now: datetime,
    ) -> None:
        considered_at = to_iso(now)
        candidate_keys = [(row["chat_id"], row["message_id"]) for row in candidates]
        selected_keys = [(row["chat_id"], row["message_id"]) for row in selected]
        with self.connection:
            self.connection.executemany(
                """
                UPDATE messages SET digest_considered_at = ?
                WHERE chat_id = ? AND message_id = ?
                """,
                ((considered_at, chat_id, message_id) for chat_id, message_id in candidate_keys),
            )
            self.connection.executemany(
                """
                UPDATE messages SET digest_pushed_at = ?
                WHERE chat_id = ? AND message_id = ?
                """,
                ((considered_at, chat_id, message_id) for chat_id, message_id in selected_keys),
            )

    def dashboard_stats(self, *, hours: int, now: datetime) -> dict[str, Any]:
        cutoff = to_iso(now - timedelta(hours=hours))
        runtime = self.get_runtime_config()
        threshold = PUSH_SCORE_THRESHOLD
        row = self.connection.execute(
            """
            SELECT
                COUNT(*) AS window_messages,
                COALESCE(SUM(
                    CASE WHEN push_eligible = 1
                              AND ai_status = 'success'
                              AND (content_kind IN ('news', 'community_signal', 'benefit_deal')
                                   OR (content_kind IS NULL
                                       AND ai_category = 'external_information'))
                              AND ai_score IS NOT NULL
                              AND ai_score >= ?
                         THEN 1 ELSE 0 END
                ), 0) AS important_messages,
                COALESCE(SUM(CASE WHEN immediate_pushed_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS immediate_pushes,
                COALESCE(SUM(CASE WHEN digest_pushed_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS digest_pushes,
                COALESCE(SUM(CASE WHEN prefilter_status = 'filtered' THEN 1 ELSE 0 END), 0) AS prefiltered_messages,
                COALESCE(SUM(CASE WHEN ai_status = 'filtered_non_information' THEN 1 ELSE 0 END), 0) AS non_information_messages,
                COALESCE(SUM(CASE WHEN push_eligible = 1 THEN 1 ELSE 0 END), 0) AS eligible_messages,
                COALESCE(SUM(CASE WHEN ai_status = 'error' THEN 1 ELSE 0 END), 0) AS analysis_errors,
                COUNT(DISTINCT chat_id) AS active_chats,
                MAX(created_at) AS latest_message_at
            FROM messages
            WHERE created_at >= ?
            """,
            (threshold, cutoff),
        ).fetchone()
        return {
            "window_messages": int(row["window_messages"]),
            "important_messages": int(row["important_messages"]),
            "immediate_pushes": int(row["immediate_pushes"]),
            "digest_pushes": int(row["digest_pushes"]),
            "prefiltered_messages": int(row["prefiltered_messages"]),
            "non_information_messages": int(row["non_information_messages"]),
            "eligible_messages": int(row["eligible_messages"]),
            "analysis_errors": int(row["analysis_errors"]),
            "active_chats": int(row["active_chats"]),
            "latest_message_at": row["latest_message_at"],
            "immediate_score": threshold,
            "heartbeat": self.get_listener_heartbeat(),
            "analysis_queue": self.analysis_queue_stats(hours=hours, now=now),
            "delivery_queue": self.delivery_queue_stats(hours=hours, now=now),
        }

    def list_chat_options(
        self,
        *,
        hours: int,
        now: datetime,
        recorded_only: bool = False,
        include_sources: bool = False,
    ) -> list[dict[str, Any]]:
        cutoff = to_iso(now - timedelta(hours=hours))
        runtime = self.get_runtime_config()
        watched = runtime["watch_chat_ids"] if runtime else frozenset()
        rows = self.connection.execute(
            """
            SELECT
                chats.chat_id,
                chats.chat_name,
                chats.chat_type,
                chats.username,
                COUNT(messages.id) AS message_count,
                chats.updated_at
            FROM available_chats AS chats
            LEFT JOIN messages
              ON messages.chat_id = chats.chat_id
             AND messages.created_at >= ?
            WHERE chats.is_current = 1
            GROUP BY chats.chat_id
            HAVING (? = 0 OR COUNT(messages.id) > 0)
            ORDER BY
                CASE chats.chat_type WHEN 'group' THEN 0 ELSE 1 END,
                chats.chat_name COLLATE NOCASE
            """,
            (cutoff, int(recorded_only)),
        ).fetchall()
        values = [
            {
                "chat_id": int(row["chat_id"]),
                "chat_name": str(row["chat_name"]),
                "chat_type": str(row["chat_type"]),
                "username": row["username"],
                "message_count": int(row["message_count"]),
                "watched": int(row["chat_id"]) in watched,
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]
        if include_sources:
            source_rows = self.connection.execute(
                """
                SELECT sources.id, sources.name, sources.enabled,
                       sources.updated_at, COUNT(messages.id) AS message_count
                FROM information_sources AS sources
                LEFT JOIN messages
                  ON messages.source_type = 'rss'
                 AND messages.source_id = sources.id
                 AND messages.created_at >= ?
                GROUP BY sources.id
                HAVING (? = 0 OR COUNT(messages.id) > 0)
                ORDER BY sources.name COLLATE NOCASE, sources.id
                """,
                (cutoff, int(recorded_only)),
            ).fetchall()
            values.extend(
                {
                    "chat_id": information_source_chat_id(int(row["id"])),
                    "chat_name": str(row["name"]),
                    "chat_type": "feed",
                    "username": None,
                    "message_count": int(row["message_count"]),
                    "watched": bool(row["enabled"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in source_rows
            )
        return values

    def list_messages(
        self,
        *,
        hours: int,
        now: datetime,
        query: str = "",
        chat_id: int | None = None,
        min_score: int = 0,
        push_status: str = "all",
        prefilter_status: str = "exclude",
        similar_status: str = "exclude",
        attention_only: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        if prefilter_status not in {"exclude", "all", "filtered"}:
            raise ValueError("无效的前置过滤状态")
        if similar_status not in {"exclude", "all", "suppressed"}:
            raise ValueError("无效的相似资讯状态")
        conditions = ["created_at >= ?"]
        params: list[Any] = [to_iso(now - timedelta(hours=hours))]
        if min_score > 0:
            conditions.extend(
                (
                    "ai_status = 'success'",
                    "ai_score IS NOT NULL",
                    "ai_score >= ?",
                )
            )
            params.append(min_score)
        if query:
            conditions.append("(text LIKE ? OR chat_name LIKE ? OR sender_name LIKE ?)")
            term = f"%{query}%"
            params.extend((term, term, term))
        if chat_id is not None:
            conditions.append("chat_id = ?")
            params.append(chat_id)
        if push_status == "immediate":
            conditions.append("immediate_pushed_at IS NOT NULL")
        elif push_status == "digest":
            conditions.append("digest_pushed_at IS NOT NULL")
        elif push_status == "eligible":
            conditions.append("push_eligible = 1")
        elif push_status == "not_pushed":
            conditions.append("immediate_pushed_at IS NULL AND digest_pushed_at IS NULL")
        if prefilter_status == "exclude":
            conditions.append("prefilter_status <> 'filtered'")
        elif prefilter_status == "filtered":
            conditions.append("prefilter_status = 'filtered'")
        suppressed_statuses = (
            "'suppressed', 'suppressed_unverified_update', 'superseded'"
        )
        if similar_status == "exclude":
            conditions.append(
                f"COALESCE(semantic_dedupe_status, 'historical_unreviewed') "
                f"NOT IN ({suppressed_statuses})"
            )
        elif similar_status == "suppressed":
            conditions.append(
                f"COALESCE(semantic_dedupe_status, 'historical_unreviewed') "
                f"IN ({suppressed_statuses})"
            )
        if attention_only:
            conditions.extend(
                (
                    "prefilter_status = 'passed'",
                    "ai_status = 'success'",
                    "(content_kind IN ('news', 'community_signal', 'benefit_deal') OR "
                    "(content_kind IS NULL AND ai_category = 'external_information'))",
                    "ai_score IS NOT NULL",
                    "push_eligible = 1",
                )
            )

        where = " AND ".join(conditions)
        total_row = self.connection.execute(
            f"SELECT COUNT(*) AS count FROM messages WHERE {where}",  # noqa: S608
            params,
        ).fetchone()
        rows = self.connection.execute(
            f"""
            SELECT * FROM messages
            WHERE {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,  # noqa: S608
            (*params, limit, offset),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            if row is None:
                continue
            value = self._decode_row(row, include_ai_response=False)
            if value is None:
                continue
            value["deliveries"] = self.get_deliveries(int(value["id"]))
            if str(value.get("semantic_dedupe_status") or "") not in {
                "suppressed",
                "suppressed_unverified_update",
                "superseded",
            }:
                value["similar_count"] = self.semantic_message_cluster_count(
                    int(value["id"])
                )
            else:
                value["similar_count"] = 0
            items.append(value)
        return {
            "total": int(total_row["count"]),
            "items": items,
        }

    def cleanup(self, retention_days: int, now: datetime) -> int:
        cutoff = to_iso(now - timedelta(days=retention_days))
        feedback_cutoff = to_iso(now - timedelta(days=FEEDBACK_WINDOW_DAYS))
        recovery_boundary = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (RECOVERY_HISTORY_PRESERVE_THROUGH_KEY,),
        ).fetchone()
        with self.connection:
            if recovery_boundary is None:
                reply_cursor = self.connection.execute(
                    "DELETE FROM replies WHERE replied_at < ?", (cutoff,)
                )
                message_cursor = self.connection.execute(
                    "DELETE FROM messages WHERE created_at < ?", (cutoff,)
                )
            else:
                preserve_through = str(recovery_boundary["value"])
                reply_cursor = self.connection.execute(
                    "DELETE FROM replies WHERE replied_at < ? AND replied_at > ?",
                    (cutoff, preserve_through),
                )
                message_cursor = self.connection.execute(
                    "DELETE FROM messages WHERE created_at < ? AND created_at > ?",
                    (cutoff, preserve_through),
                )
            self.connection.execute(
                "DELETE FROM feedback_records WHERE voted_at < ?",
                (feedback_cutoff,),
            )
        _ = reply_cursor.rowcount
        return max(0, message_cursor.rowcount)

    @staticmethod
    def _decode_row(
        row: sqlite3.Row | None,
        *,
        include_ai_response: bool = True,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        try:
            local_reasons = tuple(json.loads(value.pop("reasons_json")))
        except (json.JSONDecodeError, TypeError):
            local_reasons = ()
            value.pop("reasons_json", None)
        value["local_reasons"] = local_reasons
        value["reasons"] = local_reasons
        if value.get("reply_bonus", 0) > 0:
            value["reasons"] = (
                *value["reasons"],
                f"当前窗口多人回复（{value['reply_count']} 人，不参与 AI 推送评分）",
            )
        value["ai_category_label"] = category_label(value.get("ai_category"))
        value["push_eligible"] = bool(value.get("push_eligible"))
        value["is_service_message"] = bool(value.get("is_service_message"))
        value["semantic_dedupe_material_update"] = bool(
            value.get("semantic_dedupe_material_update")
        )
        value["semantic_dedupe_update_validated"] = bool(
            value.get("semantic_dedupe_update_validated")
        )
        try:
            value["feedback_context"] = json.loads(
                str(value.pop("feedback_context_json") or "{}")
            )
        except (json.JSONDecodeError, TypeError):
            value["feedback_context"] = {}
        if not include_ai_response:
            value.pop("ai_response_text", None)
            value.pop("ai_category_response_text", None)
            value.pop("community_response_text", None)
            value.pop("benefit_response_text", None)
            value.pop("semantic_dedupe_response_text", None)
            value.pop("notification_prepare_response_text", None)
        return value
