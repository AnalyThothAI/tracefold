from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

from tracefold.app.projection_edf import run_projection_edf
from tracefold.market.radar.projection_worker import RadarProjectionCandidate
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.projection import ProjectionShard

_COMPLETED_STAGE_SECONDS = 0.75


def test_radar_projection_allows_cumulative_in_budget_stages_to_exceed_five_seconds() -> None:
    class _CompletedDatabase:
        def __init__(self) -> None:
            self.claim = SimpleNamespace(targets=(object(),))
            self.calls: list[str] = []

        async def run_business(
            self,
            operation_name: str,
            *_args: Any,
            on_submitted=None,
            **kwargs: Any,
        ) -> Any:
            self.calls.append(operation_name)
            if operation_name == "radar_projection_claim":
                return self.claim
            assert float(kwargs["operation_timeout_seconds"]) > _COMPLETED_STAGE_SECONDS
            if on_submitted is not None:
                on_submitted()
            await asyncio.sleep(_COMPLETED_STAGE_SECONDS)
            return {
                "radar_projection_load": {"targets": []},
                "radar_projection_rank_input": {},
                "radar_projection_hydration_input": {},
            }.get(operation_name)

    class _CompletedCpu:
        async def run(
            self,
            operation_name: str,
            *_args: Any,
            service_timeout_seconds: float,
            on_submitted=None,
            **_kwargs: Any,
        ) -> Any:
            assert service_timeout_seconds > _COMPLETED_STAGE_SECONDS
            if on_submitted is not None:
                on_submitted()
            await asyncio.sleep(_COMPLETED_STAGE_SECONDS)
            return {
                "radar_projection_features": {"targets": []},
                "radar_projection_rank": [],
                "radar_projection_hydration": {},
            }[operation_name]

    async def scenario() -> tuple[bool, list[str], float, str]:
        database = _CompletedDatabase()
        candidate = RadarProjectionCandidate(
            db=database,
            cpu=_CompletedCpu(),
            runtime_id="runtime-1",
        )
        stop_event = asyncio.Event()
        completed = False
        shard = ProjectionShard(
            domain="radar",
            shard_key='{"venue":"all","window":"1h"}',
            deadline_at_ms=1_000,
            stable_order=10,
        )

        class _OneTurnCandidate:
            async def peek(self, *, now_ms: int) -> ProjectionShard:
                del now_ms
                return shard

            async def execute(self, selected_shard: ProjectionShard) -> bool:
                nonlocal completed
                completed = await candidate.execute(selected_shard)
                stop_event.set()
                return completed

        telemetry = TelemetryRegistry()
        started = time.monotonic()
        await run_projection_edf(
            (_OneTurnCandidate(),),
            stop_event=stop_event,
            telemetry=telemetry,
        )
        return completed, database.calls, time.monotonic() - started, telemetry.render_prometheus_text()

    completed, calls, elapsed_seconds, metrics = asyncio.run(scenario())

    assert completed is True
    assert calls[-1] == "radar_projection_publish"
    assert elapsed_seconds > 5.0
    assert 'tracefold_worker_projection_soft_slo_overruns_total{domain="radar"} 1.0' in metrics
