"""Concrete Trading repository assembled from lifecycle-owned storage modules."""

from __future__ import annotations

from typing import Any

from .authority import AuthorityStorage
from .bindings import BindingStorage
from .capabilities import CapabilityStorage
from .cases import CaseStorage
from .catalog import CatalogStorage
from .control import ControlStorage
from .evidence import EvidenceStorage
from .gate import CandidateGateStorage
from .intents import IntentStorage
from .lane import LaneStorage
from .queries import QueryStorage
from .replay import ReplayStorage
from .verification import VerificationStorage


class TradingRepository(
    ControlStorage,
    EvidenceStorage,
    AuthorityStorage,
    BindingStorage,
    CatalogStorage,
    CapabilityStorage,
    CandidateGateStorage,
    CaseStorage,
    IntentStorage,
    LaneStorage,
    QueryStorage,
    ReplayStorage,
    VerificationStorage,
):
    """Connection-bound persistence facade; callers continue to own transactions."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn


__all__ = ["TradingRepository"]
