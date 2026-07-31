from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any

from tracefold.app.projection_coordinator import SteadyProjectionCoordinator
from tracefold.app.repositories import repositories_for_connection
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.workers.projection_candidate import ProjectionShard
from tracefold.platform.workers.worker_result import WorkerResult


class _FakeConnection:
    @contextmanager
    def transaction(self) -> Any:
        yield


class _ProjectionCandidate:
    async def next_due_shard(self, *, now_ms: int) -> ProjectionShard:
        return ProjectionShard(
            domain="profile",
            shard_key="Asset:asset:test",
            deadline_at_ms=now_ms,
            stable_order=1,
        )

    async def run_shard(self, _shard: ProjectionShard) -> WorkerResult:
        return WorkerResult(
            processed=1,
            notes={
                "source_rows": 100,
                "candidate_rows": 10,
                "hydrated_rows": 5,
                "rows_written": 2,
                "projection_status": "unchanged_input",
            },
        )


def test_worker_metrics_cover_transactions_amplification_cache_and_queue_age() -> None:
    telemetry = TelemetryRegistry()
    repos = repositories_for_connection(
        _FakeConnection(),
        transaction_observer=lambda seconds: telemetry.record_transaction_seconds(
            "projection",
            seconds,
        ),
    )
    with repos.transaction():
        pass

    clock = iter((100, 225))
    worker = SteadyProjectionCoordinator(
        name="projection",
        candidates=(_ProjectionCandidate(),),
        telemetry=telemetry,
        now_ms=lambda: next(clock),
    )
    asyncio.run(worker._run_iteration())

    metrics = telemetry.render_prometheus_text()
    assert 'tracefold_worker_transaction_seconds_count{worker="projection"} 1.0' in metrics
    assert 'tracefold_worker_projection_rows{stage="source",worker="projection"} 100.0' in metrics
    assert 'tracefold_worker_projection_rows{stage="hydrated",worker="projection"} 5.0' in metrics
    assert 'tracefold_worker_projection_cache_total{outcome="hit",worker="projection"} 1.0' in metrics
    assert 'tracefold_worker_projection_deadline_misses_total{domain="profile",worker="projection"} 1.0' in metrics
