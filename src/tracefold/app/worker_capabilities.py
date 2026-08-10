from __future__ import annotations

import asyncio
import multiprocessing
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from functools import partial
from typing import Any

from pebble import ProcessExpired, ProcessPool

from tracefold.platform.resource import (
    CpuTaskProcessExpired,
    CpuTaskTimeout,
    ResourceAdmissionTimeout,
    ResourceOperationOverrun,
)

_THREAD_FUTURE_COMPLETION_GRACE_SECONDS = 0.500
_CPU_FUTURE_COMPLETION_GRACE_SECONDS = 4.000


class FiniteOperations:
    """The one process-wide capability for bounded synchronous external work."""

    def __init__(self, *, telemetry: Any | None = None) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=3,
            thread_name_prefix="tracefold-finite-operation",
        )
        self._gate = asyncio.BoundedSemaphore(3)
        self._pending: set[asyncio.Future[Any]] = set()
        self._accepting = True
        self._closed = False
        self._telemetry = telemetry

    async def run[T](
        self,
        operation_name: str,
        function: Callable[..., T],
        /,
        *args: Any,
        timeout_seconds: float,
        before_submit: Callable[[], Awaitable[None]] | None = None,
        on_submitted: Callable[[], None] | None = None,
        allow_shutdown: bool = False,
        **kwargs: Any,
    ) -> T:
        if self._closed or (not self._accepting and not allow_shutdown):
            raise RuntimeError("finite_operations_closed")
        admission_started = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(self._gate.acquire(), timeout=5.0)
        except TimeoutError as exc:
            _record_admission(
                self._telemetry,
                "finite_operation",
                operation_name,
                "timeout",
                admission_started,
            )
            raise ResourceAdmissionTimeout(
                f"finite_operation_admission_timeout:{_operation_name(operation_name)}"
            ) from exc
        _record_admission(
            self._telemetry,
            "finite_operation",
            operation_name,
            "accepted",
            admission_started,
        )
        try:
            return await self._submit_thread(
                operation_name,
                function,
                args,
                kwargs,
                timeout_seconds=timeout_seconds,
                before_submit=before_submit,
                on_submitted=on_submitted,
            )
        except BaseException:
            raise

    async def _submit_thread[T](
        self,
        operation_name: str,
        function: Callable[..., T],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        timeout_seconds: float,
        before_submit: Callable[[], Awaitable[None]] | None,
        on_submitted: Callable[[], None] | None,
    ) -> T:
        loop = asyncio.get_running_loop()
        try:
            if before_submit is not None:
                await before_submit()
            submitted_at = loop.time()
            underlying = self._executor.submit(partial(function, *args, **kwargs))
        except BaseException:
            self._gate.release()
            raise
        wrapped = asyncio.wrap_future(underlying)
        self._pending.add(wrapped)
        _change_active(self._telemetry, "finite_operation", 1)
        _release_on_completion(
            underlying,
            loop=loop,
            wrapped=wrapped,
            pending=self._pending,
            release=self._gate.release,
            completed=lambda future: _record_completion(
                self._telemetry,
                "finite_operation",
                operation_name,
                submitted_at,
                future,
            ),
        )
        if on_submitted is not None:
            on_submitted()
        done, _ = await asyncio.wait(
            {wrapped},
            timeout=max(0.001, float(timeout_seconds) + _THREAD_FUTURE_COMPLETION_GRACE_SECONDS),
        )
        if not done:
            raise ResourceOperationOverrun(f"resource_operation_overrun:{_operation_name(operation_name)}")
        return await wrapped

    def close_admission(self) -> None:
        self._accepting = False

    async def drain(self, *, timeout_seconds: float) -> bool:
        return await _drain(self._pending, timeout_seconds=timeout_seconds)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        self._executor.shutdown(wait=False, cancel_futures=False)


