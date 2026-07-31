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
        candidates: Sequence[ProjectionCandidate],
        telemetry: object,
        now_ms: Callable[[], int] | None = None,
        name: str = "steady_projection_coordinator",
    ) -> None:
        super().__init__(
            name=name,
            interval_seconds=0.05,
            telemetry=telemetry,
        )
        self.candidates = tuple(candidates)
        self._now_ms = now_ms or _now_ms

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
        completed = replace(
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
        self._record_projection_metrics(completed.notes)
        return completed

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

    def _record_projection_metrics(self, notes: dict[str, object]) -> None:
        if self.telemetry is None:
            return
        stages = {
            "source": _first_metric(
                notes,
                "source_rows",
                "source_rows_scanned",
            ),
            "candidate": _first_metric(
                notes,
                "candidate_rows",
                "items",
                "targets_loaded",
            ),
            "hydrated": _first_metric(notes, "hydrated_rows"),
            "written": _first_metric(notes, "rows_written"),
        }
        for stage, value in stages.items():
            if value is not None:
                self.telemetry.set_projection_rows(
                    self.name,
                    stage,
                    value,
                )
        projection_status = str(notes.get("projection_status") or "").strip().lower()
        if projection_status:
            outcome = {
                "unchanged_input": "hit",
                "rebuilt": "miss",
                "stale_snapshot": "stale",
            }.get(projection_status, projection_status)
            self.telemetry.record_projection_cache(
                self.name,
                outcome,
            )
        deadline_lag_ms = _first_metric(
            notes,
            "projection_deadline_lag_ms",
        )
        if deadline_lag_ms is not None and deadline_lag_ms > 0:
            self.telemetry.record_projection_deadline_miss(
                self.name,
                str(notes.get("projection_domain") or "unknown"),
            )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _first_metric(
    notes: dict[str, object],
    *keys: str,
) -> int | None:
    for key in keys:
        value = notes.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return max(0, int(value))
    return None
