from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from tracefold.platform.workers.worker_base import (
    WorkerBase,
    _successful_iteration_delay,
)
from tracefold.platform.workers.worker_result import WorkerResult


def test_successful_worker_cadence_targets_start_to_start_interval() -> None:
    assert _successful_iteration_delay(interval_seconds=10, duration_seconds=2.5) == 7.5


def test_overrun_worker_skips_missed_ticks_and_waits_for_the_next_cadence_boundary() -> None:
    assert _successful_iteration_delay(interval_seconds=10, duration_seconds=12) == 8
    assert _successful_iteration_delay(interval_seconds=10, duration_seconds=20) == 10
    assert _successful_iteration_delay(interval_seconds=0.25, duration_seconds=2) == 0.25


def test_shared_iteration_gate_bounds_background_worker_concurrency() -> None:
    async def scenario() -> None:
        active = 0
        max_active = 0
        first_entered = asyncio.Event()
        release = asyncio.Event()

        async def iteration() -> WorkerResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            first_entered.set()
            await release.wait()
            active -= 1
            return WorkerResult(processed=1)

        gate = asyncio.Semaphore(1)
        first = _GatedWorker(name="first", iteration=iteration)
        second = _GatedWorker(name="second", iteration=iteration)
        first.set_iteration_gate(gate)
        second.set_iteration_gate(gate)

        first_task = asyncio.create_task(first.run_one_iteration())
        await first_entered.wait()
        second_task = asyncio.create_task(second.run_one_iteration())
        await asyncio.sleep(0)

        assert max_active == 1
        assert not second_task.done()

        release.set()
        await asyncio.gather(first_task, second_task)
        assert max_active == 1

    asyncio.run(scenario())


class _GatedWorker(WorkerBase):
    def __init__(self, *, name: str, iteration: Any) -> None:
        super().__init__(
            name=name,
            settings=SimpleNamespace(
                enabled=True,
                interval_seconds=1.0,
                backoff=SimpleNamespace(base_ms=1, max_ms=1),
            ),
            db=None,
            telemetry=None,
        )
        self.iteration = iteration

    async def run_once(self) -> WorkerResult:
        return await self.iteration()
