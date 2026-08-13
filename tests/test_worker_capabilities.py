from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future
from contextlib import suppress
from threading import Event, Lock
from typing import Any

import pytest
from pebble import CONSTS as PEBBLE_CONSTS

from tracefold.app import database as database_module
from tracefold.app.database import WorkerDatabase
from tracefold.app.worker_capabilities import CpuProcess, FiniteOperations, ModelAdapter
from tracefold.app.worker_cpu_prewarm import (
    news_cpu_modules_loaded,
    prewarm_news_cpu_modules,
    prewarm_projection_cpu_modules,
    projection_cpu_modules_loaded,
)
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.resource import CpuTaskTimeout, ResourceOperationOverrun, await_concurrent_future


def _sleep_and_return(delay_seconds: float, value: int) -> int:
    time.sleep(delay_seconds)
    return value


def _ignore_sigterm_and_sleep(delay_seconds: float, value: int) -> int:
    import signal

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(delay_seconds)
    return value


def _process_start_method() -> str:
    import multiprocessing

    return multiprocessing.get_start_method()


class _ExpectedNativeFailure(RuntimeError):
    pass


def _delayed_native_failure(delay_seconds: float) -> None:
    time.sleep(delay_seconds)
    raise _ExpectedNativeFailure("native_failure")


@pytest.mark.parametrize("capability_type", [FiniteOperations, ModelAdapter])
def test_native_completion_finishes_before_thread_wrapper_watchdog(capability_type: type[Any]) -> None:
    async def scenario() -> None:
        capability = capability_type()
        try:
            with pytest.raises(_ExpectedNativeFailure, match="native_failure"):
                await capability.run(
                    "native_failure",
                    _delayed_native_failure,
                    0.05,
                    timeout_seconds=0.01,
                )
        finally:
            capability.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["result", "error"])
def test_completed_native_future_wins_and_cancels_its_delayed_wrapper(outcome: str) -> None:
    async def scenario() -> None:
        underlying: Future[int] = Future()
        wrapped: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        if outcome == "result":
            underlying.set_result(7)
            assert (
                await await_concurrent_future(
                    underlying,
                    wrapped,
                    timeout_seconds=0.001,
                    overrun_code="resource_operation_overrun:test",
                )
                == 7
            )
        else:
            underlying.set_exception(_ExpectedNativeFailure("native_failure"))
            with pytest.raises(_ExpectedNativeFailure, match="native_failure"):
                await await_concurrent_future(
                    underlying,
                    wrapped,
                    timeout_seconds=0.001,
                    overrun_code="resource_operation_overrun:test",
                )
        assert wrapped.cancelled()

    asyncio.run(scenario())


def test_finite_permits_follow_underlying_futures_after_callers_time_out() -> None:
    async def scenario() -> str:
        telemetry = TelemetryRegistry()
        capability = FiniteOperations(telemetry=telemetry)
        release = Event()
        all_started = Event()
        submitted = 0
        submitted_lock = Lock()

        def blocking() -> str:
            nonlocal submitted
            with submitted_lock:
                submitted += 1
                if submitted == 3:
                    all_started.set()
            release.wait(timeout=2.0)
            return "done"

        try:
            first: list[asyncio.Task[str]] = [
                asyncio.create_task(
                    capability.run(
                        f"blocking_{index}",
                        blocking,
                        timeout_seconds=0.01,
                    )
                )
                for index in range(3)
            ]
            results = await asyncio.gather(*first, return_exceptions=True)
            assert all(isinstance(result, ResourceOperationOverrun) for result in results)
            assert all_started.wait(timeout=1.0)

            waiting = asyncio.create_task(capability.run("must_wait", blocking, timeout_seconds=0.5))
            await asyncio.sleep(0.05)
            assert not waiting.done()
            assert submitted == 3
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await waiting

            release.set()
            assert await capability.drain(timeout_seconds=1.0)
            await asyncio.sleep(0)
            assert (
                await capability.run(
                    "after_release",
                    lambda: "released",
                    timeout_seconds=0.5,
                )
                == "released"
            )
            await asyncio.sleep(0)
            return telemetry.render_prometheus_text()
        finally:
            release.set()
            capability.close()

    metrics = asyncio.run(scenario())
    assert 'tracefold_worker_resource_active{capability="finite_operation"} 0.0' in metrics
    assert "tracefold_worker_resource_admission_seconds_count" in metrics
    assert "tracefold_worker_resource_service_seconds_count" in metrics


