from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any

from tracefold.app.worker_status import effective_worker_status


class WorkerRuntimeSupervisor:
    """Owns lifecycle only; scheduling policy remains in typed workers/coordinators."""

    def __init__(
        self,
        *,
        workers: Mapping[str, Any],
        status_sink: Callable[[dict[str, dict[str, Any]]], Awaitable[None]] | None = None,
        heartbeat_interval_seconds: float = 5.0,
    ) -> None:
        self.workers = dict(workers)
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.status_sink = status_sink
        self.heartbeat_interval_seconds = max(0.1, float(heartbeat_interval_seconds))
        self.status_task: asyncio.Task[None] | None = None
        self._started = False
        self._stop_requested = False

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("worker_runtime_supervisor_already_started")
        self._started = True
        self._stop_requested = False
        try:
            for name, worker in self.workers.items():
                if effective_worker_status(worker.status_payload()) in {
                    "disabled",
                    "intentionally_not_started",
                    "unavailable",
                }:
                    continue
                self.tasks[name] = asyncio.create_task(worker.run(), name=f"worker:{name}")
                await asyncio.sleep(0)
                task = self.tasks[name]
                if task.done() and (error := task.exception()) is not None:
                    raise error
            await self._publish_status()
            if self.status_sink is not None:
                self.status_task = asyncio.create_task(
                    self._status_heartbeat_loop(),
                    name="worker:runtime_status",
                )
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        errors = await self.request_stop()
        errors.extend(await self.finish())
        if errors:
            raise ExceptionGroup("worker_runtime_supervisor_stop_failed", errors)

    async def request_stop(self) -> list[Exception]:
        errors: list[Exception] = []
        if self._stop_requested:
            return errors
        self._stop_requested = True
        if self.status_task is not None:
            self.status_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.status_task
            self.status_task = None
        for worker in self.workers.values():
            try:
                await worker.stop()
            except Exception as exc:
                errors.append(exc)
        return errors

    async def finish(
        self,
        *,
        timeout_seconds: float | None = None,
        publish_status: bool = True,
    ) -> list[Exception]:
        errors: list[Exception] = []
        if self.tasks:
            gathered = asyncio.gather(*self.tasks.values(), return_exceptions=True)
            if timeout_seconds is None:
                results = await gathered
            else:
                try:
                    results = await asyncio.wait_for(
                        asyncio.shield(gathered),
                        timeout=max(0.0, float(timeout_seconds)),
                    )
                except TimeoutError:
                    for task in self.tasks.values():
                        task.cancel()
                    results = await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, Exception))
        for worker in self.workers.values():
            try:
                await worker.aclose()
            except Exception as exc:
                errors.append(exc)
        self.tasks.clear()
        self._started = False
        self._stop_requested = False
        if publish_status:
            try:
                await self._publish_status()
            except Exception as exc:
                errors.append(exc)
        return errors

    def status_payload(self) -> dict[str, dict[str, Any]]:
        return {name: dict(worker.status_payload()) for name, worker in self.workers.items()}

    async def _status_heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            await self._publish_status()

    async def _publish_status(self) -> None:
        if self.status_sink is not None:
            await self.status_sink(self.status_payload())
