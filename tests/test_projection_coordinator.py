import asyncio
from dataclasses import dataclass

import pytest

from tracefold.app.projection_coordinator import SteadyProjectionCoordinator
from tracefold.platform.config.settings import PerWorkerSettings
from tracefold.platform.workers.projection_candidate import ProjectionShard
from tracefold.platform.workers.worker_result import WorkerResult


@dataclass
class _Candidate:
    shard: ProjectionShard | None
    ran: list[str]

    async def next_due_shard(self, *, now_ms: int) -> ProjectionShard | None:
        return self.shard

    async def run_shard(self, shard: ProjectionShard) -> WorkerResult:
        self.ran.append(shard.shard_key)
        self.shard = None
        return WorkerResult(processed=1)


def test_projection_coordinator_runs_one_earliest_deadline_shard_with_stable_tie_break():
    async def scenario() -> None:
        ran: list[str] = []
        candidates = (
            _Candidate(ProjectionShard("macro", "rates_fed", 200, 2), ran),
            _Candidate(ProjectionShard("news", "bucket:1", 100, 2), ran),
            _Candidate(ProjectionShard("radar", "5m:all", 100, 1), ran),
        )
        coordinator = SteadyProjectionCoordinator(
            settings=PerWorkerSettings(interval_seconds=0),
            candidates=candidates,
            telemetry=None,
            now_ms=lambda: 500,
        )

        result = await coordinator.run_once()

        assert ran == ["5m:all"]
        assert result.processed == 1
        assert result.notes["projection_deadline_lag_ms"] == 400

    asyncio.run(scenario())


def test_projection_coordinator_runs_an_already_eligible_shard_before_its_deadline():
    async def scenario() -> None:
        ran: list[str] = []
        candidate = _Candidate(
            ProjectionShard("profile", "Asset:asset:test", 30_000, 1),
            ran,
        )
        coordinator = SteadyProjectionCoordinator(
            settings=PerWorkerSettings(interval_seconds=0),
            candidates=(candidate,),
            telemetry=None,
            now_ms=lambda: 1_000,
        )

        result = await coordinator.run_once()

        assert ran == ["Asset:asset:test"]
        assert result.processed == 1
        assert result.notes["projection_deadline_lag_ms"] == 0

    asyncio.run(scenario())


def test_projection_coordinator_drains_completed_work_without_cadence_sleep() -> None:
    coordinator = SteadyProjectionCoordinator(
        settings=PerWorkerSettings(interval_seconds=0.05),
        candidates=(),
        telemetry=None,
    )

    assert (
        coordinator.next_iteration_delay_seconds(
            result=WorkerResult(processed=1),
            duration_seconds=0.012,
        )
        == 0
    )
    assert (
        coordinator.next_iteration_delay_seconds(
            result=WorkerResult(failed=1),
            duration_seconds=0.012,
        )
        == 0
    )


def test_projection_coordinator_keeps_bounded_idle_poll_cadence() -> None:
    coordinator = SteadyProjectionCoordinator(
        settings=PerWorkerSettings(interval_seconds=0.05),
        candidates=(),
        telemetry=None,
    )

    assert coordinator.next_iteration_delay_seconds(
        result=WorkerResult(skipped=1),
        duration_seconds=0.012,
    ) == pytest.approx(0.038)
