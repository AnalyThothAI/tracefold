"""Stable value and port contracts for the News bounded context.

App owns composition. Policies, evaluators, persistence, review workflows, runtime helpers, and
configuration constants remain under their owning modules instead of becoming an accidental API here.
"""

from __future__ import annotations

from .card_format import LINKABLE_TICKER_RE
from .card_format import clock as card_clock
from .delivery_contracts import COMMIT_PHASE_NOT_SENT, COMMIT_PHASE_UNKNOWN
from .market_contracts import MARKET_PAGE_MAX, MARKET_WINDOW_DEFAULT_MS, MARKET_WINDOW_MAX_MS
from .models import (
    ReaderDeliveryPresentation,
    ReaderMarketMovement,
    ReaderTradeTarget,
    TelegramDeliveryReceipt,
)
from .oi_contracts import OI_METRIC_VERSION
from .opennews import OpenNewsExpectedError
from .program.contracts import (
    ProgramTrace,
    ProgramUsage,
    SemanticJudge,
    SemanticJudgeError,
    SemanticJudgment,
    TradeRelevanceV1,
    TriageContext,
)
from .progression_review import PROGRESSION_REVIEW_TIMEOUT_SECONDS, ProgressionVerifier
from .reader_card import NOVELTY_ZH, UNTRADEABLE_NOTICE_ZH, ReaderCard, quote_line
from .source_contracts import EVENT_KINDS, MARKET_KINDS, EventKind
from .taxonomy import (
    ASSERTION_STATUSES,
    CHANGE_STATES,
    EVENT_FAMILIES,
    IPTC_SUBJECT_CODES,
    SOURCE_AUTHORITIES,
    IPTCCodebookSha,
    NewsTaxonomyV1,
    SourceAuthority,
    source_authority_from_evidence,
)
from .told_context import NEWS_RETRIEVAL_SHA256

__all__ = [
    "ASSERTION_STATUSES",
    "CHANGE_STATES",
    "COMMIT_PHASE_NOT_SENT",
    "COMMIT_PHASE_UNKNOWN",
    "EVENT_FAMILIES",
    "EVENT_KINDS",
    "IPTC_SUBJECT_CODES",
    "LINKABLE_TICKER_RE",
    "MARKET_KINDS",
    "MARKET_PAGE_MAX",
    "MARKET_WINDOW_DEFAULT_MS",
    "MARKET_WINDOW_MAX_MS",
    "NEWS_RETRIEVAL_SHA256",
    "NOVELTY_ZH",
    "OI_METRIC_VERSION",
    "PROGRESSION_REVIEW_TIMEOUT_SECONDS",
    "SOURCE_AUTHORITIES",
    "UNTRADEABLE_NOTICE_ZH",
    "EventKind",
    "IPTCCodebookSha",
    "NewsTaxonomyV1",
    "OpenNewsExpectedError",
    "ProgramTrace",
    "ProgramUsage",
    "ProgressionVerifier",
    "ReaderCard",
    "ReaderDeliveryPresentation",
    "ReaderMarketMovement",
    "ReaderTradeTarget",
    "SemanticJudge",
    "SemanticJudgeError",
    "SemanticJudgment",
    "SourceAuthority",
    "TelegramDeliveryReceipt",
    "TradeRelevanceV1",
    "TriageContext",
    "card_clock",
    "quote_line",
    "source_authority_from_evidence",
]
