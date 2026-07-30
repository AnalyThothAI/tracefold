from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any

from loguru import logger as default_logger

from tracefold.platform.workers.worker_result import WorkerResult

_MIN_WAIT_SECONDS = 0.001
_MAX_DURATION_SAMPLES = 256


@dataclass(slots=True)
class WorkerStatus:
    enabled: bool
    running: bool
    effective_status: str
    unavailable_reason: str | None
    last_started_at_ms: int | None
    last_finished_at_ms: int | None
    last_result: dict[str, Any] | None
    last_error: str | None
    iteration_duration_p99_ms: float | None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class WorkerBase(ABC):
    def __init__(
        self,
        *,
        name: str,
        settings: Any,
        db: Any,
        telemetry: Any,
        logger: Any | None = None,
    ) -> None:
        self.name = str(name)
        self.settings = settings
        self.db = db
        self.telemetry = telemetry
        self.logger = logger or default_logger.bind(worker=self.name)

        self.last_started_at_ms: int | None = None
        self.last_finished_at_ms: int | None = None
        self.last_result: WorkerResult | None = None
        self.last_error: str | None = None
        self.running = False

        self._stop_event = asyncio.Event()
        self._iteration_duration_ms: list[float] = []
        self._consecutive_failures = 0
        self._closed = False
        self.resources: Any | None = None
        self.provider_governor: Any | None = None
        self.runtime_id: str | None = None

    async def on_start(self) -> None:
        return None

    async def on_stop(self) -> None:
        return None

    async def on_close(self) -> None:
        return None

    @abstractmethod
    async def run_once(self) -> WorkerResult:
        raise NotImplementedError

    async def run_one_iteration(self) -> WorkerResult:
        """Run one production-equivalent iteration for maintenance commands."""
        if self._closed:
            raise RuntimeError(f"worker:{self.name}:closed")
        if self.effective_status in {"disabled", "intentionally_not_started", "unavailable"}:
            return WorkerResult(skipped=1, notes={"reason": self.effective_status})

        started = time.perf_counter()
        started_hook_completed = False
        self._start_run()
        try:
            await self.on_start()
            started_hook_completed = True
            return await self._run_iteration(started=started)
        except Exception as exc:
            if not started_hook_completed:
                self._record_iteration_failure(started, exc)
            raise
        finally:
            self.running = False
            if started_hook_completed:
                await self.on_stop()

    async def run(self) -> None:
        if not self.enabled:
            return
        self._start_run()
        try:
            await self.on_start()
            while not self._stop_event.is_set():
                iteration_started = time.perf_counter()
                try:
                    await self._run_iteration(started=iteration_started)
                except Exception:
                    await self._wait_for_next_iteration(self._backoff_seconds())
                    continue

                if self._stop_event.is_set():
                    break
                await self._wait_for_next_iteration(
                    _successful_iteration_delay(
                        interval_seconds=self.interval_seconds,
                        duration_seconds=max(0.0, time.perf_counter() - iteration_started),
                    )
                )
        finally:
            self.running = False
            await self.on_stop()

    async def stop(self) -> None:
        self._stop_event.set()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.on_close()

    def bind_runtime_resources(self, resources: Any) -> None:
        if self.running:
            raise RuntimeError(f"worker:{self.name}:resources_after_start")
        self.resources = resources

    def require_runtime_resources(self) -> Any:
        if self.resources is None:
            raise RuntimeError(f"worker:{self.name}:runtime_resources_required")
        return self.resources

    def bind_provider_governor(self, governor: Any) -> None:
        if self.running:
            raise RuntimeError(f"worker:{self.name}:provider_governor_after_start")
        self.provider_governor = governor

    def bind_runtime_id(self, runtime_id: str) -> None:
        if self.running:
            raise RuntimeError(f"worker:{self.name}:runtime_id_after_start")
        normalized = str(runtime_id).strip()
        if not normalized:
            raise ValueError("worker_runtime_id_required")
        if self.runtime_id is not None and self.runtime_id != normalized:
            raise RuntimeError(f"worker:{self.name}:runtime_id_rebind")
        self.runtime_id = normalized

    @property
    def claim_owner(self) -> str:
        if self.runtime_id is None:
            return self.name
        return f"{self.name}:{self.runtime_id}"

    def require_provider_governor(self) -> Any:
        if self.provider_governor is None:
            raise RuntimeError(f"worker:{self.name}:provider_governor_required")
        return self.provider_governor

    def status_payload(self) -> dict[str, Any]:
        return WorkerStatus(
            enabled=self.enabled,
            running=self.running,
            effective_status=self.effective_status,
            unavailable_reason=self.unavailable_reason,
            last_started_at_ms=self.last_started_at_ms,
            last_finished_at_ms=self.last_finished_at_ms,
            last_result=_worker_result_payload(self.last_result),
            last_error=self.last_error,
            iteration_duration_p99_ms=_p99(self._iteration_duration_ms),
        ).payload()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    @property
    def effective_status(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.last_error or _worker_result_failed(self.last_result):
            return "failed"
        if _worker_result_degraded(self.last_result):
            return "degraded"
        if self.running:
            return "running"
        return "stopped"

    @property
    def unavailable_reason(self) -> str | None:
        return None

    @property
    def interval_seconds(self) -> float:
        return float(self.settings.interval_seconds)

    async def _wait_for_next_iteration(self, delay_seconds: float) -> None:
        if self._stop_event.is_set():
            return
        timeout = _loop_wait_seconds(delay_seconds)
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except TimeoutError:
            return

    def _start_run(self) -> None:
        if self.running:
            raise RuntimeError(f"worker:{self.name}:already_running")
        self.running = True

    async def _run_iteration(self, *, started: float | None = None) -> WorkerResult:
        return await self._run_ungated_iteration(started=started)

    async def _run_ungated_iteration(self, *, started: float | None = None) -> WorkerResult:
        iteration_started = time.perf_counter() if started is None else started
        self.last_started_at_ms = _now_ms()
        try:
            result = _require_worker_result(self.name, await self.run_once())
        except Exception as exc:
            self._record_iteration_failure(iteration_started, exc)
            raise

        self._consecutive_failures = 0
        self.last_error = None
        self.last_result = result
        self._record_successful_iteration(iteration_started, result)
        return result

    def _record_iteration_failure(self, started: float, exc: BaseException) -> None:
        self._consecutive_failures += 1
        self.last_error = _error_text(exc)
        self.last_result = None
        self._record_failed_iteration(started)

    def _record_successful_iteration(self, started: float, result: WorkerResult) -> None:
        self.last_finished_at_ms = _now_ms()
        duration_seconds = max(0.0, time.perf_counter() - started)
        self._record_duration(duration_seconds * 1000)
        self._call_telemetry("record_processing_seconds", self.name, duration_seconds)
        self._call_telemetry("mark_last_run", self.name)
        self._record_result_metrics(result)

    def _record_failed_iteration(self, started: float) -> None:
        self.last_finished_at_ms = _now_ms()
        duration_seconds = max(0.0, time.perf_counter() - started)
        self._record_duration(duration_seconds * 1000)
        self._call_telemetry("record_processing_seconds", self.name, duration_seconds)
        self._call_telemetry("mark_last_run", self.name)
        self._call_telemetry("record_job", self.name, "failed", 1)

    def _record_result_metrics(self, result: WorkerResult) -> None:
        counts = {
            "success": result.processed,
            "failed": result.failed,
            "dead": result.dead,
            "skipped": result.skipped,
        }
        if sum(counts.values()) == 0:
            counts["success"] = 1
        for status, count in counts.items():
            if count:
                self._call_telemetry("record_job", self.name, status, count)
        self._record_projection_metrics(result.notes)

    def _record_projection_metrics(self, notes: dict[str, Any]) -> None:
        stages = {
            "source": _first_metric(notes, "source_rows", "source_rows_scanned"),
            "candidate": _first_metric(notes, "candidate_rows", "items", "targets_loaded"),
            "hydrated": _first_metric(notes, "hydrated_rows"),
            "written": _first_metric(notes, "rows_written"),
        }
        for stage, value in stages.items():
            if value is not None:
                self._call_telemetry("set_projection_rows", self.name, stage, value)

        projection_status = str(notes.get("projection_status") or "").strip().lower()
        if projection_status:
            outcome = {
                "unchanged_input": "hit",
                "rebuilt": "miss",
                "stale_snapshot": "stale",
            }.get(projection_status, projection_status)
            self._call_telemetry("record_projection_cache", self.name, outcome)
        merged = _first_metric(notes, "merged_rank_set_triggers")
        if merged:
            self._call_telemetry("record_projection_merged", self.name, merged)
        deadline_lag_ms = _first_metric(
            notes,
            "projection_deadline_lag_ms",
        )
        if deadline_lag_ms is not None and deadline_lag_ms > 0:
            self._call_telemetry(
                "record_projection_deadline_miss",
                self.name,
                str(notes.get("projection_domain") or "unknown"),
            )

        queue_depth = _first_metric(notes, "queue_depth")
        if queue_depth is not None:
            self._call_telemetry(
                "set_queue_depth",
                self.name,
                str(notes.get("queue_name") or "primary"),
                "due",
                queue_depth,
            )
        oldest_due_age_ms = _first_metric(notes, "oldest_due_age_ms")
        if oldest_due_age_ms is not None:
            self._call_telemetry(
                "set_queue_oldest_delay_seconds",
                self.name,
                str(notes.get("queue_name") or "primary"),
                oldest_due_age_ms / 1000,
            )

    def _call_telemetry(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        method = getattr(self.telemetry, method_name, None)
        if method is not None:
            method(*args, **kwargs)

    def _record_duration(self, duration_ms: float) -> None:
        self._iteration_duration_ms.append(max(0.0, float(duration_ms)))
        del self._iteration_duration_ms[:-_MAX_DURATION_SAMPLES]

    def _backoff_seconds(self) -> float:
        backoff = self.settings.backoff
        base_ms = int(backoff.base_ms)
        max_ms = int(backoff.max_ms)
        delay_ms = min(max_ms, base_ms * max(1, self._consecutive_failures))
        return _loop_wait_seconds(delay_ms / 1000)


def _worker_result_payload(result: WorkerResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "processed": int(result.processed),
        "failed": int(result.failed),
        "dead": int(result.dead),
        "skipped": int(result.skipped),
        "notes": _compact_status_notes(result.notes),
    }


def _worker_result_failed(result: WorkerResult | None) -> bool:
    return result is not None and (int(result.failed) > 0 or int(result.dead) > 0)


def _worker_result_degraded(result: WorkerResult | None) -> bool:
    if result is None:
        return False
    notes = dict(result.notes)
    if notes.get("degraded") is True:
        return True
    return str(notes.get("status") or "").strip().lower() == "degraded"


def _require_worker_result(worker_name: str, result: Any) -> WorkerResult:
    if not isinstance(result, WorkerResult):
        raise TypeError(f"worker:{worker_name}:run_once returned {type(result).__name__}")
    return result


def _compact_status_notes(notes: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _compact_status_value(value) for key, value in dict(notes).items()}


def _compact_status_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:500]
    if depth >= 2:
        return _compact_leaf(value)
    if isinstance(value, dict):
        items = list(value.items())[:20]
        compact = {str(key): _compact_status_value(item, depth=depth + 1) for key, item in items}
        if len(value) > 20:
            compact["_truncated"] = len(value) - 20
        return compact
    if isinstance(value, list | tuple):
        compact_list = [_compact_status_value(item, depth=depth + 1) for item in list(value)[:20]]
        if len(value) > 20:
            compact_list.append({"_truncated": len(value) - 20})
        return compact_list
    return _compact_leaf(value)


def _compact_leaf(value: Any) -> Any:
    text = str(value)
    return text[:500]


def _first_metric(notes: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = notes.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return max(0, int(value))
    return None


def _p99(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.99) - 1))
    return ordered[index]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _loop_wait_seconds(seconds: float) -> float:
    return max(_MIN_WAIT_SECONDS, float(seconds))


def _successful_iteration_delay(*, interval_seconds: float, duration_seconds: float) -> float:
    interval = _loop_wait_seconds(interval_seconds)
    duration = max(0.0, float(duration_seconds))
    next_slot = (int(duration // interval) + 1) * interval
    return _loop_wait_seconds(next_slot - duration)


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