class ModelAdapter:
    """One private thread for a synchronous SDK used by the serial arbiter."""

    def __init__(self, *, telemetry: Any | None = None) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tracefold-model-adapter",
        )
        self._pending: set[asyncio.Future[Any]] = set()
        self._admitting = False
        self._accepting = True
        self._closed = False
        self._telemetry = telemetry

    async def run[T](
        self,
        operation_name: str,
        function: Callable[..., T],
        /,
        *args: Any,
        timeout_seconds: float,
        before_submit: Callable[[], Awaitable[None]] | None = None,
        on_submitted: Callable[[], None] | None = None,
        allow_shutdown: bool = False,
        **kwargs: Any,
    ) -> T:
        if self._closed or (not self._accepting and not allow_shutdown):
            raise RuntimeError("model_adapter_closed")
        if self._admitting or any(not future.done() for future in self._pending):
            raise RuntimeError("model_adapter_parallel_submission")
        loop = asyncio.get_running_loop()
        admission_started = loop.time()
        _record_admission(
            self._telemetry,
            "model_adapter",
            operation_name,
            "accepted",
            admission_started,
        )
        self._admitting = True
        try:
            if before_submit is not None:
                await before_submit()
            submitted_at = loop.time()
            underlying = self._executor.submit(partial(function, *args, **kwargs))
        finally:
            self._admitting = False
        wrapped = asyncio.wrap_future(underlying)
        self._pending.add(wrapped)
        _change_active(self._telemetry, "model_adapter", 1)
        _release_on_completion(
            underlying,
            loop=loop,
            wrapped=wrapped,
            pending=self._pending,
            completed=lambda future: _record_completion(
                self._telemetry,
                "model_adapter",
                operation_name,
                submitted_at,
                future,
            ),
        )
        if on_submitted is not None:
            on_submitted()
        done, _ = await asyncio.wait(
            {wrapped},
            timeout=max(0.001, float(timeout_seconds) + _THREAD_FUTURE_COMPLETION_GRACE_SECONDS),
        )
        if not done:
            raise ResourceOperationOverrun(f"resource_operation_overrun:{_operation_name(operation_name)}")
        return await wrapped

    def close_admission(self) -> None:
        self._accepting = False

    async def drain(self, *, timeout_seconds: float) -> bool:
        return await _drain(self._pending, timeout_seconds=timeout_seconds)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        self._executor.shutdown(wait=False, cancel_futures=False)


