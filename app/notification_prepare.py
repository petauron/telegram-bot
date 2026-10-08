from __future__ import annotations

from datetime import datetime
from typing import Any

from app.database import DIGEST_MIN_AI_SCORE, Database, utc_now
from app.llm import (
    NotificationPreparationOutcome,
    ModelRuntimeConfig,
    OpenAICompatibleClient,
    clean_notification_body,
    clean_notification_title,
)


PREPARATION_PASS_STATUSES = frozenset(
    {
        "unique",
        "unique_no_candidates",
        "material_update",
        "failed_open",
        "low_confidence_pass",
        "representative_replaced",
    }
)
PREPARATION_FINAL_STATUSES = frozenset({"success", "failed_fallback"})


def deterministic_notification_fallback(row: dict[str, Any]) -> tuple[str, str]:
    title = clean_notification_title(str(row.get("ai_summary") or ""))
    body = clean_notification_body(str(row.get("text") or ""))
    if not title:
        for line in str(row.get("text") or "").splitlines():
            title = clean_notification_title(line)
            if title:
                break
    if not title:
        title = "重要资讯提醒"
    if not body:
        body = title
    return title, body


async def prepare_persisted_notification(
    *,
    database: Database,
    client: OpenAICompatibleClient,
    config: ModelRuntimeConfig,
    row_id: int,
    manual: bool,
    now: datetime,
) -> dict[str, Any] | None:
    """Prepare one accepted news item and persist one reusable final result."""
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
        or row.get("semantic_dedupe_status") not in PREPARATION_PASS_STATUSES
    ):
        return row
    if row.get("notification_prepare_status") in PREPARATION_FINAL_STATUSES:
        return row

    prepared = database.begin_notification_preparation(
        row_id,
        model=config.notification_model,
        effort=config.notification_reasoning_effort,
        now=now,
    )
    if prepared is None:
        return database.get_message_by_id(row_id)
    fallback_title, fallback_body = deterministic_notification_fallback(prepared)
    if prepared.get("content_kind") == "community_signal":
        outcome = NotificationPreparationOutcome(
            status="success",
            model=str(prepared.get("community_model") or config.model or "community"),
            effort=str(prepared.get("community_effort") or config.reasoning_effort),
            title=clean_notification_title(
                str(prepared.get("community_title") or fallback_title)
            ),
            body=clean_notification_body(
                str(prepared.get("community_summary") or fallback_body)
            ),
        )
        database.complete_notification_preparation(
            row_id,
            outcome=outcome,
            fallback_title=fallback_title,
            fallback_body=fallback_body,
            now=utc_now(),
            allow_push=not manual,
        )
        return database.get_message_by_id(row_id)
    if prepared.get("content_kind") == "benefit_deal":
        outcome = NotificationPreparationOutcome(
            status="success",
            model=str(prepared.get("benefit_model") or config.model or "benefit"),
            effort=str(prepared.get("benefit_effort") or config.reasoning_effort),
            title=clean_notification_title(
                str(prepared.get("benefit_title") or fallback_title)
            ),
            body=clean_notification_body(
                str(prepared.get("benefit_summary") or fallback_body)
            ),
        )
        database.complete_notification_preparation(
            row_id,
            outcome=outcome,
            fallback_title=fallback_title,
            fallback_body=fallback_body,
            now=utc_now(),
            allow_push=not manual,
        )
        return database.get_message_by_id(row_id)
    try:
        outcome = await client.prepare_notification(
            config=config,
            sent_at=str(prepared.get("sent_at") or prepared.get("created_at") or ""),
            text=str(prepared.get("text") or ""),
            session_key=database.llm_session_key(int(prepared["chat_id"])),
        )
    except Exception:
        outcome = NotificationPreparationOutcome(
            status="error",
            model=config.notification_model,
            effort=config.notification_reasoning_effort,
            error_category="internal_error",
        )
    database.complete_notification_preparation(
        row_id,
        outcome=outcome,
        fallback_title=fallback_title,
        fallback_body=fallback_body,
        now=utc_now(),
        allow_push=not manual,
    )
    return database.get_message_by_id(row_id)
