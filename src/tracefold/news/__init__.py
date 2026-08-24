"""Stable value and port contracts for the News bounded context.

App owns composition. Policies, evaluators, persistence, review workflows, runtime helpers, and
configuration constants remain under their owning modules instead of becoming an accidental API here.
"""

from __future__ import annotations

from .models import NewsFeedEntry, ReaderReceipt, TriageVerdict
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

__all__ = [
    "EditorialEnvelope",
    "NewsFeedEntry",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "ProgramTrace",
    "ProgramUsage",
    "ReaderCardSemanticView",
    "ReaderReceipt",
    "ScoredJudgment",
    "SemanticJudge",
    "SemanticJudgeError",
    "SemanticJudgment",
    "TradeRelevanceV1",
    "TriageContext",
    "TriageVerdict",
]
