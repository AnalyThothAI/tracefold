"""Concrete News repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any

from .chain_tape import ChainTapeStorage
from .decisions import DecisionStorage
from .events import EventStorage
from .feed import FeedStorage
from .learning import LearningStorage
from .market import MarketStorage
from .operations import OperationsStorage
from .trade_projection import TradeProjectionStorage


class NewsRepository(
    OperationsStorage,
    EventStorage,
    DecisionStorage,
    MarketStorage,
    ChainTapeStorage,
    TradeProjectionStorage,
    LearningStorage,
    FeedStorage,
):
    def __init__(self, conn: Any) -> None:
        self.conn = conn


__all__ = ["NewsRepository"]
