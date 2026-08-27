"""Stable value and port contracts for the News bounded context.

App owns composition. Policies, evaluators, persistence, review workflows, runtime helpers, and
configuration constants remain under their owning modules instead of becoming an accidental API here.
"""

from __future__ import annotations

from .models import (
    NewsFeedEntry,
    ReaderDeliveryPresentation,
    ReaderMarketMovement,
    ReaderMarketState,
    ReaderReceipt,
    ReaderTradeTarget,
    TriageVerdict,
)
from .opennews import (
    OpenNewsEvent,
    OpenNewsExpectedError,
    OpenNewsHistoryError,
    OpenNewsStrategyHistory,
)
from .program.contracts import (
    EditorialEnvelope,
    ProgramTrace,
    ProgramUsage,
    ReaderCardSemanticView,
    ScoredJudgment,
    SemanticJudge,
    SemanticJudgeError,
    SemanticJudgment,
    TradeRelevanceV1,
    TriageContext,
)
from .source_contracts import EVENT_KINDS, EventKind, SourceContractReason
from .told_context import NEWS_RETRIEVAL_SHA256

__all__ = [
    "EVENT_KINDS",
    "NEWS_RETRIEVAL_SHA256",
    "EditorialEnvelope",
    "EventKind",
    "NewsFeedEntry",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "ProgramTrace",
    "ProgramUsage",
    "ReaderCardSemanticView",
    "ReaderDeliveryPresentation",
    "ReaderMarketMovement",
    "ReaderMarketState",
    "ReaderReceipt",
    "ReaderTradeTarget",
    "ScoredJudgment",
    "SemanticJudge",
    "SemanticJudgeError",
    "SemanticJudgment",
    "SourceContractReason",
    "TradeRelevanceV1",
    "TriageContext",
    "TriageVerdict",
]
