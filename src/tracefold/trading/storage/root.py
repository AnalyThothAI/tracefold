"""Concrete Trading repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any

from .cases import CaseStorage
from .control import ControlStorage
from .orders import OrderStorage
from .queries import QueryStorage


class TradingRepository(ControlStorage, CaseStorage, OrderStorage, QueryStorage):
    """Connection-bound persistence facade; callers continue to own transactions."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn


__all__ = ["TradingRepository"]
