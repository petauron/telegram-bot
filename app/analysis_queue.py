from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.database import Database, utc_now
from app.llm import (
    AnalysisInProgressError,
    AnalysisUnavailableError,
    BatchClassificationOutcome,
    ClassificationOutcome,
    ClassificationReuseClient,
    OpenAICompatibleClient,
    analyze_persisted_message,
    model_runtime_config_from_value,
    preflight_persisted_message_for_batch,
)
from app.semantic_dedupe import SemanticDedupeGate


LOGGER = logging.getLogger("telegram_priority.analysis_queue")

DEFAULT_ANALYSIS_WORKERS = 2
DEFAULT_ANALYSIS_BATCH_TARGET = 20
DEFAULT_ANALYSIS_BATCH_MAX_ITEMS = 50
DEFAULT_ANALYSIS_BATCH_MAX_CHARS = 12_000
DEFAULT_ANALYSIS_BATCH_MAX_WAIT_SECONDS = 2
ANALYSIS_MAX_ATTEMPTS = 5
ANALYSIS_RETRY_BASE_SECONDS = 5
ANALYSIS_RETRY_MAX_SECONDS = 300
STRUCTURED_RESPONSE_MAX_ATTEMPTS = 2
# Four bounded model stages plus one serialized semantic tail can legitimately
# exceed the former 15-minute lease at the configured 180-second read timeout.
ANALYSIS_LEASE_SECONDS = 1500
TRANSIENT_ANALYSIS_ERRORS = frozenset(
    {"busy", "network_error", "timeout", "rate_limited", "upstream_error"}
)
TERMINAL_PIPELINE_STATUSES = frozenset(
    {"success", "prefiltered", "filtered_non_information"}
)


def retry_delay_seconds(attempts: int) -> int:
    exponent = max(0, int(attempts) - 1)
    return min(ANALYSIS_RETRY_MAX_SECONDS, ANALYSIS_RETRY_BASE_SECONDS * (2**exponent))


def should_retry_analysis_error(error_category: str | None, attempts: int) -> bool:
    """Retry transient failures normally and malformed model JSON only once."""
    if error_category in TRANSIENT_ANALYSIS_ERRORS:
        return True
    return bool(
        error_category == "invalid_response"
        and int(attempts) < STRUCTURED_RESPONSE_MAX_ATTEMPTS
    )


def _database_call(path: str, method: str, *args: Any, **kwargs: Any) -> Any:
    database = Database(path, initialize=False)
    try:
        return getattr(database, method)(*args, **kwargs)
    finally:
        database.close()


