"""Trading domain models: the typed vocabulary the whole bounded context shares.

Everything here is pure data. No provider payload, no credential, no database handle, no clock. The
package depends on `platform` and third-party libraries only, so these shapes are also the seam the
composition root converts News projections into.

Two conventions carried over from News on purpose:

* thresholds are integer basis points, so a stored number and the comparison against it cannot
  disagree because of a float;
* money and quantities are `Decimal`, never `float`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bumped whenever the manifest layout, the regime arithmetic or the pure policy changes shape: a case
# frozen under one version is not comparable with a case frozen under another.
TRADING_MANIFEST_VERSION = "trading_manifest_v2"
TRADING_POLICY_VERSION = "trading_policy_v1"
TRADING_PROGRAM_VERSION = "trading_news_oi_decision_v1"
# Code-owned execution timing shared by the pipeline and the one-attempt protocol.
TRADING_COLD_WRITE_TIMEOUT_SECONDS = 10.0
TRADING_RECONCILE_BACKOFF_MS = 30_000

# Trading consumes one explicit News generation. This is an upstream input contract, not a fallback:
# a case frozen under an older News Program/policy is terminal audit history after #160's hard cut.
NewsLearningEpoch = Literal["program_v6"]

TradingMode = Literal["paper", "live_reviewed", "live_bounded"]
ControlState = Literal["RUNNING", "CLOSE_ONLY", "PAUSED"]
CaseKind = Literal["oi_only", "news_only", "news_oi"]
ExchangeId = Literal["binance", "hyperliquid", "paper"]
OrderSide = Literal["buy", "sell"]
PolicyDecision = Literal["no_trade", "long", "short"]


def utc_day_key(now_ms: int) -> str:
    """Stable UTC budget key derived from an injected timestamp."""

    return datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


class CaseState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    NO_TRADE = "NO_TRADE"
    POLICY_REJECTED = "POLICY_REJECTED"
    ORDER_PREPARED = "ORDER_PREPARED"
    BLOCKED = "BLOCKED"


class OrderState(StrEnum):
    PREPARED = "PREPARED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED_BY_OPERATOR = "REJECTED_BY_OPERATOR"
    SUBMITTING = "SUBMITTING"
    REJECTED = "REJECTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL = "PARTIAL"
    OPEN = "OPEN"
    UNPROTECTED = "UNPROTECTED"
    SAFETY_CLOSING = "SAFETY_CLOSING"
    AMBIGUOUS = "AMBIGUOUS"
    RECONCILING = "RECONCILING"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    CLOSED = "CLOSED"


# Kept byte-identical with the partial unique index predicate in `20260823_0300_trading_core`. The index
# is the authority; this tuple is what the runners use to reason about the same set, and one test asserts
# the two agree so they cannot drift.
ACTIVE_ORDER_STATES: tuple[str, ...] = (
    OrderState.PREPARED,
    OrderState.AWAITING_APPROVAL,
    OrderState.APPROVED,
    OrderState.SUBMITTING,
    OrderState.AMBIGUOUS,
    OrderState.RECONCILING,
    OrderState.MANUAL_REVIEW_REQUIRED,
    OrderState.ACKNOWLEDGED,
    OrderState.PARTIAL,
    OrderState.OPEN,
    OrderState.UNPROTECTED,
    OrderState.SAFETY_CLOSING,
)

TERMINAL_ORDER_STATES: tuple[str, ...] = (
    OrderState.REJECTED,
    OrderState.REJECTED_BY_OPERATOR,
    OrderState.CLOSED,
)


class OiRegime(StrEnum):
    """The OI/price quadrant. OI direction alone is never a price direction (#104).

    The reader card maps `OI rise -> bullish` to fit the News delivery vocabulary. That mapping exists
    so a Chinese card reads naturally; it is not a market claim, and Trading must not inherit it.
    """

    BUILDUP_UP = "buildup_up"
    BUILDUP_DOWN = "buildup_down"
    DELEVERAGING_UP = "deleveraging_up"
    DELEVERAGING_DOWN = "deleveraging_down"
    UNCLEAR = "unclear"


def canonical_base_symbol(value: object) -> str:
    """The one place a provider spelling becomes an underlying identity.

    Provider coin tags carry an `XYZ-` prefix for the same instrument, exactly as the Gate strips it.
    Doing this before the blacklist lookup is what lets one `CL` row block `CL` and `XYZ-CL` without
    the operator enumerating spellings.
    """

    return str(value or "").strip().upper().removeprefix("XYZ-")


def underlying_key(base_symbol: object) -> str:
    """Venue-independent identity. One issuer, one bucket, whether it trades on Binance or Hyperliquid."""

    canonical = canonical_base_symbol(base_symbol)
    return f"crypto:{canonical}" if canonical else ""


def canonical_sha256(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    """Content address for a frozen manifest or an order payload. Sorted keys, no whitespace drift."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bar(_Frozen):
    """One closed interval from a public venue REST catalogue.

    Trading keeps its own shape rather than importing `tracefold.news.market_review.pricing.Candle`: the dependency
    rule is `trading -> platform`, and the composition root converts. `close_at_ms` is the exclusive end.
    """

    open_at_ms: int
    close_at_ms: int
    close: Decimal


class InstrumentRef(_Frozen):
    """One exactly-resolved contract. `(exchange_id, provider_symbol)` is the execution identity.

    `base_symbol` is a join hint and never an order field: two venues spell the same underlying
    differently, and a display symbol has never been safe to submit.
    """

    exchange_id: ExchangeId
    venue: str
    provider_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None = None
    observed_at_ms: int


class OiTradeCandidate(_Frozen):
    """The public projection of one deterministic telemetry verdict plus its rank-ledger row."""

    event_id: str
    observed_at_ms: int
    base_symbol: str
    venue: str

    oi_direction: Literal["rise", "fall"]
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int
    rank_in_window: int

    metric_version: str
    learning_epoch: NewsLearningEpoch
    program_version: Literal["news_oi_signal_v1"]
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: Literal["news_triage_policy_v10"]
    editorial_origin: Literal["telemetry_deterministic"]
    editorial_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scored_judgment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_manifest_sha: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def source_key(self) -> str:
        return f"oi:{self.event_id}:{self.metric_version}"


class NewsTradeCandidate(_Frozen):
    """The public projection of one persisted model Triage verdict, frozen at the verdict cutoff.

    Everything here was knowable when the verdict was written. No reaction, no review, no later member:
    a manifest that can see the future is a backtest that proves nothing.
    """

    event_id: str
    verdict_created_at_ms: int
    opened_at_ms: int

    base_symbol: str
    evidence_version: int
    evidence_sha256: str
    focus_fact_id: str
    comparison_fingerprint: str
    source_artifact_id: str | None
    source_published_at_ms: int | None

    final_decision: Literal["push", "escalate"]
    event_type: str
    # `direction` is the News vocabulary's sign — the rule reads "the named assets **or** risk assets",
    # so it is a risk-sentiment field as often as an instrument field. It is context for the model, and
    # the pure policy never turns it into a side on its own.
    risk_direction: str
    scope: str
    magnitude: int
    novelty: str
    headline_zh: str
    why_zh: str

    learning_epoch: NewsLearningEpoch
    program_version: Literal["news_semantic_program_v4"]
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: Literal["news_triage_policy_v10"]
    editorial_origin: Literal["model"]
    editorial_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scored_judgment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_manifest_sha: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def source_key(self) -> str:
        """`source_fact_key` (#154/#157): one artifact can carry several numbered facts."""

        artifact = self.source_artifact_id or f"event:{self.event_id}"
        return canonical_sha256({"artifact": artifact, "fingerprint": self.comparison_fingerprint})


class MarketContext(_Frozen):
    """Point-in-time market truth, read fresh and never cached across a decision."""

    instrument: InstrumentRef
    mark_price: Decimal
    observed_at_ms: int
    pre_move_bps: int | None
    pre_move_lookback_ms: int
    spread_bps: int | None
    # Paper cannot read an order book, so spread is unknown rather than zero. Live fails closed on it.
    spread_available: bool
    # Exact contract precision. The public instrument catalogue does not carry it, so paper sizes on a
    # declared conservative step and live must get it from provider metadata or refuse to size at all.
    quantity_step: Decimal | None = None
    min_quantity: Decimal | None = None
    contract_size: Decimal = Decimal("1")


class RegimeAssessment(_Frozen):
    regime: OiRegime
    reason: str
    pre_move_bps: int | None
    oi_direction: str | None


class TradeDecision(_Frozen):
    """What one `dspy.Predict` call returned, normalised. Never an order — an input to a pure policy."""

    decision: PolicyDecision
    directness: Literal["direct", "indirect", "broad"]
    surprise: int = Field(ge=0, le=3)
    price_in: int = Field(ge=0, le=3)
    alignment: Literal["aligned", "contradictory", "insufficient"]
    horizon: Literal["minutes", "hours", "none"]
    reason_code: str
    thesis_zh: str
    invalidation_zh: str


NO_TRADE_DECISION = TradeDecision(
    decision="no_trade",
    directness="broad",
    surprise=0,
    price_in=3,
    alignment="insufficient",
    horizon="none",
    reason_code="weak_evidence",
    thesis_zh="",
    invalidation_zh="",
)


class PolicyOutcome(_Frozen):
    """The pure mapping's answer. Every path names its rule, exactly as `news.decide()` does."""

    decision: PolicyDecision
    rule: str
    policy_version: str = TRADING_POLICY_VERSION


class TradingCaseManifest(_Frozen):
    """The frozen, content-addressed input to one decision. Nothing later than `cutoff_ms` may enter."""

    manifest_version: str = TRADING_MANIFEST_VERSION
    case_kind: CaseKind
    underlying_key: str
    base_symbol: str
    cutoff_ms: int

    oi: OiTradeCandidate | None = None
    news: NewsTradeCandidate | None = None
    regime: RegimeAssessment
    instrument: InstrumentRef
    mark_price: Decimal
    pre_move_bps: int | None

    def digest(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class PreparedOrder(_Frozen):
    """The immutable economic intent. The payload is frozen before any network call exists."""

    order_id: str
    case_id: str
    underlying_key: str
    instrument: InstrumentRef
    mode: TradingMode
    side: OrderSide
    notional_usd: Decimal
    quantity: Decimal
    entry_reference: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None
    must_close_after_ms: int
    payload: dict[str, Any]

    @property
    def payload_sha256(self) -> str:
        return canonical_sha256(self.payload)


class RiskRejection(_Frozen):
    rule: str
    detail: str = ""


class ExecutionReceipt(_Frozen):
    """What one adapter attempt returned. `ACKNOWLEDGED` is not a fill; remote reads own that."""

    state: Literal["ACKNOWLEDGED", "REJECTED", "AMBIGUOUS"]
    remote_order_id: str | None = None
    filled_quantity: Decimal | None = None
    average_price: Decimal | None = None
    reason: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ACTIVE_ORDER_STATES",
    "NO_TRADE_DECISION",
    "TERMINAL_ORDER_STATES",
    "TRADING_COLD_WRITE_TIMEOUT_SECONDS",
    "TRADING_MANIFEST_VERSION",
    "TRADING_POLICY_VERSION",
    "TRADING_PROGRAM_VERSION",
    "TRADING_RECONCILE_BACKOFF_MS",
    "Bar",
    "CaseKind",
    "CaseState",
    "ControlState",
    "ExchangeId",
    "ExecutionReceipt",
    "InstrumentRef",
    "MarketContext",
    "NewsTradeCandidate",
    "OiRegime",
    "OiTradeCandidate",
    "OrderSide",
    "OrderState",
    "PolicyDecision",
    "PolicyOutcome",
    "PreparedOrder",
    "RegimeAssessment",
    "RiskRejection",
    "TradeDecision",
    "TradingCaseManifest",
    "TradingMode",
    "canonical_base_symbol",
    "canonical_sha256",
    "underlying_key",
    "utc_day_key",
]