def test_finite_awaits_durable_fence_before_submitting_thread_work() -> None:
    async def scenario() -> None:
        capability = FiniteOperations()
        events: list[str] = []

        async def persist_fence() -> None:
            events.append("fenced")

        def operation() -> str:
            assert events == ["fenced"]
            events.append("submitted")
            return "done"

        try:
            assert (
                await capability.run(
                    "fenced_operation",
                    operation,
                    timeout_seconds=0.5,
                    before_submit=persist_fence,
                )
                == "done"
            )
            assert events == ["fenced", "submitted"]
        finally:
            capability.close()

    asyncio.run(scenario())


def test_finite_does_not_submit_when_durable_fence_fails() -> None:
    class FenceFailure(RuntimeError):
        pass

    async def scenario() -> None:
        capability = FiniteOperations()
        submitted = False

        async def reject_fence() -> None:
            raise FenceFailure("fence_failed")

        def operation() -> None:
            nonlocal submitted
            submitted = True

        try:
            with pytest.raises(FenceFailure, match="fence_failed"):
                await capability.run(
                    "rejected_fenced_operation",
                    operation,
                    timeout_seconds=0.5,
                    before_submit=reject_fence,
                )
            assert submitted is False
            assert (
                await capability.run(
                    "after_rejected_fence",
                    lambda: "available",
                    timeout_seconds=0.5,
                )
                == "available"
            )
        finally:
            capability.close()

    asyncio.run(scenario())


def test_model_adapter_keeps_its_only_slot_until_underlying_future_finishes() -> None:
    async def scenario() -> None:
        capability = ModelAdapter()
        release = Event()
        started = Event()

        def blocking() -> int:
            started.set()
            release.wait(timeout=2.0)
            return 1

        try:
            with pytest.raises(ResourceOperationOverrun):
                await capability.run("first", blocking, timeout_seconds=0.01)
            assert started.wait(timeout=1.0)
            with pytest.raises(RuntimeError, match="model_adapter_parallel_submission"):
                await capability.run("parallel", lambda: 2, timeout_seconds=0.5)
            release.set()
            assert await capability.drain(timeout_seconds=1.0)
            await asyncio.sleep(0)
            assert await capability.run("next", lambda: 3, timeout_seconds=0.5) == 3
        finally:
            release.set()
            capability.close()

    asyncio.run(scenario())


def test_model_awaits_durable_fence_before_submitting_thread_work() -> None:
    async def scenario() -> None:
        capability = ModelAdapter()
        events: list[str] = []

        async def persist_fence() -> None:
            events.append("fenced")

        def operation() -> str:
            assert events == ["fenced"]
            events.append("submitted")
            return "done"

        try:
            assert (
                await capability.run(
                    "fenced_model",
                    operation,
                    timeout_seconds=0.5,
                    before_submit=persist_fence,
                )
                == "done"
            )
            assert events == ["fenced", "submitted"]
        finally:
            capability.close()

    asyncio.run(scenario())


def test_model_does_not_submit_when_durable_fence_fails() -> None:
    class FenceFailure(RuntimeError):
        pass

    async def scenario() -> None:
        capability = ModelAdapter()
        submitted = False

        async def reject_fence() -> None:
            raise FenceFailure("fence_failed")

        def operation() -> None:
            nonlocal submitted
            submitted = True

        try:
            with pytest.raises(FenceFailure, match="fence_failed"):
                await capability.run(
                    "rejected_fenced_model",
                    operation,
                    timeout_seconds=0.5,
                    before_submit=reject_fence,
                )
            assert submitted is False
            assert await capability.run("after_rejected_fence", lambda: "available", timeout_seconds=0.5) == "available"
        finally:
            capability.close()

    asyncio.run(scenario())


