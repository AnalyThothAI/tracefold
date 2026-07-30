from __future__ import annotations

import asyncio
import time
from typing import Any

from tracefold.app.worker_scheduler import WorkerScheduler


class _FakeWorker:
    def __init__(self, name: str, started: list[tuple[str, float]]) -> None:
        self.name = name
        self.started = started
        self._stopped = asyncio.Event()

    def status_payload(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "running": False,
            "effective_status": "stopped",
            "unavailable_reason": None,
        }

    async def run(self) -> None:
        self.started.append((self.name, time.perf_counter()))
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()

    async def aclose(self) -> None:
        return None


class _FakeDB:
    async def aclose(self) -> None:
        return None


def test_scheduler_starts_realtime_before_analysis_and_background_workers() -> None:
    async def scenario() -> None:
        started: list[tuple[str, float]] = []
        workers = {name: _FakeWorker(name, started) for name in ("asset_profile_refresh", "news_pipeline", "collector")}
        scheduler = WorkerScheduler(
            workers=workers,
            db=_FakeDB(),
            startup_phase_delays_seconds={0: 0, 1: 0.02, 2: 0.04},
            startup_stagger_seconds=0,
        )

        await scheduler.start()
        await asyncio.sleep(0)
        assert [name for name, _started_at in started] == ["collector"]

        await asyncio.sleep(0.025)
        assert [name for name, _started_at in started] == ["collector", "news_pipeline"]

        await asyncio.sleep(0.025)
        assert [name for name, _started_at in started] == [
            "collector",
            "news_pipeline",
            "asset_profile_refresh",
        ]
        await scheduler.stop()

    asyncio.run(scenario())


def test_scheduler_stop_interrupts_long_startup_delay() -> None:
    async def scenario() -> None:
        started: list[tuple[str, float]] = []
        scheduler = WorkerScheduler(
            workers={"asset_profile_refresh": _FakeWorker("asset_profile_refresh", started)},
            db=_FakeDB(),
            startup_phase_delays_seconds={2: 60},
            startup_stagger_seconds=0,
        )

        await scheduler.start()
        await asyncio.wait_for(scheduler.stop(), timeout=0.1)
        assert started == []

    asyncio.run(scenario())
