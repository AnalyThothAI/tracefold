from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from tracefold.app.worker_manifest import (
    worker_iteration_group,
    worker_start_phase,
    worker_start_priority,
)
from tracefold.app.worker_status import effective_worker_status

_START_PRIORITY = worker_start_priority()
_START_PHASE = worker_start_phase()
_ITERATION_GROUP = worker_iteration_group()
_DEFAULT_PHASE_DELAYS_SECONDS = {0: 0.0, 1: 15.0, 2: 60.0}
_DEFAULT_STAGGER_SECONDS = 1.0
_DEFAULT_ITERATION_GROUP_CAPACITIES = {
    "latency_projection": 1,
    "background_projection": 2,
}


class WorkerScheduler:
    def __init__(
        self,
        *,
        workers: Mapping[str, Any],
        db: Any,
        startup_phase_delays_seconds: Mapping[int, float] | None = None,
        startup_stagger_seconds: float = _DEFAULT_STAGGER_SECONDS,
        iteration_group_capacities: Mapping[str, int] | None = None,
    ) -> None:
        self.workers = dict(workers)
        self.db = db
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.startup_phase_delays_seconds = _startup_phase_delays(startup_phase_delays_seconds)
        self.startup_stagger_seconds = _nonnegative_seconds(
            startup_stagger_seconds,
            error="worker_scheduler_startup_stagger_invalid",
        )
        self.iteration_gates = {
            name: asyncio.Semaphore(capacity)
            for name, capacity in _iteration_group_capacities(iteration_group_capacities).items()
        }
        self._stop_event = asyncio.Event()
        self._started = False

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("worker_scheduler:already_started")
        self._started = True
        self._stop_event.clear()
        try:
            phase_positions: dict[int, int] = {}
            for name in self._ordered_worker_names():
                worker = self.workers[name]
                if not _worker_startable(worker):
                    continue
                iteration_group = _ITERATION_GROUP.get(name)
                if iteration_group is not None:
                    _set_iteration_gate(worker, self.iteration_gates[iteration_group])
                phase = _START_PHASE.get(name, 2)
                phase_position = phase_positions.get(phase, 0)
                phase_positions[phase] = phase_position + 1
                startup_delay_seconds = (
                    self.startup_phase_delays_seconds.get(phase, self.startup_phase_delays_seconds[2])
                    + phase_position * self.startup_stagger_seconds
                )
                self.tasks[name] = asyncio.create_task(
                    self._run_after_startup_delay(
                        worker,
                        delay_seconds=startup_delay_seconds,
                    ),
                    name=f"worker:{name}",
                )
                await asyncio.sleep(0)
                task = self.tasks[name]
                if task.done():
                    exc = task.exception()
                    if exc is not None:
                        raise exc
        except Exception:
            self._stop_event.set()
            for name in self.tasks:
                await self.workers[name].stop()
            if self.tasks:
                await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            self.tasks.clear()
            self._started = False
            raise

    async def stop(self) -> None:
        errors: list[Exception] = []
        self._stop_event.set()
        for worker in self.workers.values():
            try:
                await worker.stop()
            except Exception as exc:
                errors.append(exc)
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for task in self.tasks.values():
            task_error = _task_exception(task)
            if task_error is not None:
                errors.append(task_error)
        for worker in self.workers.values():
            try:
                await worker.aclose()
            except Exception as exc:
                errors.append(exc)
        try:
            await self.db.aclose()
        except Exception as exc:
            errors.append(exc)
        self._started = False
        if errors:
            raise ExceptionGroup("worker_scheduler_stop_failed", errors)

    async def _run_after_startup_delay(self, worker: Any, *, delay_seconds: float) -> None:
        if delay_seconds > 0:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay_seconds)
        if self._stop_event.is_set():
            return
        await worker.run()

    def status_payload(self) -> dict[str, dict[str, Any]]:
        return {name: _worker_status_payload(worker) for name, worker in self.workers.items()}

    def _ordered_worker_names(self) -> list[str]:
        return sorted(
            self.workers,
            key=lambda name: (_START_PRIORITY.get(name, 25), name),
        )


def _worker_startable(worker: Any) -> bool:
    return worker_effective_status(worker) not in {"disabled", "intentionally_not_started", "unavailable"}


def worker_effective_status(worker: Any) -> str:
    return effective_worker_status(_worker_status_payload(worker))


def _worker_status_payload(worker: Any) -> dict[str, Any]:
    payload = worker.status_payload()
    if not isinstance(payload, Mapping):
        raise TypeError("worker_status_payload_must_be_dict")
    return dict(payload)


def _task_exception(task: asyncio.Task[Any]) -> Exception | None:
    if not task.done():
        return None
    if task.cancelled():
        return None
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return None
    return error if isinstance(error, Exception) else None


def _startup_phase_delays(value: Mapping[int, float] | None) -> dict[int, float]:
    resolved = dict(_DEFAULT_PHASE_DELAYS_SECONDS)
    if value is not None:
        resolved.update(
            {
                int(phase): _nonnegative_seconds(
                    delay,
                    error="worker_scheduler_startup_phase_delay_invalid",
                )
                for phase, delay in value.items()
            }
        )
    return resolved


def _nonnegative_seconds(value: float, *, error: str) -> float:
    seconds = float(value)
    if seconds < 0:
        raise ValueError(error)
    return seconds


def _iteration_group_capacities(value: Mapping[str, int] | None) -> dict[str, int]:
    resolved = dict(_DEFAULT_ITERATION_GROUP_CAPACITIES)
    if value is not None:
        resolved.update({str(name): int(capacity) for name, capacity in value.items()})
    if any(capacity <= 0 for capacity in resolved.values()):
        raise ValueError("worker_scheduler_iteration_group_capacity_invalid")
    missing = set(_ITERATION_GROUP.values()).difference(resolved)
    if missing:
        raise ValueError(f"worker_scheduler_iteration_group_capacity_missing:{','.join(sorted(missing))}")
    return resolved


def _set_iteration_gate(worker: Any, gate: asyncio.Semaphore) -> None:
    setter = getattr(worker, "set_iteration_gate", None)
    if not callable(setter):
        raise TypeError("worker_scheduler_iteration_gate_unsupported")
    setter(gate)
