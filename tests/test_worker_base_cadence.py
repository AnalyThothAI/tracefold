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


def test_worker_base_has_no_retired_shared_iteration_gate() -> None:
    worker = _GatedWorker(
        name="ungated",
        iteration=lambda: asyncio.sleep(0, result=WorkerResult(processed=1)),
    )

    assert not hasattr(worker, "set_iteration_gate")
    assert not hasattr(worker, "_iteration_gate")


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
