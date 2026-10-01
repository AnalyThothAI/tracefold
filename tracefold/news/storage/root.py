"""Concrete News repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any

from .chain_tape import ChainTapeStorage
from .decisions import DecisionStorage
from .events import EventStorage
from .evidence import EvidenceStorage
from .feed import FeedStorage
from .head_scope_repairs import HeadScopeRepairStorage
from .judgment_cache import JudgmentCacheStorage
from .market import MarketStorage
from .notification_context import NotificationContextStorage
from .notification_delivery import NotificationDeliveryStorage
from .notification_work import NotificationWorkStorage
from .operations import OperationsStorage
from .semantic_input import SemanticInputStorage
from .semantic_updates import SemanticUpdateStorage
from .semantic_work import SemanticWorkStorage
from .trade_projection import TradeProjectionStorage
from .wallet_diagnostics import WalletDiagnosticsStorage
from .wallet_events import WalletEventStorage


class NewsRepository(
    OperationsStorage,
    EventStorage,
    EvidenceStorage,
    DecisionStorage,
    MarketStorage,
    ChainTapeStorage,
    WalletEventStorage,
    WalletDiagnosticsStorage,
    TradeProjectionStorage,
    FeedStorage,
):
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.semantic_work = SemanticWorkStorage(conn)
        self.semantic_updates = SemanticUpdateStorage(conn, work=self.semantic_work, outbox=self)
        self.semantic_input = SemanticInputStorage(
            conn, evidence=self, head_document=self.semantic_updates.event_update_head_document
        )
        self.notification_context = NotificationContextStorage(conn, updates=self.semantic_updates)
        self.notification_work = NotificationWorkStorage(conn, context=self.notification_context)
        self.notification_delivery = NotificationDeliveryStorage(
            conn, context=self.notification_context, work=self.notification_work
        )
        self.judgment_cache = JudgmentCacheStorage(conn)
        self.head_scope_repairs = HeadScopeRepairStorage(conn, outbox=self)


__all__ = ["NewsRepository"]
