from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from threading import Event, Lock

import pytest

from tracefold.app.database import WorkerDatabase
from tracefold.app.worker_capabilities import CpuProcess, FiniteOperations, ModelAdapter
from tracefold.app.worker_cpu_prewarm import (
    prewarm_worker_cpu_modules,
    worker_cpu_modules_loaded,
)
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.resource import ResourceOperationOverrun


def _sleep_and_return(delay_seconds: float, value: int) -> int:
    time.sleep(delay_seconds)
    return value


def _process_start_method() -> str:
    import multiprocessing

    return multiprocessing.get_start_method()


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
            release.wait(timeout=2.0)
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


def test_cpu_process_is_spawn_only_and_serial_across_caller_timeout() -> None:
    async def scenario() -> str:
        capability = CpuProcess()
        try:
            await capability.prewarm()
            assert (
                await capability.run(
                    "verify_spawn",
                    _process_start_method,
                    service_timeout_seconds=2.0,
                    operation_timeout_seconds=2.0,
                )
                == "spawn"
            )
            with pytest.raises(ResourceOperationOverrun):
                await capability.run(
                    "slow",
                    _sleep_and_return,
                    0.2,
                    1,
                    service_timeout_seconds=1.0,
                    operation_timeout_seconds=0.01,
                )
            second = asyncio.create_task(
                capability.run(
                    "second",
                    _process_start_method,
                    service_timeout_seconds=1.0,
                    operation_timeout_seconds=1.0,
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


def test_cpu_process_preloads_all_worker_compute_modules() -> None:
    async def scenario() -> tuple[str, ...]:
        capability = CpuProcess()
        try:
            await capability.prewarm()
            expected = await capability.run(
                "workers_cpu_modules_prewarm",
                prewarm_worker_cpu_modules,
                service_timeout_seconds=20.0,
                operation_timeout_seconds=20.0,
            )
            loaded = await capability.run(
                "workers_cpu_modules_loaded",
                worker_cpu_modules_loaded,
                service_timeout_seconds=2.0,
                operation_timeout_seconds=2.0,
            )
            assert loaded == expected
            return loaded
        finally:
            capability.close()

    assert asyncio.run(scenario())
