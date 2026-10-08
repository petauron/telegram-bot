from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from typing import Any

from app.database import DIGEST_MIN_AI_SCORE, Database, utc_now
from app.dedupe import select_semantic_candidates
from app.llm import (
    DEFAULT_CLASSIFICATION_MODEL,
    DEFAULT_CLASSIFICATION_REASONING_EFFORT,
    DEFAULT_NOTIFICATION_MODEL,
    DEFAULT_NOTIFICATION_REASONING_EFFORT,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_SEMANTIC_DEDUPE_MODEL,
    DEFAULT_SEMANTIC_DEDUPE_REASONING_EFFORT,
    ModelRuntimeConfig,
    NotificationPreparationOutcome,
    OpenAICompatibleClient,
    SemanticDedupeOutcome,
    validate_base_url,
    validate_model_id,
    validate_reasoning_effort,
)


SEMANTIC_DEDUPE_LEASE_SECONDS = 450
SEMANTIC_DEDUPE_LOCK_POLL_SECONDS = 0.25


class SemanticDedupeGate:
    """Serialize and persist cross-source semantic news deduplication."""

    def __init__(self, client: OpenAICompatibleClient) -> None:
        self._client = client
        self._process_lock = asyncio.Lock()
        self._owner_token = secrets.token_urlsafe(24)

    async def evaluate(
        self,
        *,
        database: Database,
        row_id: int,
        manual: bool,
        now: datetime,
    ) -> dict[str, Any] | None:
        row = database.get_message_by_id(row_id)
        if (
            row is None
            or row.get("ai_status") != "success"
            or not (
                row.get("content_kind") in {"news", "community_signal", "benefit_deal"}
                or (
                    row.get("content_kind") is None
                    and row.get("ai_category") == "external_information"
                )
            )
            or int(row.get("ai_score") or 0) < DIGEST_MIN_AI_SCORE
        ):
            return row

        async with self._process_lock:
            await self._acquire_database_lease(database)
            try:
                prepared = database.begin_semantic_dedupe(row_id, now=utc_now())
                if prepared is None:
                    return database.get_message_by_id(row_id)
                current = prepared["current"]
                candidates = select_semantic_candidates(
                    current,
                    tuple(prepared["candidates"]),
                )
                model: str | None = None
                outcome: SemanticDedupeOutcome | None = None
                config: ModelRuntimeConfig | None = None
                try:
                    config = self._runtime_config(database)
                except Exception:
                    config = None
                if candidates:
                    try:
                        if config is None:
                            raise ValueError("模型分析配置不可用")
                        model = config.semantic_dedupe_model
                        outcome = await self._client.semantic_dedupe(
                            config=config,
                            current_event=self._event_payload(current),
                            candidates=tuple(
                                self._event_payload(candidate) for candidate in candidates
                            ),
                            session_key=database.llm_dedupe_session_key(),
                        )
                    except Exception:
                        outcome = SemanticDedupeOutcome(
                            status="error",
                            model=model or DEFAULT_SEMANTIC_DEDUPE_MODEL,
                            effort=config.semantic_dedupe_reasoning_effort
                            if config is not None
                            else DEFAULT_SEMANTIC_DEDUPE_REASONING_EFFORT,
                            error_category="internal_error",
                        )
                database.complete_semantic_dedupe(
                    row_id,
                    outcome=outcome,
                    candidates=candidates,
                    model=model,
                    now=utc_now(),
                    allow_push=not manual,
                )
                prepared_row = database.get_message_by_id(row_id)
                if (
                    prepared_row is not None
                    and prepared_row.get("notification_prepare_status") == "pending"
                ):
                    if config is not None:
                        from app.notification_prepare import prepare_persisted_notification

                        await prepare_persisted_notification(
                            database=database,
                            client=self._client,
                            config=config,
                            row_id=row_id,
                            manual=manual,
                            now=utc_now(),
                        )
                    else:
                        from app.notification_prepare import (
                            deterministic_notification_fallback,
                        )

                        claimed = database.begin_notification_preparation(
                            row_id,
                            model=DEFAULT_NOTIFICATION_MODEL,
                            effort=DEFAULT_NOTIFICATION_REASONING_EFFORT,
                            now=utc_now(),
                        )
                        if claimed is not None:
                            title, body = deterministic_notification_fallback(claimed)
                            database.complete_notification_preparation(
                                row_id,
                                outcome=NotificationPreparationOutcome(
                                    status="error",
                                    model=DEFAULT_NOTIFICATION_MODEL,
                                    effort=DEFAULT_NOTIFICATION_REASONING_EFFORT,
                                    error_category="configuration",
                                ),
                                fallback_title=title,
                                fallback_body=body,
                                now=utc_now(),
                                allow_push=not manual,
                            )
                return database.get_message_by_id(row_id)
            finally:
                database.release_semantic_dedupe_lock(
                    self._owner_token,
                    now=utc_now(),
                )

    async def _acquire_database_lease(self, database: Database) -> None:
        while not database.acquire_semantic_dedupe_lock(
            self._owner_token,
            now=utc_now(),
            lease_seconds=SEMANTIC_DEDUPE_LEASE_SECONDS,
        ):
            await asyncio.sleep(SEMANTIC_DEDUPE_LOCK_POLL_SECONDS)

    @staticmethod
    def _event_payload(row: dict[str, Any]) -> dict[str, str]:
        return {
            "time": str(row.get("sent_at") or row.get("created_at") or ""),
            "summary": str(row.get("ai_summary") or ""),
            "text": str(row.get("text") or ""),
        }

    @staticmethod
    def _runtime_config(database: Database) -> ModelRuntimeConfig:
        value = database.get_model_config(include_api_key=True)
        api_key = value.get("api_key")
        if not value.get("enabled") or not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("模型分析配置不可用")
        return ModelRuntimeConfig(
            enabled=True,
            base_url=validate_base_url(str(value.get("base_url") or "")),
            api_key=api_key,
            model=validate_model_id(str(value.get("model") or "")),
            reasoning_effort=validate_reasoning_effort(
                str(value.get("reasoning_effort") or DEFAULT_REASONING_EFFORT)
            ),
            classification_model=validate_model_id(
                str(value.get("classification_model") or DEFAULT_CLASSIFICATION_MODEL)
            ),
            classification_reasoning_effort=validate_reasoning_effort(
                str(
                    value.get("classification_reasoning_effort")
                    or DEFAULT_CLASSIFICATION_REASONING_EFFORT
                )
            ),
            semantic_dedupe_model=validate_model_id(
                str(
                    value.get("semantic_dedupe_model")
                    or DEFAULT_SEMANTIC_DEDUPE_MODEL
                )
            ),
            semantic_dedupe_reasoning_effort=validate_reasoning_effort(
                str(
                    value.get("semantic_dedupe_reasoning_effort")
                    or DEFAULT_SEMANTIC_DEDUPE_REASONING_EFFORT
                )
            ),
            notification_model=validate_model_id(
                str(value.get("notification_model") or DEFAULT_NOTIFICATION_MODEL)
            ),
            notification_reasoning_effort=validate_reasoning_effort(
                str(
                    value.get("notification_reasoning_effort")
                    or DEFAULT_NOTIFICATION_REASONING_EFFORT
                )
            ),
            community_insights_enabled=bool(
                value.get("community_insights_enabled", True)
            ),
            benefit_deals_enabled=bool(value.get("benefit_deals_enabled", True)),
        )
