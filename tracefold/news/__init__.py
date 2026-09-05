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
    NewsFeedEntry,
    ReaderDeliveryPresentation,
    ReaderMarketMovement,
    ReaderMarketState,
    ReaderReceipt,
    ReaderTradeTarget,
    TelegramDeliveryReceipt,
    TriageVerdict,
)
from .oi_contracts import OI_METRIC_VERSION
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
from .progression_review import (
    PROGRESSION_REVIEW_REASON_MAX_CHARS,
    PROGRESSION_REVIEW_TIMEOUT_SECONDS,
    ProgressionVerifier,
)
from .reader_card import NOVELTY_ZH, UNTRADEABLE_NOTICE_ZH, ReaderCard, quote_line
from .source_contracts import EVENT_KINDS, MARKET_KINDS, EventKind, MarketKind
from .taxonomy import (
    ASSERTION_STATUSES,
    CHANGE_STATES,
    EVENT_FAMILIES,
    IPTC_SUBJECT_CODES,
    SOURCE_AUTHORITIES,
    IPTCCodebookSha,
    ModelTaxonomyV1,
    NewsTaxonomyV1,
    SourceAuthority,
    source_authority_from_evidence,
)
from .told_context import NEWS_RETRIEVAL_SHA256
from .tradability import (
    REQUIRED_TRADABILITY_VENUES,
    TRADABILITY_REVIEW_TIMEOUT_SECONDS,
    TradabilityMatch,
    TradabilityReview,
    TradabilityVerifier,
)

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
    "PROGRESSION_REVIEW_REASON_MAX_CHARS",
    "PROGRESSION_REVIEW_TIMEOUT_SECONDS",
    "REQUIRED_TRADABILITY_VENUES",
    "SOURCE_AUTHORITIES",
    "TRADABILITY_REVIEW_TIMEOUT_SECONDS",
    "UNTRADEABLE_NOTICE_ZH",
    "EditorialEnvelope",
    "EventKind",
    "IPTCCodebookSha",
    "MarketKind",
    "ModelTaxonomyV1",
    "NewsFeedEntry",
    "NewsTaxonomyV1",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "ProgramTrace",
    "ProgramUsage",
    "ProgressionVerifier",
    "ReaderCard",
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
    "SourceAuthority",
    "TelegramDeliveryReceipt",
    "TradabilityMatch",
    "TradabilityReview",
    "TradabilityVerifier",
    "TradeRelevanceV1",
    "TriageContext",
    "TriageVerdict",
    "card_clock",
    "quote_line",
    "source_authority_from_evidence",
]
