from __future__ import annotations

import asyncio
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
            _Candidate(ProjectionShard("profile", "1h:all", 100, 1), ran, stop_event),
        )
        telemetry = TelemetryRegistry()
        clock = iter((500, 501))
        monkeypatch.setattr(projection_edf_module, "_now_ms", lambda: next(clock))
        await run_projection_edf(candidates, stop_event=stop_event, telemetry=telemetry)
        assert ran == ["1h:all"]
        return telemetry.render_prometheus_text()

    metrics = asyncio.run(scenario())
    assert 'tracefold_worker_projection_deadline_misses_total{domain="profile",worker="projection_edf"} 1.0' in metrics


def test_projection_edf_rereads_all_domains_after_each_productive_turn() -> None:
    class _Backlog:
        def __init__(self, domain: str, deadlines: list[int], order: int) -> None:
            self.domain = domain
            self.deadlines = deadlines
            self.order = order

        async def peek(self, *, now_ms: int) -> ProjectionShard | None:
            del now_ms
            if not self.deadlines:
                return None
            return ProjectionShard(self.domain, self.domain, self.deadlines[0], self.order)

        async def execute(self, shard: ProjectionShard) -> bool:
            ran.append(f"{shard.domain}:{self.deadlines.pop(0)}")
            if len(ran) == 4:
                stop_event.set()
            return True

    async def scenario() -> None:
        await run_projection_edf(
            (
                _Backlog("news", [100, 400], 10),
                _Backlog("profile", [200], 20),
                _Backlog("macro", [300], 30),
            ),
            stop_event=stop_event,
            telemetry=TelemetryRegistry(),
        )

    stop_event = asyncio.Event()
    ran: list[str] = []

    asyncio.run(scenario())

    assert ran == ["news:100", "profile:200", "macro:300", "news:400"]


def test_productive_projection_repolls_without_idle_delay(monkeypatch) -> None:
    class _BackloggedCandidate:
        def __init__(self, stop_event: asyncio.Event) -> None:
            self.stop_event = stop_event
            self.turns = 0

        async def peek(self, *, now_ms: int) -> ProjectionShard:
            del now_ms
            return ProjectionShard("profile", "1h:all", 100, 1)

        async def execute(self, shard: ProjectionShard) -> bool:
            del shard
            self.turns += 1
            if self.turns == 2:
                self.stop_event.set()
            return True

    async def scenario() -> tuple[int, list[float]]:
        stop_event = asyncio.Event()
        candidate = _BackloggedCandidate(stop_event)
        waits: list[float] = []

        async def record_wait(_stop_event: asyncio.Event, seconds: float) -> None:
            waits.append(seconds)

        monkeypatch.setattr(projection_edf_module, "_wait_or_stop", record_wait)
        await run_projection_edf(
            (candidate,),
            stop_event=stop_event,
            telemetry=TelemetryRegistry(),
        )
        return candidate.turns, waits

    turns, waits = asyncio.run(scenario())

    assert turns == 2
    assert waits == []


def test_nonproductive_projection_uses_idle_delay(monkeypatch) -> None:
    class _NoProgressCandidate:
        async def peek(self, *, now_ms: int) -> ProjectionShard:
            del now_ms
            return ProjectionShard("profile", "1h:all", 100, 1)

        async def execute(self, shard: ProjectionShard) -> bool:
            del shard
            stop_event.set()
            return False

    async def scenario() -> list[float]:
        waits: list[float] = []

        async def record_wait(_stop_event: asyncio.Event, seconds: float) -> None:
            waits.append(seconds)

        monkeypatch.setattr(projection_edf_module, "_wait_or_stop", record_wait)
        await run_projection_edf(
            (_NoProgressCandidate(),),
            stop_event=stop_event,
            telemetry=TelemetryRegistry(),
        )
        return waits

    stop_event = asyncio.Event()

    assert asyncio.run(scenario()) == [0.250]


def test_retained_worker_metric_names_and_labels_have_no_framework_dependency() -> None:
    telemetry = TelemetryRegistry()
    telemetry.record_transaction_seconds("projection_edf", 0.1)
    telemetry.set_projection_rows("projection_edf", "source", 10)
    telemetry.set_projection_bytes("news_brief_current", "output", 20_480)
    telemetry.record_projection_cache("projection_edf", "hit")
    telemetry.set_queue_oldest_delay_seconds("news_acquisition", "sources", 2.0)

    metrics = telemetry.render_prometheus_text()
    assert 'tracefold_worker_transaction_seconds_count{worker="projection_edf"} 1.0' in metrics
    assert 'tracefold_worker_projection_rows{stage="source",worker="projection_edf"} 10.0' in metrics
    assert 'tracefold_worker_projection_bytes{direction="output",worker="news_brief_current"} 20480.0' in metrics
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
