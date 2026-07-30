from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from tracefold.app.repositories import repositories_for_connection
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class _FakeConnection:
    @contextmanager
    def transaction(self) -> Any:
        yield


class _ProjectionWorker(WorkerBase):
    async def run_once(self) -> WorkerResult:
        return WorkerResult(
            processed=1,
            notes={
                "source_rows": 100,
                "candidate_rows": 10,
                "hydrated_rows": 5,
                "rows_written": 2,
                "projection_status": "unchanged_input",
                "merged_rank_set_triggers": 3,
                "queue_depth": 7,
                "oldest_due_age_ms": 2_500,
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

    worker = _ProjectionWorker(
        name="projection",
        settings=SimpleNamespace(
            enabled=True,
            interval_seconds=10,
            backoff=SimpleNamespace(base_ms=100, max_ms=1_000),
        ),
        db=None,
        telemetry=telemetry,
    )
    asyncio.run(worker.run_one_iteration())

    metrics = telemetry.render_prometheus_text()
    assert 'tracefold_worker_transaction_seconds_count{worker="projection"} 1.0' in metrics
    assert 'tracefold_worker_projection_rows{stage="source",worker="projection"} 100.0' in metrics
    assert 'tracefold_worker_projection_rows{stage="hydrated",worker="projection"} 5.0' in metrics
    assert 'tracefold_worker_projection_cache_total{outcome="hit",worker="projection"} 1.0' in metrics
    assert 'tracefold_worker_projection_merged_total{worker="projection"} 3.0' in metrics
    assert 'tracefold_worker_queue_oldest_delay_seconds{queue="primary",worker="projection"} 2.5' in metrics