class CpuProcess:
    """One spawn-only deterministic CPU process with pre-submit admission."""

    def __init__(self, *, telemetry: Any | None = None) -> None:
        self._pool = ProcessPool(
            max_workers=1,
            context=multiprocessing.get_context("spawn"),
        )
        self._gate = asyncio.BoundedSemaphore(1)
        self._pending: set[asyncio.Future[Any]] = set()
        self._accepting = True
        self._closed = False
        self._telemetry = telemetry

    async def prewarm(self, *, timeout_seconds: float = 10.0) -> None:
        start_method = await self.run(
            "cpu_process_prewarm",
            _cpu_process_start_method,
            service_timeout_seconds=timeout_seconds,
        )
        if start_method != "spawn":
            raise RuntimeError("cpu_process_not_spawn")

    async def run[T](
        self,
        operation_name: str,
        function: Callable[..., T],
        /,
        *args: Any,
        service_timeout_seconds: float,
        total_timeout_seconds: float | None = None,
        on_submitted: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> T:
        if not self._accepting or self._closed:
            raise RuntimeError("cpu_process_closed")
        _require_spawn_safe_function(function)
        loop = asyncio.get_running_loop()
        admission_started = loop.time()
        total_timeout = None if total_timeout_seconds is None else float(total_timeout_seconds)
        if total_timeout is not None and total_timeout <= 0.0:
            raise ValueError("cpu_total_timeout_seconds_required")
        admission_timeout = 1.0 if total_timeout is None else total_timeout
        try:
            await asyncio.wait_for(self._gate.acquire(), timeout=admission_timeout)
        except TimeoutError as exc:
            _record_admission(
                self._telemetry,
                "cpu_process",
                operation_name,
                "timeout",
                admission_started,
            )
            raise ResourceAdmissionTimeout(f"cpu_admission_timeout:{_operation_name(operation_name)}") from exc
        _record_admission(
            self._telemetry,
            "cpu_process",
            operation_name,
            "accepted",
            admission_started,
        )
        service_timeout = float(service_timeout_seconds)
        if total_timeout is not None:
            service_timeout = min(
                service_timeout,
                max(0.001, total_timeout - (loop.time() - admission_started)),
            )
        submitted_at = loop.time()
        try:
            underlying = self._pool.schedule(
                function,
                args=args,
                kwargs=kwargs,
                timeout=service_timeout,
            )
        except BaseException:
            self._gate.release()
            raise
        wrapped = asyncio.wrap_future(underlying)
        self._pending.add(wrapped)
        _change_active(self._telemetry, "cpu_process", 1)
        _release_on_completion(
            underlying,
            loop=loop,
            wrapped=wrapped,
            pending=self._pending,
            release=self._gate.release,
            completed=lambda future: _record_completion(
                self._telemetry,
                "cpu_process",
                operation_name,
                submitted_at,
                future,
            ),
        )
        if on_submitted is not None:
            on_submitted()
        done, _ = await asyncio.wait(
            {wrapped},
            timeout=max(
                0.001,
                service_timeout + _CPU_FUTURE_COMPLETION_GRACE_SECONDS,
            ),
        )
        if not done:
            raise ResourceOperationOverrun(f"resource_operation_overrun:{_operation_name(operation_name)}")
        try:
            return await wrapped
        except FutureTimeoutError as exc:
            raise CpuTaskTimeout(f"cpu_task_timeout:{_operation_name(operation_name)}:{service_timeout:g}s") from exc
        except ProcessExpired as exc:
            raise CpuTaskProcessExpired(f"cpu_task_process_expired:pid={exc.pid}:exitcode={exc.exitcode}") from exc

    def close_admission(self) -> None:
        self._accepting = False

    async def drain(self, *, timeout_seconds: float) -> bool:
        return await _drain(self._pending, timeout_seconds=timeout_seconds)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        self._pool.stop()
        with suppress(FutureTimeoutError):
            self._pool.join(timeout=1.0)


def _release_on_completion(
    underlying: Future[Any],
    *,
    loop: asyncio.AbstractEventLoop,
    wrapped: asyncio.Future[Any],
    pending: set[asyncio.Future[Any]],
    release: Callable[[], None] | None = None,
    completed: Callable[[Future[Any]], None] | None = None,
) -> None:
    def on_done(future: Future[Any]) -> None:
        def finalize() -> None:
            pending.discard(wrapped)
            if release is not None:
                release()
            if completed is not None:
                completed(future)

        loop.call_soon_threadsafe(finalize)

    underlying.add_done_callback(on_done)


def _record_admission(
    telemetry: Any | None,
    capability: str,
    operation: str,
    outcome: str,
    started_at: float,
) -> None:
    if telemetry is not None:
        telemetry.record_resource_admission(
            capability,
            _operation_name(operation),
            outcome,
            max(0.0, asyncio.get_running_loop().time() - started_at),
        )


def _record_completion(
    telemetry: Any | None,
    capability: str,
    operation: str,
    submitted_at: float,
    future: Future[Any],
) -> None:
    if telemetry is None:
        return
    outcome = "cancelled" if future.cancelled() else "error" if future.exception() is not None else "success"
    telemetry.record_resource_service(
        capability,
        _operation_name(operation),
        outcome,
        max(0.0, asyncio.get_running_loop().time() - submitted_at),
    )
    telemetry.change_resource_active(capability, -1)


def _change_active(telemetry: Any | None, capability: str, delta: int) -> None:
    if telemetry is not None:
        telemetry.change_resource_active(capability, delta)


async def _drain(
    pending: set[asyncio.Future[Any]],
    *,
    timeout_seconds: float,
) -> bool:
    active = {future for future in pending if not future.done()}
    if not active:
        return True
    _, unfinished = await asyncio.wait(
        active,
        timeout=max(0.0, float(timeout_seconds)),
    )
    return not unfinished


def _require_spawn_safe_function(function: Callable[..., Any]) -> None:
    qualname = str(getattr(function, "__qualname__", ""))
    module = str(getattr(function, "__module__", ""))
    if not module or not qualname or "<locals>" in qualname:
        raise TypeError("cpu_task_requires_top_level_function")


def _cpu_process_start_method() -> str:
    return str(multiprocessing.get_start_method())


def _operation_name(value: str) -> str:
    normalized = str(value).strip().replace(" ", "_")
    if not normalized:
        raise ValueError("operation_name_required")
    return normalized[:128]


__all__ = [
    "CpuProcess",
    "CpuTaskProcessExpired",
    "CpuTaskTimeout",
    "FiniteOperations",
    "ModelAdapter",
    "ResourceAdmissionTimeout",
    "ResourceOperationOverrun",
]
