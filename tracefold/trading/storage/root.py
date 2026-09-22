"""Concrete Trading repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any, Protocol

from .health import ExecutionHealthStorage
from .lane import LaneStorage
from .queries import QueryStorage
from .trade_plans import TradePlanStorage


class TradingRepository(LaneStorage, QueryStorage, TradePlanStorage, ExecutionHealthStorage):
    """Connection-bound persistence facade; callers continue to own transactions.

    `LaneStorage` already carries the admission ledger and the execution stream, because the lane's
    Case and Signal writes are atomic compositions with them. `ExecutionHealthStorage` is the watchdog's
    read-only view of the execution facts (#680 RC11).
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn


class TradingRepositories(Protocol):
    """The Trading callback capability; deliberately no raw connection or News repository."""

    @property
    def trading(self) -> TradingRepository: ...


__all__ = ["TradingRepositories", "TradingRepository"]
