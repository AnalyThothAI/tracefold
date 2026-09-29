"""Concrete Trading repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any, Protocol

from .analysis import AnalysisStorage
from .executor import ExecutorStorage
from .history import HistoricalCaseStorage
from .scoreboard import ScoreboardStorage


class TradingRepository(
    ExecutorStorage,
    AnalysisStorage,
    HistoricalCaseStorage,
    ScoreboardStorage,
):
    """Connection-bound persistence facade; callers continue to own transactions."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn


class TradingRepositories(Protocol):
    """The Trading callback capability; deliberately no raw connection or News repository."""

    @property
    def trading(self) -> TradingRepository: ...


__all__ = ["TradingRepositories", "TradingRepository"]
