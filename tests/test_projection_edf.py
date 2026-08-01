from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import pytest

import tracefold.app.projection_edf as projection_edf_module
from tracefold.app.projection_edf import run_projection_edf
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.projection import ProjectionShard
from tracefold.platform.resource import ResourceAdmissionTimeout


@dataclass
class _Candidate:
    shard: ProjectionShard | None
    ran: list[str]
    stop_event: asyncio.Event

    async def peek(self, *, now_ms: int) -> ProjectionShard | None:
        del now_ms
        return self.shard

    async def execute(self, shard: ProjectionShard) -> bool:
        self.ran.append(shard.shard_key)
        self.shard = None
        self.stop_event.set()
        return True


def test_projection_edf_runs_one_earliest_deadline_with_stable_order(monkeypatch) -> None:
    async def scenario() -> str:
        stop_event = asyncio.Event()
        ran: list[str] = []
        candidates = (
            _Candidate(ProjectionShard("macro", "rates", 200, 2), ran, stop_event),
            _Candidate(ProjectionShard("news", "bucket", 100, 2), ran, stop_event),
            _Candidate(ProjectionShard("radar", "1h:all", 100, 1), ran, stop_event),
        )
        telemetry = TelemetryRegistry()
        clock = iter((500, 501))
        monkeypatch.setattr(projection_edf_module, "_now_ms", lambda: next(clock))
        await run_projection_edf(candidates, stop_event=stop_event, telemetry=telemetry)
        assert ran == ["1h:all"]
        return telemetry.render_prometheus_text()

    metrics = asyncio.run(scenario())
    assert 'tracefold_worker_projection_deadline_misses_total{domain="radar",worker="projection_edf"} 1.0' in metrics


def test_productive_projection_has_a_minimum_repoll_cadence() -> None:
    class _BackloggedCandidate:
        def __init__(self, stop_event: asyncio.Event) -> None:
            self.stop_event = stop_event
            self.started_at: list[float] = []

        async def peek(self, *, now_ms: int) -> ProjectionShard:
            del now_ms
            return ProjectionShard("radar", "1h:all", 100, 1)

        async def execute(self, shard: ProjectionShard) -> bool:
            del shard
            self.started_at.append(time.monotonic())
            if len(self.started_at) == 2:
                self.stop_event.set()
            return True

    async def scenario() -> list[float]:
        stop_event = asyncio.Event()
        candidate = _BackloggedCandidate(stop_event)
        await run_projection_edf(
            (candidate,),
            stop_event=stop_event,
            telemetry=TelemetryRegistry(),
        )
        return candidate.started_at

    started_at = asyncio.run(scenario())

    assert len(started_at) == 2
    assert started_at[1] - started_at[0] >= 0.20


def test_retained_worker_metric_names_and_labels_have_no_framework_dependency() -> None:
    telemetry = TelemetryRegistry()
    telemetry.record_transaction_seconds("projection_edf", 0.1)
    telemetry.set_projection_rows("projection_edf", "source", 10)
    telemetry.record_projection_cache("projection_edf", "hit")
    telemetry.set_queue_oldest_delay_seconds("news_acquisition", "sources", 2.0)

    metrics = telemetry.render_prometheus_text()
    assert 'tracefold_worker_transaction_seconds_count{worker="projection_edf"} 1.0' in metrics
    assert 'tracefold_worker_projection_rows{stage="source",worker="projection_edf"} 10.0' in metrics
    assert 'tracefold_worker_projection_cache_total{outcome="hit",worker="projection_edf"} 1.0' in metrics
    assert 'tracefold_worker_queue_oldest_delay_seconds{queue="sources",worker="news_acquisition"} 2.0' in metrics


def test_projection_edf_propagates_post_claim_admission_timeout() -> None:
    class _PostWorkTimeout:
        async def peek(self, *, now_ms: int) -> ProjectionShard:
            del now_ms
            return ProjectionShard("news", "claimed", 100, 1)

        async def execute(self, shard: ProjectionShard) -> bool:
            del shard
            raise ResourceAdmissionTimeout("publication_db_saturated")

    async def scenario() -> None:
        with pytest.raises(ResourceAdmissionTimeout, match="publication_db_saturated"):
            await run_projection_edf(
                (_PostWorkTimeout(),),
                stop_event=asyncio.Event(),
                telemetry=TelemetryRegistry(),
            )

    asyncio.run(scenario())