def test_database_has_two_business_slots_and_an_independent_control_slot() -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=object(), telemetry=None)
        release = Event()
        both_started = Event()
        submitted = 0
        submitted_lock = Lock()

        def blocking() -> int:
            nonlocal submitted
            with submitted_lock:
                submitted += 1
                if submitted == 2:
                    both_started.set()
            release.wait()
            return 1

        try:
            first: list[asyncio.Task[int]] = [
                asyncio.create_task(
                    database.run_business(
                        f"business_{index}",
                        blocking,
                        operation_timeout_seconds=0.01,
                    )
                )
                for index in range(2)
            ]
            results = await asyncio.gather(*first, return_exceptions=True)
            assert all(isinstance(result, ResourceOperationOverrun) for result in results)
            assert both_started.wait(timeout=1.0)
            assert (
                await database.run_control(
                    "control",
                    lambda: 7,
                    operation_timeout_seconds=0.5,
                )
                == 7
            )

            waiting: asyncio.Task[int] = asyncio.create_task(
                database.run_business(
                    "third_business",
                    blocking,
                    operation_timeout_seconds=0.5,
                )
            )
            await asyncio.sleep(0.05)
            assert not waiting.done()
            assert submitted == 2
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await waiting

            release.set()
            assert await database.drain_business(timeout_seconds=1.0)
            await asyncio.sleep(0)
            assert (
                await database.run_business(
                    "after_release",
                    lambda: 9,
                    operation_timeout_seconds=0.5,
                )
                == 9
            )
        finally:
            release.set()
            database.close_executors()

    asyncio.run(scenario())


def test_database_native_transaction_timeout_precedes_outer_overrun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NativeTransactionTimeout(RuntimeError):
        pass

    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=object(), telemetry=None)

        def native_timeout() -> None:
            time.sleep(0.02)
            raise _NativeTransactionTimeout("native transaction timeout")

        try:
            with pytest.raises(_NativeTransactionTimeout, match="native transaction timeout"):
                await database.run_business(
                    "native_timeout_precedes_outer",
                    native_timeout,
                    operation_timeout_seconds=0.01,
                )
        finally:
            database.close_executors()

    monkeypatch.setattr(
        database_module,
        "_WORKER_BUSINESS_OPERATION_COMPLETION_GRACE_SECONDS",
        0.02,
    )
    asyncio.run(scenario())


def test_database_outer_grace_exceeds_worker_transaction_timeout_margin() -> None:
    assert (
        database_module._WORKER_BUSINESS_OPERATION_COMPLETION_GRACE_SECONDS
        > database_module._WORKER_TRANSACTION_TIMEOUT_MARGIN_SECONDS
    )
    assert (
        database_module._WORKER_CONTROL_OPERATION_COMPLETION_GRACE_SECONDS
        > database_module._WORKER_TRANSACTION_TIMEOUT_MARGIN_SECONDS
    )


