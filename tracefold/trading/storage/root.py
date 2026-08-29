"""Concrete Trading repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any

from .capabilities import CapabilityStorage
from .cases import CaseStorage
from .catalog import CatalogStorage
from .control import ControlStorage
from .gate import CandidateGateStorage
from .intents import IntentStorage
from .lane import LaneStorage
from .queries import QueryStorage
from .replay import ReplayStorage


class TradingRepository(
    ControlStorage,
    CatalogStorage,
    CapabilityStorage,
    CandidateGateStorage,
    CaseStorage,
    IntentStorage,
    LaneStorage,
    QueryStorage,
    ReplayStorage,
):
    """Connection-bound persistence facade; callers continue to own transactions."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn


__all__ = ["TradingRepository"]
