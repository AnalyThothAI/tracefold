"""Concrete News repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any

from .chain_tape import ChainTapeStorage
from .decisions import DecisionStorage
from .event_updates import EventUpdateStorage
from .events import EventStorage
from .evidence import EvidenceStorage
from .feed import FeedStorage
from .market import MarketStorage
from .operations import OperationsStorage
from .trade_projection import TradeProjectionStorage
from .wallet_diagnostics import WalletDiagnosticsStorage
from .wallet_events import WalletEventStorage


class NewsRepository(
    OperationsStorage,
    EventStorage,
    EvidenceStorage,
    DecisionStorage,
    EventUpdateStorage,
    MarketStorage,
    ChainTapeStorage,
    WalletEventStorage,
    WalletDiagnosticsStorage,
    TradeProjectionStorage,
    FeedStorage,
):
    def __init__(self, conn: Any) -> None:
        self.conn = conn


__all__ = ["NewsRepository"]