def test_late_wrapped_failure_after_overrun_is_retrieved() -> None:
    async def scenario() -> list[dict[str, object]]:
        loop = asyncio.get_running_loop()
        underlying: Future[int] = Future()
        wrapped = asyncio.wrap_future(underlying)
        unhandled: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        with pytest.raises(ResourceOperationOverrun, match="late_native_failure"):
            await await_concurrent_future(
                underlying,
                wrapped,
                timeout_seconds=0.001,
                overrun_code="resource_operation_overrun:late_native_failure",
            )
        underlying.set_exception(_ExpectedNativeFailure("late native failure"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        del wrapped
        await asyncio.sleep(0)
        return unhandled

    assert asyncio.run(scenario()) == []


def test_late_wrapped_failure_after_caller_cancellation_is_retrieved() -> None:
    async def scenario() -> list[dict[str, object]]:
        loop = asyncio.get_running_loop()
        underlying: Future[int] = Future()
        wrapped = asyncio.wrap_future(underlying)
        unhandled: list[dict[str, object]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        waiting = asyncio.create_task(
            await_concurrent_future(
                underlying,
                wrapped,
                timeout_seconds=1.0,
                overrun_code="resource_operation_overrun:cancelled_caller",
            )
        )

        await asyncio.sleep(0)
        waiting.cancel()
        with suppress(asyncio.CancelledError):
            await waiting
        underlying.set_exception(_ExpectedNativeFailure("late native failure"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        del wrapped
        await asyncio.sleep(0)
        return unhandled

    assert asyncio.run(scenario()) == []


def test_cpu_process_is_spawn_only_and_serial_across_caller_cancellation() -> None:
    async def scenario() -> str:
        capability = CpuProcess()
        try:
            await capability.prewarm()
            assert (
                await capability.run(
                    "verify_spawn",
                    _process_start_method,
                    service_timeout_seconds=2.0,
                )
                == "spawn"
            )
            submitted = asyncio.Event()
            slow = asyncio.create_task(
                capability.run(
                    "slow",
                    _sleep_and_return,
                    0.2,
                    1,
                    service_timeout_seconds=1.0,
                    on_submitted=submitted.set,
                )
            )
            await asyncio.wait_for(submitted.wait(), timeout=1.0)
            slow.cancel()
            with suppress(asyncio.CancelledError):
                await slow
            second = asyncio.create_task(
                capability.run(
                    "second",
                    _process_start_method,
                    service_timeout_seconds=1.0,
                )
            )
            await asyncio.sleep(0.05)
            assert not second.done()
            start_method = await second
            assert await capability.drain(timeout_seconds=1.0)
            return start_method
        finally:
            capability.close()

    assert asyncio.run(scenario()) == "spawn"


def test_cpu_process_shares_one_total_timeout_between_admission_and_service() -> None:
    async def scenario() -> int:
        capability = CpuProcess()
        try:
            await capability.prewarm()
            submitted = asyncio.Event()
            blocking = asyncio.create_task(
                capability.run(
                    "blocking",
                    _sleep_and_return,
                    1.1,
                    1,
                    service_timeout_seconds=2.0,
                    on_submitted=submitted.set,
                )
            )
            await asyncio.wait_for(submitted.wait(), timeout=1.0)
            result = await capability.run(
                "wait_then_compute",
                _sleep_and_return,
                0.05,
                2,
                service_timeout_seconds=2.5,
                total_timeout_seconds=2.5,
            )
            assert await blocking == 1
            return result
        finally:
            capability.close()

    assert asyncio.run(scenario()) == 2


def test_cpu_native_timeout_finishes_before_the_wrapper_watchdog() -> None:
    async def scenario() -> None:
        capability = CpuProcess()
        try:
            await capability.prewarm()
            with pytest.raises(CpuTaskTimeout, match="cpu_task_timeout:native_timeout"):
                await capability.run(
                    "native_timeout",
                    _ignore_sigterm_and_sleep,
                    10.0,
                    1,
                    service_timeout_seconds=0.05,
                )
        finally:
            capability.close()

    asyncio.run(scenario())


def test_cpu_native_timeout_grace_includes_pebble_process_termination_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PEBBLE_CONSTS, "term_timeout", 4.5)

    async def scenario() -> None:
        capability = CpuProcess()
        try:
            await capability.prewarm()
            with pytest.raises(CpuTaskTimeout, match="cpu_task_timeout:late_native_timeout"):
                await capability.run(
                    "late_native_timeout",
                    _ignore_sigterm_and_sleep,
                    30.0,
                    1,
                    service_timeout_seconds=0.05,
                )
            assert (
                await capability.run(
                    "after_late_native_timeout",
                    _sleep_and_return,
                    0.01,
                    7,
                    service_timeout_seconds=1.0,
                )
                == 7
            )
        finally:
            capability.close()

    asyncio.run(scenario())


def test_projection_cpu_process_preloads_only_short_worker_compute_modules() -> None:
    async def scenario() -> tuple[str, ...]:
        capability = CpuProcess()
        try:
            await capability.prewarm()
            expected = await capability.run(
                "projection_cpu_modules_prewarm",
                prewarm_projection_cpu_modules,
                service_timeout_seconds=20.0,
            )
            loaded = await capability.run(
                "projection_cpu_modules_loaded",
                projection_cpu_modules_loaded,
                service_timeout_seconds=2.0,
            )
            assert loaded == expected
            return loaded
        finally:
            capability.close()

    loaded = asyncio.run(scenario())

    assert loaded
    assert "tracefold.news.projection" not in loaded


def test_news_cpu_process_preloads_only_news_compute() -> None:
    async def scenario() -> tuple[str, ...]:
        capability = CpuProcess()
        try:
            await capability.prewarm()
            expected = await capability.run(
                "news_cpu_modules_prewarm",
                prewarm_news_cpu_modules,
                service_timeout_seconds=20.0,
            )
            loaded = await capability.run(
                "news_cpu_modules_loaded",
                news_cpu_modules_loaded,
                service_timeout_seconds=2.0,
            )
            assert loaded == expected
            return loaded
        finally:
            capability.close()

    assert asyncio.run(scenario()) == ("tracefold.news.projection",)
