"""Concrete Trading repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any, Protocol

from .analysis import AnalysisStorage
from .execution_stream import ExecutionStreamStorage
from .gate import HistoricalGateStorage
from .health import ExecutionHealthStorage
from .history import HistoricalCaseStorage
from .queries import QueryStorage
from .trade_plans import TradePlanStorage


class TradingRepository(
    AnalysisStorage,
    HistoricalGateStorage,
    ExecutionStreamStorage,
    HistoricalCaseStorage,
    QueryStorage,
    TradePlanStorage,
    ExecutionHealthStorage,
):
    """Connection-bound persistence facade; callers continue to own transactions.

    Historical admission rows remain readable; the Analysis process owns new
    Trigger and Case writes. ExecutionHealthStorage reads venue facts for the watchdog.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn


class TradingRepositories(Protocol):
    """The Trading callback capability; deliberately no raw connection or News repository."""

    @property
    def trading(self) -> TradingRepository: ...


__all__ = ["TradingRepositories", "TradingRepository"]
