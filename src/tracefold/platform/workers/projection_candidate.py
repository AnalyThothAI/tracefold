from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tracefold.platform.workers.worker_result import WorkerResult


@dataclass(frozen=True, slots=True)
class ProjectionShard:
    domain: str
    shard_key: str
    deadline_at_ms: int
    stable_order: int


class ProjectionCandidate(Protocol):
    async def next_due_shard(
        self,
        *,
        now_ms: int,
    ) -> ProjectionShard | None: ...

    async def run_shard(self, shard: ProjectionShard) -> WorkerResult: ...


__all__ = ["ProjectionCandidate", "ProjectionShard"]
