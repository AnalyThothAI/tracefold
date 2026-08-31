"""Concrete Trading repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any, Protocol

from .cases import CaseStorage
from .catalog import CatalogStorage
from .execution_stream import ExecutionStreamStorage
from .gate import CandidateGateStorage
from .lane import LaneStorage
from .queries import QueryStorage


class TradingRepository(
    ExecutionStreamStorage,
    CatalogStorage,
    CandidateGateStorage,
    CaseStorage,
    LaneStorage,
    QueryStorage,
):
    """Connection-bound persistence facade; callers continue to own transactions."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn


class TradingRepositories(Protocol):
    """The Trading callback capability; deliberately no raw connection or News repository."""

    @property
    def trading(self) -> TradingRepository: ...


__all__ = ["TradingRepositories", "TradingRepository"]
