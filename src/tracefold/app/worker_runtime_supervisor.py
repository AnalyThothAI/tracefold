from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any

_CONTROL_RETRY_SECONDS = (1.0, 5.0, 15.0)
_CONTROL_STALE_SECONDS = 15.0


def _terminate_process(_reason: str) -> None:
    os.kill(os.getpid(), signal.SIGTERM)


class WorkerRuntimeSupervisor:
    """Own runnable-unit and persisted-control-loop lifecycles."""

    def __init__(
        self,
        *,
        workers: Mapping[str, Any],
        inactive_statuses: Mapping[str, Mapping[str, Any]] | None = None,
        status_sink: (Callable[[dict[str, dict[str, Any]]], Awaitable[None]] | None) = None,
        heartbeat_interval_seconds: float = 5.0,
        fatal_exit: Callable[[str], None] = _terminate_process,
    ) -> None:
        self.workers = dict(workers)
        self.inactive_statuses = {name: dict(status) for name, status in dict(inactive_statuses or {}).items()}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.status_sink = status_sink
        self.heartbeat_interval_seconds = max(
            0.1,
            float(heartbeat_interval_seconds),
        )
        self.fatal_exit = fatal_exit
        self.control_task: asyncio.Task[None] | None = None
        self._first_control_attempt = asyncio.Event()
        self._control_started_monotonic: float | None = None
        self._last_control_success_monotonic: float | None = None
        self._control_error: str | None = None
        self._control_failures = 0
        self._started = False
        self._stop_requested = False

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("worker_runtime_supervisor_already_started")
        self._started = True
        self._stop_requested = False
        self._first_control_attempt.clear()
        self._control_started_monotonic = time.monotonic()
        self._last_control_success_monotonic = None
        self._control_error = None
        self._control_failures = 0
        try:
            for name, worker in self.workers.items():
                self.tasks[name] = asyncio.create_task(
                    worker.run(),
                    name=f"worker:{name}",
                )
                await asyncio.sleep(0)
                task = self.tasks[name]
                if task.done() and (error := task.exception()) is not None:
                    raise error
            if self.status_sink is not None:
                self.control_task = asyncio.create_task(
                    self._control_loop(),
                    name="worker:runtime_control",
                )
                await self._first_control_attempt.wait()
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        errors = await self.request_stop()
        errors.extend(await self.finish())
        if errors:
            raise ExceptionGroup(
                "worker_runtime_supervisor_stop_failed",
                errors,
            )

    async def request_stop(self) -> list[Exception]:
        errors: list[Exception] = []
        if self._stop_requested:
            return errors
        self._stop_requested = True
        if self.control_task is not None:
            self.control_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.control_task
            self.control_task = None
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
            gathered = asyncio.gather(
                *self.tasks.values(),
                return_exceptions=True,
            )
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
                    results = await asyncio.gather(
                        *self.tasks.values(),
                        return_exceptions=True,
                    )
            errors.extend(result for result in results if isinstance(result, Exception))
        for worker in self.workers.values():
            try:
                await worker.aclose()
            except Exception as exc:
                errors.append(exc)
        self.tasks.clear()
        self._started = False
        self._stop_requested = False
        if publish_status and self.status_sink is not None:
            try:
                await self._publish_status()
            except Exception as exc:
                errors.append(exc)
        return errors

    def status_payload(self) -> dict[str, dict[str, Any]]:
        runnable = {name: dict(worker.status_payload()) for name, worker in self.workers.items()}
        return {**runnable, **self.inactive_statuses}

    def readiness(
        self,
        *,
        now_monotonic: float | None = None,
    ) -> dict[str, Any]:
        if not self._started or self._stop_requested:
            return {
                "ok": False,
                "reason": "supervisor_not_running",
            }
        if self.status_sink is None:
            return {"ok": True, "reason": None}
        task = self.control_task
        if task is None or task.done():
            return {
                "ok": False,
                "reason": "control_loop_stopped",
                "last_error": self._control_error,
            }
        last_success = self._last_control_success_monotonic
        if last_success is None:
            started = self._control_started_monotonic
            current = time.monotonic() if now_monotonic is None else float(now_monotonic)
            stale_seconds = max(
                0.0,
                current - (started if started is not None else current),
            )
            if stale_seconds > _CONTROL_STALE_SECONDS:
                return {
                    "ok": False,
                    "reason": "heartbeat_stale",
                    "stale_seconds": stale_seconds,
                    "last_error": self._control_error,
                }
            return {
                "ok": False,
                "reason": "heartbeat_never_persisted",
                "stale_seconds": stale_seconds,
                "last_error": self._control_error,
            }
        current = time.monotonic() if now_monotonic is None else float(now_monotonic)
        stale_seconds = max(0.0, current - last_success)
        if stale_seconds > _CONTROL_STALE_SECONDS:
            return {
                "ok": False,
                "reason": "heartbeat_stale",
                "stale_seconds": stale_seconds,
                "last_error": self._control_error,
            }
        return {
            "ok": True,
            "reason": None,
            "stale_seconds": stale_seconds,
        }

    async def _control_loop(self) -> None:
        while True:
            delay = self.heartbeat_interval_seconds
            try:
                await self._publish_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._control_failures += 1
                self._control_error = _error_text(exc)
                last_viable = (
                    self._last_control_success_monotonic
                    if self._last_control_success_monotonic is not None
                    else self._control_started_monotonic
                )
                if last_viable is not None and time.monotonic() - last_viable > _CONTROL_STALE_SECONDS:
                    self.fatal_exit(self._control_error)
                    return
                delay = _CONTROL_RETRY_SECONDS[
                    min(
                        self._control_failures - 1,
                        len(_CONTROL_RETRY_SECONDS) - 1,
                    )
                ]
            else:
                self._last_control_success_monotonic = time.monotonic()
                self._control_error = None
                self._control_failures = 0
            finally:
                self._first_control_attempt.set()
            await asyncio.sleep(delay)

    async def _publish_status(self) -> None:
        if self.status_sink is not None:
            await self.status_sink(self.status_payload())


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return (f"{type(exc).__name__}: {text}" if text else type(exc).__name__)[:500]


__all__ = ["WorkerRuntimeSupervisor"]
