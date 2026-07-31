from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProjectionShard:
    domain: str
    shard_key: str
    deadline_at_ms: int
    stable_order: int


class ProjectionCandidate(Protocol):
    async def peek(self, *, now_ms: int) -> ProjectionShard | None: ...

    async def execute(self, shard: ProjectionShard) -> bool: ...


__all__ = ["ProjectionCandidate", "ProjectionShard"]
