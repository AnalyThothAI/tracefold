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
_DEFAULT_BACKOFF_BASE_MS = 1_000
_DEFAULT_BACKOFF_MAX_MS = 60_000


@dataclass(slots=True)
class WorkerStatus:
    enabled: bool
    running: bool
    effective_status: str
    unavailable_reason: None
    last_started_at_ms: int | None
    last_finished_at_ms: int | None
    last_result: dict[str, Any] | None
    last_error: str | None
    iteration_duration_p99_ms: float | None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class WorkerBase(ABC):
    """Small sequential runtime loop; domains own all work policy."""

    def __init__(
        self,
        *,
        name: str,
        interval_seconds: float,
        telemetry: Any,
        logger: Any | None = None,
    ) -> None:
        self.name = str(name)
        self.interval_seconds = max(
            _MIN_WAIT_SECONDS,
            float(interval_seconds),
        )
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

    async def on_start(self) -> None:
        return None

    async def on_stop(self) -> None:
        return None

    async def on_close(self) -> None:
        return None

    @abstractmethod
    async def run_once(self) -> WorkerResult:
        raise NotImplementedError

    async def run(self) -> None:
        self._start_run()
        try:
            await self.on_start()
            while not self._stop_event.is_set():
                iteration_started = time.perf_counter()
                try:
                    result = await self._run_iteration(
                        started=iteration_started,
                    )
                except Exception:
                    await self._wait_for_next_iteration(self._backoff_seconds())
                    continue
                if self._stop_event.is_set():
                    break
                await self._wait_for_next_iteration(
                    self.next_iteration_delay_seconds(
                        result=result,
                        duration_seconds=max(
                            0.0,
                            time.perf_counter() - iteration_started,
                        ),
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

    def status_payload(self) -> dict[str, Any]:
        return WorkerStatus(
            enabled=True,
            running=self.running,
            effective_status=self.effective_status,
            unavailable_reason=None,
            last_started_at_ms=self.last_started_at_ms,
            last_finished_at_ms=self.last_finished_at_ms,
            last_result=_worker_result_payload(self.last_result),
            last_error=self.last_error,
            iteration_duration_p99_ms=_p99(self._iteration_duration_ms),
        ).payload()

    @property
    def effective_status(self) -> str:
        if self.last_error or _worker_result_failed(self.last_result):
            return "failed"
        if _worker_result_degraded(self.last_result):
            return "degraded"
        if self.running:
            return "running"
        return "stopped"

    def next_iteration_delay_seconds(
        self,
        *,
        result: WorkerResult,
        duration_seconds: float,
    ) -> float:
        del result
        return _successful_iteration_delay(
            interval_seconds=self.interval_seconds,
            duration_seconds=duration_seconds,
        )

    async def _wait_for_next_iteration(
        self,
        delay_seconds: float,
    ) -> None:
        if self._stop_event.is_set():
            return
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=_loop_wait_seconds(delay_seconds),
            )
        except TimeoutError:
            return

    def _start_run(self) -> None:
        if self.running:
            raise RuntimeError(f"worker:{self.name}:already_running")
        if self._closed:
            raise RuntimeError(f"worker:{self.name}:closed")
        self.running = True

    async def _run_iteration(
        self,
        *,
        started: float | None = None,
    ) -> WorkerResult:
        iteration_started = time.perf_counter() if started is None else started
        self.last_started_at_ms = _now_ms()
        try:
            result = _require_worker_result(
                self.name,
                await self.run_once(),
            )
        except Exception as exc:
            self._record_iteration_failure(iteration_started, exc)
            raise
        self._consecutive_failures = 0
        self.last_error = None
        self.last_result = result
        self._record_successful_iteration(iteration_started, result)
        return result

    def _record_iteration_failure(
        self,
        started: float,
        exc: BaseException,
    ) -> None:
        self._consecutive_failures += 1
        self.last_error = _error_text(exc)
        self.last_result = None
        self._record_failed_iteration(started)

    def _record_successful_iteration(
        self,
        started: float,
        result: WorkerResult,
    ) -> None:
        self.last_finished_at_ms = _now_ms()
        duration_seconds = max(0.0, time.perf_counter() - started)
        self._record_duration(duration_seconds * 1000)
        self._call_telemetry(
            "record_processing_seconds",
            self.name,
            duration_seconds,
        )
        self._call_telemetry("mark_last_run", self.name)
        self._record_result_metrics(result)

    def _record_failed_iteration(self, started: float) -> None:
        self.last_finished_at_ms = _now_ms()
        duration_seconds = max(0.0, time.perf_counter() - started)
        self._record_duration(duration_seconds * 1000)
        self._call_telemetry(
            "record_processing_seconds",
            self.name,
            duration_seconds,
        )
        self._call_telemetry("mark_last_run", self.name)
        self._call_telemetry(
            "record_job",
            self.name,
            "failed",
            1,
        )

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
                self._call_telemetry(
                    "record_job",
                    self.name,
                    status,
                    count,
                )

    def _call_telemetry(
        self,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        method = getattr(self.telemetry, method_name, None)
        if method is not None:
            method(*args, **kwargs)

    def _record_duration(self, duration_ms: float) -> None:
        self._iteration_duration_ms.append(max(0.0, float(duration_ms)))
        del self._iteration_duration_ms[:-_MAX_DURATION_SAMPLES]

    def _backoff_seconds(self) -> float:
        delay_ms = min(
            _DEFAULT_BACKOFF_MAX_MS,
            _DEFAULT_BACKOFF_BASE_MS * max(1, self._consecutive_failures),
        )
        return _loop_wait_seconds(delay_ms / 1000)


def _worker_result_payload(
    result: WorkerResult | None,
) -> dict[str, Any] | None:
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
    return notes.get("degraded") is True or (str(notes.get("status") or "").strip().lower() == "degraded")


def _require_worker_result(
    worker_name: str,
    result: Any,
) -> WorkerResult:
    if not isinstance(result, WorkerResult):
        raise TypeError(f"worker:{worker_name}:run_once returned {type(result).__name__}")
    return result


def _compact_status_notes(
    notes: dict[str, Any],
) -> dict[str, Any]:
    return {str(key): _compact_status_value(value) for key, value in dict(notes).items()}


def _compact_status_value(
    value: Any,
    *,
    depth: int = 0,
) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:500]
    if depth >= 2:
        return str(value)[:500]
    if isinstance(value, dict):
        items = list(value.items())[:20]
        compact = {
            str(key): _compact_status_value(
                item,
                depth=depth + 1,
            )
            for key, item in items
        }
        if len(value) > 20:
            compact["_truncated"] = len(value) - 20
        return compact
    if isinstance(value, list | tuple):
        compact_list = [_compact_status_value(item, depth=depth + 1) for item in list(value)[:20]]
        if len(value) > 20:
            compact_list.append({"_truncated": len(value) - 20})
        return compact_list
    return str(value)[:500]


def _p99(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0,
        min(
            len(ordered) - 1,
            int(len(ordered) * 0.99) - 1,
        ),
    )
    return ordered[index]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _loop_wait_seconds(seconds: float) -> float:
    return max(_MIN_WAIT_SECONDS, float(seconds))


def _successful_iteration_delay(
    *,
    interval_seconds: float,
    duration_seconds: float,
) -> float:
    interval = _loop_wait_seconds(interval_seconds)
    duration = max(0.0, float(duration_seconds))
    next_slot = (int(duration // interval) + 1) * interval
    return _loop_wait_seconds(next_slot - duration)


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


__all__ = ["WorkerBase"]
