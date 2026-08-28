"""Concrete Trading repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any

from .capabilities import CapabilityStorage
from .cases import CaseStorage
from .control import ControlStorage
from .evaluations import EvaluationStorage
from .gate import CandidateGateStorage
from .intents import IntentStorage
from .manual import ManualStorage
from .queries import QueryStorage
from .replay import ReplayStorage


class TradingRepository(
    ControlStorage,
    CapabilityStorage,
    CandidateGateStorage,
    CaseStorage,
    EvaluationStorage,
    IntentStorage,
    ManualStorage,
    QueryStorage,
    ReplayStorage,
):
    """Connection-bound persistence facade; callers continue to own transactions."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn


__all__ = ["TradingRepository"]
