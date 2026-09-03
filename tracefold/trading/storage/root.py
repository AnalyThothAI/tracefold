"""Concrete Trading repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any, Protocol

from .lane import LaneStorage
from .queries import QueryStorage


class TradingRepository(LaneStorage, QueryStorage):
    """Connection-bound persistence facade; callers continue to own transactions.

    Two bases, four modules: `LaneStorage` already carries the admission ledger and the execution
    stream, because the lane's Case and Signal writes are atomic compositions with them.
    """

    def __init__(self, conn: Any) -> None:
        self.conn = conn


class TradingRepositories(Protocol):
    """The Trading callback capability; deliberately no raw connection or News repository."""

    @property
    def trading(self) -> TradingRepository: ...


__all__ = ["TradingRepositories", "TradingRepository"]
