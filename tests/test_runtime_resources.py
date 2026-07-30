import asyncio
import time
from threading import Event

import pytest

from tracefold.app.runtime_resources import ProviderGovernor, RuntimeResources
from tracefold.platform.workers.resource_errors import CpuTaskTimeout


def _sleep_then_return(delay_seconds: float, value: str) -> str:
    time.sleep(delay_seconds)
    return value


def test_runtime_resources_are_code_owned_and_bounded():
    resources = RuntimeResources()
    try:
        assert resources.capacities == {
            "realtime_db": 1,
            "background_db": 1,
            "provider_io": 3,
            "model": 1,
            "cpu": 1,
        }
    finally:
        resources.close()


def test_cpu_timeout_replaces_the_worker_and_next_pure_task_succeeds():
    async def scenario() -> None:
        resources = RuntimeResources()
        try:
            with pytest.raises(CpuTaskTimeout):
                await resources.run_cpu(_sleep_then_return, 0.2, "late", timeout_seconds=0.01)
            assert (
                await resources.run_cpu(
                    _sleep_then_return,
                    0.0,
                    "ready",
                    timeout_seconds=1.0,
                )
                == "ready"
            )
        finally:
            resources.close()

    asyncio.run(scenario())


def test_provider_governor_enforces_global_host_and_specialized_lanes():
    async def scenario() -> None:
        governor = ProviderGovernor()
        active_global = 0
        active_by_host: dict[str, int] = {}
        maxima = {"global": 0, "same_host": 0, "profile_gmgn": 0}
        active_profile_gmgn = 0
        state_lock = asyncio.Lock()

        async def exercise(host: str, lane: str | None = None) -> None:
            nonlocal active_global, active_profile_gmgn
            async with governor.acquire(host=host, lane=lane):
                async with state_lock:
                    active_global += 1
                    active_by_host[host] = active_by_host.get(host, 0) + 1
                    if lane == "profile_gmgn":
                        active_profile_gmgn += 1
                    maxima["global"] = max(maxima["global"], active_global)
                    maxima["same_host"] = max(maxima["same_host"], active_by_host[host])
                    maxima["profile_gmgn"] = max(maxima["profile_gmgn"], active_profile_gmgn)
                await asyncio.sleep(0.01)
                async with state_lock:
                    active_global -= 1
                    active_by_host[host] -= 1
                    if lane == "profile_gmgn":
                        active_profile_gmgn -= 1

        await asyncio.gather(
            exercise("gmgn.example", "profile_gmgn"),
            exercise("gmgn.example", "profile_gmgn"),
            exercise("gmgn.example"),
            exercise("binance.example"),
            exercise("relay.example"),
        )

        assert governor.capacities == {
            "global": 3,
            "per_host": 2,
            "profile_gmgn": 1,
            "profile_binance": 1,
            "image": 1,
        }
        assert maxima["global"] <= 3
        assert maxima["same_host"] <= 2
        assert maxima["profile_gmgn"] == 1

    asyncio.run(scenario())


def test_runtime_resources_stop_admission_and_report_bounded_lane_drain():
    async def scenario() -> None:
        resources = RuntimeResources()
        release = Event()
        started = Event()

        def blocking_provider() -> str:
            started.set()
            release.wait()
            return "done"

        task = asyncio.create_task(resources.run_provider_io(blocking_provider))
        async with asyncio.timeout(1):
            while not started.is_set():
                await asyncio.sleep(0.001)
        resources.begin_shutdown()
        try:
            assert not await resources.drain(("provider_io",), timeout_seconds=0.01)
            with pytest.raises(RuntimeError, match="runtime_resources_shutting_down"):
                await resources.run_provider_io(lambda: None)
            release.set()
            assert await task == "done"
            assert await resources.drain(("provider_io",), timeout_seconds=0.01)
        finally:
            release.set()
            resources.close()

    asyncio.run(scenario())


def test_runtime_resources_allow_bounded_cleanup_after_business_admission_stops():
    async def scenario() -> None:
        resources = RuntimeResources()
        resources.begin_shutdown()
        try:
            with pytest.raises(RuntimeError, match="runtime_resources_shutting_down"):
                await resources.run_provider_io(lambda: "business")
            assert await resources.run_provider_cleanup(lambda: "closed") == "closed"
            assert await resources.run_model_cleanup(lambda: "closed") == "closed"
        finally:
            resources.close()

    asyncio.run(scenario())
