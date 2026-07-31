from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    kind: str
    target_key: str
    due_at_ms: int
    stable_order: int


class NativeModelCandidate(Protocol):
    async def peek(self, *, now_ms: int) -> ModelCandidate | None: ...

    async def execute(self, candidate: ModelCandidate) -> bool: ...


__all__ = ["ModelCandidate", "NativeModelCandidate"]
