from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import replace

from tracefold.platform.workers.projection_candidate import (
    ProjectionCandidate,
    ProjectionShard,
)
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class SteadyProjectionCoordinator(WorkerBase):
    """Stateless EDF arbiter over typed domain projection candidates."""

    def __init__(
        self,
        *,
        settings: object,
        candidates: Sequence[ProjectionCandidate],
        telemetry: object,
        db: object | None = None,
        now_ms: Callable[[], int] | None = None,
        name: str = "steady_projection_coordinator",
    ) -> None:
        super().__init__(
            name=name,
            settings=settings,
            db=db,
            telemetry=telemetry,
        )
        self.candidates = tuple(candidates)
        self._now_ms = now_ms or _now_ms

    def bind_runtime_resources(self, resources: object) -> None:
        super().bind_runtime_resources(resources)
        for candidate in self.candidates:
            worker = getattr(candidate, "worker", None)
            if isinstance(worker, WorkerBase):
                worker.bind_runtime_resources(resources)

    def bind_provider_governor(self, governor: object) -> None:
        super().bind_provider_governor(governor)
        for candidate in self.candidates:
            worker = getattr(candidate, "worker", None)
            if isinstance(worker, WorkerBase):
                worker.bind_provider_governor(governor)

    async def on_start(self) -> None:
        for candidate in self.candidates:
            hook = getattr(candidate, "on_start", None)
            if callable(hook):
                await hook()

    async def on_stop(self) -> None:
        for candidate in self.candidates:
            hook = getattr(candidate, "on_stop", None)
            if callable(hook):
                await hook()

    async def on_close(self) -> None:
        for candidate in self.candidates:
            hook = getattr(candidate, "aclose", None)
            if callable(hook):
                await hook()

    async def run_once(self) -> WorkerResult:
        now_ms = self._now_ms()
        available: list[tuple[ProjectionShard, ProjectionCandidate]] = []
        for candidate in self.candidates:
            shard = await candidate.next_due_shard(now_ms=now_ms)
            if shard is not None:
                available.append((shard, candidate))
        if not available:
            return WorkerResult(skipped=1, notes={"reason": "no_projection_shard"})
        shard, candidate = min(
            available,
            key=lambda item: (
                item[0].deadline_at_ms,
                item[0].stable_order,
                item[0].domain,
                item[0].shard_key,
            ),
        )
        result = await candidate.run_shard(shard)
        completed_at_ms = self._now_ms()
        return replace(
            result,
            notes={
                **result.notes,
                "projection_domain": shard.domain,
                "projection_deadline_at_ms": shard.deadline_at_ms,
                "projection_completed_at_ms": completed_at_ms,
                "projection_deadline_lag_ms": max(
                    0,
                    completed_at_ms - shard.deadline_at_ms,
                ),
            },
        )

    def next_iteration_delay_seconds(
        self,
        *,
        result: WorkerResult,
        duration_seconds: float,
    ) -> float:
        if result.processed or result.failed or result.dead:
            return 0.0
        return super().next_iteration_delay_seconds(
            result=result,
            duration_seconds=duration_seconds,
        )


def _now_ms() -> int:
    return int(time.time() * 1000)
