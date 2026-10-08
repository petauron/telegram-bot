from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.analysis_queue import retry_delay_seconds
from app.database import Database, utc_now
from app.push import PushDispatcher


LOGGER = logging.getLogger("telegram_priority.delivery_queue")
DELIVERY_WORKERS = 2
DELIVERY_LEASE_SECONDS = 120


def _database_call(path: str, method: str, *args: Any, **kwargs: Any) -> Any:
    database = Database(path, initialize=False)
    try:
        return getattr(database, method)(*args, **kwargs)
    finally:
        database.close()


class PersistentDeliveryQueue:
    """Durable per-channel delivery with independent partial-failure recovery."""

    def __init__(
        self,
        database_path: str,
        dispatcher: PushDispatcher,
        *,
        worker_count: int = DELIVERY_WORKERS,
    ) -> None:
        self._database_path = database_path
        self._dispatcher = dispatcher
        self._worker_count = max(1, worker_count)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> int:
        recovered = await asyncio.to_thread(
            _database_call,
            self._database_path,
            "recover_delivery_jobs",
            now=utc_now(),
        )
        self._tasks = [
            asyncio.create_task(self._supervise_worker(index), name=f"delivery-worker-{index}")
            for index in range(self._worker_count)
        ]
        self._wake.set()
        return int(recovered)

    def wake(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _supervise_worker(self, index: int) -> None:
        delay = 1.0
        while not self._stop.is_set():
            started = asyncio.get_running_loop().time()
            try:
                await self._worker(index)
                if self._stop.is_set():
                    return
                LOGGER.error("Delivery worker exited unexpectedly: worker=%s", index)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log exception text: transport errors may contain tokens.
                LOGGER.error(
                    "Delivery worker recovered: worker=%s error=%s",
                    index, type(exc).__name__,
                )
            if asyncio.get_running_loop().time() - started >= 60:
                delay = 1.0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 30.0)

    async def _worker(self, _: int) -> None:
        while not self._stop.is_set():
            unit = await asyncio.to_thread(
                _database_call,
                self._database_path,
                "claim_next_delivery_unit",
                now=utc_now(),
                lease_seconds=DELIVERY_LEASE_SECONDS,
            )
            if unit is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
            await self._deliver(unit)
            await asyncio.sleep(0)

    async def _deliver(self, unit: dict[str, Any]) -> None:
        ids = tuple(int(value) for value in unit["ids"])
        error_category: str | None = None
        try:
            succeeded = await self._dispatcher.send_channel(
                str(unit["channel"]),
                str(unit["delivery_type"]),
                tuple(unit["rows"]),
            )
            if not succeeded:
                error_category = "delivery_failed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "投递任务异常：channel=%s type=%s error=%s",
                unit.get("channel"),
                unit.get("delivery_type"),
                type(exc).__name__,
            )
            succeeded = False
            error_category = "internal_error"

        if not succeeded:
            retried = await asyncio.to_thread(
                _database_call,
                self._database_path,
                "retry_delivery_unit",
                ids,
                now=utc_now(),
                delay_seconds=retry_delay_seconds(int(unit.get("attempts") or 1)),
                error_category=error_category or "delivery_failed",
            )
            if retried:
                self._wake.set()
                return
        await asyncio.to_thread(
            _database_call,
            self._database_path,
            "finish_delivery_unit",
            ids,
            now=utc_now(),
            succeeded=succeeded,
            error_category=None if succeeded else error_category,
        )