class PersistentAnalysisQueue:
    """Durable, per-chat ordered analysis workers with fair chat scheduling."""

    def __init__(
        self,
        database_path: str,
        client: OpenAICompatibleClient,
        *,
        on_live_success: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        worker_count: int = DEFAULT_ANALYSIS_WORKERS,
        batch_target: int = DEFAULT_ANALYSIS_BATCH_TARGET,
        batch_max_items: int = DEFAULT_ANALYSIS_BATCH_MAX_ITEMS,
        batch_max_chars: int = DEFAULT_ANALYSIS_BATCH_MAX_CHARS,
        batch_max_wait_seconds: int = DEFAULT_ANALYSIS_BATCH_MAX_WAIT_SECONDS,
    ) -> None:
        self._database_path = database_path
        self._client = client
        self._semantic_gate = SemanticDedupeGate(client)
        self._on_live_success = on_live_success
        self._worker_count = max(1, worker_count)
        self._batch_target = max(1, int(batch_target))
        self._batch_max_items = max(self._batch_target, int(batch_max_items))
        self._batch_max_chars = max(1_000, int(batch_max_chars))
        self._batch_max_wait_seconds = max(0, int(batch_max_wait_seconds))
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> dict[str, int]:
        recovered = await asyncio.to_thread(
            _database_call,
            self._database_path,
            "recover_analysis_jobs",
            now=utc_now(),
        )
        recovered["semantic_dedupe"] = await asyncio.to_thread(
            _database_call,
            self._database_path,
            "recover_semantic_dedupe",
            now=utc_now(),
        )
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"analysis-worker-{index}")
            for index in range(self._worker_count)
        ]
        self._wake.set()
        return recovered

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _claim(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            _database_call,
            self._database_path,
            "claim_next_analysis_job",
            now=utc_now(),
            lease_seconds=ANALYSIS_LEASE_SECONDS,
        )

    async def _claim_batch(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            _database_call,
            self._database_path,
            "claim_next_analysis_batch",
            now=utc_now(),
            lease_seconds=ANALYSIS_LEASE_SECONDS,
            target_items=self._batch_target,
            max_items=self._batch_max_items,
            max_chars=self._batch_max_chars,
            max_wait_seconds=self._batch_max_wait_seconds,
        )

    async def _worker(self, _: int) -> None:
        while not self._stop.is_set():
            batch = await self._claim_batch()
            if batch is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                except TimeoutError:
                    pass
                continue
            await self._process_batch(batch)
            await asyncio.sleep(0)

    async def _process(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        row_id = int(job["message_row_id"])
        manual = str(job["kind"]) == "manual"
        database = Database(self._database_path, initialize=False)
        error_category: str | None = None
        error_stage: str | None = None
        result_status = "error"
        row: dict[str, Any] | None = None
        try:
            row = database.get_message_by_id(row_id)
            if row is None:
                error_category = "message_missing"
            else:
                try:
                    row = await analyze_persisted_message(
                        database=database,
                        client=self._client,
                        row=row,
                        now=utc_now(),
                        manual=manual,
                        semantic_gate=self._semantic_gate,
                    )
                except AnalysisUnavailableError:
                    row = database.get_message_by_id(row_id)
                except AnalysisInProgressError:
                    error_category = "busy"
                    error_stage = "queue"
                    row = database.get_message_by_id(row_id)
                if row is not None:
                    result_status = str(row.get("ai_status") or "error")
                    error_category = error_category or row.get("ai_error_category")
                    error_stage = error_stage or row.get("ai_error_stage")
                    if result_status == "disabled" and not error_category:
                        error_category = "model_disabled"
                    elif result_status == "unavailable" and not error_category:
                        error_category = "configuration"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error("分析任务异常：job_id=%s error=%s", job_id, type(exc).__name__)
            error_category = "internal_error"
            error_stage = error_stage or "queue"
        finally:
            database.close()

        await self._finalize_job(
            job,
            row=row,
            result_status=result_status,
            error_category=error_category,
            error_stage=error_stage,
        )

    async def _finalize_job(
        self,
        job: dict[str, Any],
        *,
        row: dict[str, Any] | None,
        result_status: str,
        error_category: str | None,
        error_stage: str | None,
        batch_id: int | None = None,
        local_terminal: bool = False,
    ) -> None:
        job_id = int(job["id"])
        row_id = int(job["message_row_id"])
        manual = str(job["kind"]) == "manual"
        attempts = int(job.get("attempts") or 0)
        if should_retry_analysis_error(error_category, attempts):
            retried = await asyncio.to_thread(
                _database_call,
                self._database_path,
                "retry_analysis_job",
                job_id,
                now=utc_now(),
                delay_seconds=retry_delay_seconds(attempts),
                error_category=str(error_category),
                error_stage=error_stage,
            )
            if retried:
                if batch_id is not None:
                    await asyncio.to_thread(
                        _database_call,
                        self._database_path,
                        "update_analysis_batch_item",
                        batch_id,
                        job_id,
                        state="retry",
                        error_category=str(error_category),
                        now=utc_now(),
                    )
                self._wake.set()
                return

        succeeded = result_status in TERMINAL_PIPELINE_STATUSES and not error_category
        created_deliveries = await asyncio.to_thread(
            _database_call,
            self._database_path,
            "finish_analysis_job",
            job_id,
            now=utc_now(),
            succeeded=succeeded,
            result_status=result_status,
            error_category=str(error_category) if error_category else None,
            error_stage=error_stage,
        )
        if batch_id is not None:
            await asyncio.to_thread(
                _database_call,
                self._database_path,
                "update_analysis_batch_item",
                batch_id,
                job_id,
                state=(
                    "local_terminal"
                    if local_terminal and succeeded
                    else ("succeeded" if succeeded else "failed")
                ),
                error_category=str(error_category) if error_category else None,
                now=utc_now(),
            )
        if (
            created_deliveries
            and succeeded
            and result_status == "success"
            and not manual
            and self._on_live_success
        ):
            latest = await asyncio.to_thread(
                _database_call,
                self._database_path,
                "get_message_by_id",
                row_id,
            )
            if latest is not None:
                await self._on_live_success(latest)

    async def _fallback_classify_batch(
        self,
        *,
        config: Any,
        items: list[dict[str, Any]],
        session_key: str,
        recent_context: tuple[dict[str, str], ...],
    ) -> BatchClassificationOutcome:
        """Compatibility path for injected test clients that only implement classify."""
        outcomes: dict[int, ClassificationOutcome] = {}
        unresolved: list[int] = []
        error_category: str | None = None
        for item in items:
            outcome = await self._client.classify(
                config=config,
                sent_at=str(item["time"]),
                text=str(item["text"]),
                session_key=session_key,
                recent_context=recent_context,
            )
            row_id = int(item["message_row_id"])
            if outcome.status == "success" and outcome.category is not None:
                outcomes[row_id] = outcome
            else:
                unresolved.append(row_id)
                error_category = outcome.error_category or "invalid_response"
        return BatchClassificationOutcome(
            status=("success" if not unresolved else ("partial" if outcomes else "error")),
            model=str(config.classification_model),
            outcomes=outcomes,
            unresolved_row_ids=tuple(unresolved),
            error_category=error_category,
            effort=str(config.classification_reasoning_effort),
        )

    async def _classify_batch_subset(
        self,
        *,
        database: Database,
        batch_id: int,
        jobs_by_row_id: dict[int, dict[str, Any]],
        items: list[dict[str, Any]],
        config: Any,
        session_key: str,
        recent_context: tuple[dict[str, str], ...],
        parent_call_id: int | None = None,
    ) -> tuple[
        dict[int, ClassificationOutcome],
        dict[int, tuple[int, int]],
    ]:
        if not items:
            return {}, {}
        try:
            classify_batch = getattr(self._client, "classify_batch", None)
            if callable(classify_batch):
                outcome = await classify_batch(
                    config=config,
                    messages=tuple(items),
                    session_key=session_key,
                    recent_context=recent_context,
                )
            else:
                outcome = await self._fallback_classify_batch(
                    config=config,
                    items=items,
                    session_key=session_key,
                    recent_context=recent_context,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = BatchClassificationOutcome(
                status="error",
                model=str(config.classification_model),
                outcomes={},
                unresolved_row_ids=tuple(
                    int(item["message_row_id"]) for item in items
                ),
                error_category="internal_error",
                effort=str(config.classification_reasoning_effort),
            )
        input_chars = sum(
            len(str(item["time"])) + len(str(item["text"])) for item in items
        )
        call_id = database.record_analysis_batch_call(
            batch_id,
            parent_call_id=parent_call_id,
            item_count=len(items),
            input_chars=input_chars,
            outcome=outcome,
            now=utc_now(),
        )
        accepted = dict(outcome.outcomes)
        references: dict[int, tuple[int, int]] = {}
        for row_id, classification in accepted.items():
            job = jobs_by_row_id.get(int(row_id))
            if job is None:
                continue
            database.store_analysis_batch_classification(
                batch_id,
                int(job["id"]),
                call_id=call_id,
                classification=classification,
                now=utc_now(),
            )
            references[int(row_id)] = (batch_id, call_id)

        unresolved_ids = tuple(
            row_id
            for row_id in outcome.unresolved_row_ids
            if int(row_id) in jobs_by_row_id and int(row_id) not in accepted
        )
        unresolved_items = [
            item for item in items if int(item["message_row_id"]) in unresolved_ids
        ]
        if not unresolved_items:
            return accepted, references

        for item in unresolved_items:
            job = jobs_by_row_id[int(item["message_row_id"])]
            database.mark_analysis_batch_item_call(
                batch_id,
                int(job["id"]),
                call_id=call_id,
                error_category=outcome.error_category or "invalid_response",
                now=utc_now(),
            )
            references[int(item["message_row_id"])] = (batch_id, call_id)

        if outcome.error_category == "invalid_response" and len(unresolved_items) > 1:
            database.increment_analysis_batch_split(batch_id, now=utc_now())
            midpoint = len(unresolved_items) // 2
            left_outcomes, left_references = await self._classify_batch_subset(
                database=database,
                batch_id=batch_id,
                jobs_by_row_id=jobs_by_row_id,
                items=unresolved_items[:midpoint],
                config=config,
                session_key=session_key,
                recent_context=recent_context,
                parent_call_id=call_id,
            )
            right_outcomes, right_references = await self._classify_batch_subset(
                database=database,
                batch_id=batch_id,
                jobs_by_row_id=jobs_by_row_id,
                items=unresolved_items[midpoint:],
                config=config,
                session_key=session_key,
                recent_context=recent_context,
                parent_call_id=call_id,
            )
            accepted.update(left_outcomes)
            accepted.update(right_outcomes)
            references.update(left_references)
            references.update(right_references)
            return accepted, references

        for item in unresolved_items:
            row_id = int(item["message_row_id"])
            accepted[row_id] = ClassificationOutcome(
                status="error",
                model=str(outcome.model),
                effort=str(outcome.effort),
                error_category=outcome.error_category or "invalid_response",
            )
        return accepted, references

    async def _process_batch(self, batch: dict[str, Any]) -> None:
        batch_id = int(batch["id"])
        jobs = list(batch.get("jobs") or ())
        if not jobs:
            await asyncio.to_thread(
                _database_call,
                self._database_path,
                "finish_analysis_batch",
                batch_id,
                now=utc_now(),
            )
            return
        if str(batch.get("kind")) == "manual":
            await self._process(jobs[0])
            job = await asyncio.to_thread(
                _database_call,
                self._database_path,
                "get_analysis_job",
                int(jobs[0]["id"]),
            )
            if job is not None:
                state = "succeeded" if job["state"] == "succeeded" else (
                    "retry" if job["state"] == "retry" else "failed"
                )
                await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "update_analysis_batch_item",
                    batch_id,
                    int(job["id"]),
                    state=state,
                    error_category=job.get("error_category"),
                    now=utc_now(),
                )
            await asyncio.to_thread(
                _database_call,
                self._database_path,
                "finish_analysis_batch",
                batch_id,
                now=utc_now(),
            )
            return

        database = Database(self._database_path, initialize=False)
        rows_by_id: dict[int, dict[str, Any]] = {}
        classifications: dict[int, ClassificationOutcome] = {}
        references: dict[int, tuple[int, int]] = {}
        ready_jobs: list[dict[str, Any]] = []
        try:
            try:
                config = model_runtime_config_from_value(
                    database.get_model_config(include_api_key=True)
                )
            except AnalysisUnavailableError:
                config = None

            for job in jobs:
                row_id = int(job["message_row_id"])
                row = database.get_message_by_id(row_id)
                if row is None:
                    await self._finalize_job(
                        job,
                        row=None,
                        result_status="error",
                        error_category="message_missing",
                        error_stage="queue",
                        batch_id=batch_id,
                    )
                    continue
                rows_by_id[row_id] = row
                if config is None:
                    ready_jobs.append(job)
                    continue
                preflight = preflight_persisted_message_for_batch(
                    database=database,
                    row=row,
                    now=utc_now(),
                )
                if not preflight.ready:
                    terminal = database.get_message_by_id(row_id)
                    result_status = str((terminal or {}).get("ai_status") or "error")
                    error_category = (terminal or {}).get("ai_error_category")
                    await self._finalize_job(
                        job,
                        row=terminal,
                        result_status=result_status,
                        error_category=error_category,
                        error_stage=(terminal or {}).get("ai_error_stage"),
                        batch_id=batch_id,
                        local_terminal=True,
                    )
                    continue
                cached = database.cached_analysis_batch_classification(int(job["id"]))
                if cached is not None:
                    classification, prior_batch_id, prior_call_id = cached
                    classifications[row_id] = classification
                    references[row_id] = (prior_batch_id, prior_call_id)
                    database.mark_analysis_batch_item_call(
                        batch_id,
                        int(job["id"]),
                        call_id=prior_call_id,
                        error_category=None,
                        now=utc_now(),
                    )
                ready_jobs.append(job)

            unclassified_jobs = [
                job
                for job in ready_jobs
                if int(job["message_row_id"]) not in classifications
            ]
            if config is not None and unclassified_jobs:
                earliest_row_id = int(unclassified_jobs[0]["message_row_id"])
                recent_context = database.recent_llm_context(earliest_row_id)
                session_key = database.llm_session_key(int(batch["chat_id"]))
                request_items = [
                    {
                        "message_row_id": int(job["message_row_id"]),
                        "message_id": int(rows_by_id[int(job["message_row_id"])]["message_id"]),
                        "time": str(
                            rows_by_id[int(job["message_row_id"])].get("sent_at")
                            or rows_by_id[int(job["message_row_id"])].get("created_at")
                            or ""
                        ),
                        "text": str(rows_by_id[int(job["message_row_id"])].get("text") or ""),
                    }
                    for job in unclassified_jobs
                ]
                jobs_by_row_id = {
                    int(job["message_row_id"]): job for job in unclassified_jobs
                }
                new_outcomes, new_references = await self._classify_batch_subset(
                    database=database,
                    batch_id=batch_id,
                    jobs_by_row_id=jobs_by_row_id,
                    items=request_items,
                    config=config,
                    session_key=session_key,
                    recent_context=recent_context,
                )
                classifications.update(new_outcomes)
                references.update(new_references)

            for job in ready_jobs:
                await asyncio.to_thread(
                    _database_call,
                    self._database_path,
                    "renew_analysis_batch_leases",
                    batch_id,
                    now=utc_now(),
                    lease_seconds=ANALYSIS_LEASE_SECONDS,
                )
                row_id = int(job["message_row_id"])
                row = database.get_message_by_id(row_id)
                if row is None:
                    await self._finalize_job(
                        job,
                        row=None,
                        result_status="error",
                        error_category="message_missing",
                        error_stage="queue",
                        batch_id=batch_id,
                    )
                    continue
                classification = classifications.get(row_id)
                if classification is None:
                    classification = ClassificationOutcome(
                        status="error",
                        model=(
                            str(config.classification_model)
                            if config is not None
                            else "unavailable"
                        ),
                        effort=(
                            str(config.classification_reasoning_effort)
                            if config is not None
                            else "low"
                        ),
                        error_category=("internal_error" if config is not None else "configuration"),
                    )
                error_category: str | None = None
                error_stage: str | None = None
                try:
                    result = await analyze_persisted_message(
                        database=database,
                        client=ClassificationReuseClient(self._client, classification),
                        row=row,
                        now=utc_now(),
                        manual=False,
                        semantic_gate=self._semantic_gate,
                    )
                except AnalysisUnavailableError:
                    result = database.get_message_by_id(row_id)
                except AnalysisInProgressError:
                    error_category = "busy"
                    error_stage = "queue"
                    result = database.get_message_by_id(row_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.error(
                        "批量分析任务异常：job_id=%s error=%s",
                        int(job["id"]),
                        type(exc).__name__,
                    )
                    error_category = "internal_error"
                    error_stage = "queue"
                    result = database.get_message_by_id(row_id)
                reference = references.get(row_id)
                if reference is not None:
                    database.link_message_classification_batch(
                        row_id,
                        batch_id=reference[0],
                        call_id=reference[1],
                    )
                    result = database.get_message_by_id(row_id)
                result_status = str((result or {}).get("ai_status") or "error")
                error_category = error_category or (result or {}).get("ai_error_category")
                error_stage = error_stage or (result or {}).get("ai_error_stage")
                if result_status == "disabled" and not error_category:
                    error_category = "model_disabled"
                elif result_status == "unavailable" and not error_category:
                    error_category = "configuration"
                await self._finalize_job(
                    job,
                    row=result,
                    result_status=result_status,
                    error_category=error_category,
                    error_stage=error_stage,
                    batch_id=batch_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "分类批次异常：batch_id=%s error=%s",
                batch_id,
                type(exc).__name__,
            )
            for job in jobs:
                current = database.get_analysis_job(int(job["id"]))
                if current is None or current.get("state") != "processing":
                    continue
                row = database.get_message_by_id(int(job["message_row_id"]))
                await self._finalize_job(
                    job,
                    row=row,
                    result_status=str((row or {}).get("ai_status") or "error"),
                    error_category="internal_error",
                    error_stage="queue",
                    batch_id=batch_id,
                )
        finally:
            database.close()
            await asyncio.to_thread(
                _database_call,
                self._database_path,
                "finish_analysis_batch",
                batch_id,
                now=utc_now(),
            )
