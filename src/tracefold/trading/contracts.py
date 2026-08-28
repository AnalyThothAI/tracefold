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
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Bumped whenever the manifest layout, the regime arithmetic or the pure policy changes shape: a case
# frozen under one version is not comparable with a case frozen under another.
TRADING_MANIFEST_VERSION = "trading_manifest_v6"
TRADING_POLICY_VERSION = "trading_strategy_policy_v1"
TRADING_PROGRAM_VERSION = "trading_news_oi_decision_v1"
# Code-owned execution timing shared by the pipeline and the one-attempt protocol.
TRADING_COLD_WRITE_TIMEOUT_SECONDS = 10.0

# Trading consumes one explicit News generation. This is an upstream input contract, not a fallback:
# a case frozen under an older News Program/policy is terminal audit history after #160's hard cut.
NewsLearningEpoch = Literal["program_v9"]

# ---------------------------------------------------------------------------- upstream input rows
# What the composition root must hand this context to produce candidates. Trading owns these because
# they are *its* requirements, not News's SELECT lists: News may add a column, rename one, or publish a
# second projection without this file moving, and the App-side mapper is where the two meet.
#
# They are `TypedDict`s rather than validating models on purpose. Eligibility fails closed on a named
# rejection for every value it cannot use — an unparseable rank, an unknown direction, a verdict that is
# not a mapping — and a model that raised on the same row would turn a counted funnel entry into an
# exception the funnel never sees. Deliberately loose where the source is loose: `verdict` is a jsonb
# document and `venue` is provider text that may be absent.


def oi_source_key(event_id: object, metric_version: object) -> str:
    """The deterministic OI lane's source identity, from either a raw row or a typed candidate.

    A row rejected at the source stage never becomes an `OiTradeCandidate`, and its admission decision
    still has to be filed under the same key the case would have used — so the construction lives here
    rather than only on the model.
    """

    return f"oi:{event_id}:{metric_version}"


class OiCandidateRow(TypedDict):
    """One parsed deterministic OI telemetry fact offered to the candidate scanner."""

    event_id: str
    verdict_created_at_ms: int
    # The reader's own judgment of this frame, and the named rule behind it. Audit, not admission: since
    # #264 the Candidate Gate decides whether the fact may trigger, and a reader policy change must not
    # silently open or close the capital lane.
    final_decision: str
    source_rule: str | None
    # What the provider proves about the measurement (#265). Nullable together; `None` means unproven.
    source_strategy_id: str | None
    source_contract_version: str | None
    measurement_window_ms: int | None
    learning_epoch: str
    program_version: str
    program_sha256: str
    policy_version: str
    editorial_origin: str
    editorial_sha256: str
    scored_judgment_sha256: str
    runtime_manifest_sha: str
    metric_version: str
    symbol: str
    direction: str
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int
    rank_in_window: int
    observed_at_ms: int
    ingest_mode: str
    venue: str | None


class NewsCandidateRow(TypedDict):
    """One editorial Triage verdict offered to the candidate scanner."""

    event_id: str
    verdict_created_at_ms: int
    final_decision: str
    # Optional upstream, so optional here: the eligibility rules already read all three through their
    # fail-closed accessors, and a contract that promised `int` would make the next reader trust it.
    evidence_version: int | None
    evidence_sha256: str | None
    focus_fact_id: str | None
    verdict: Any
    learning_epoch: str
    program_version: str
    program_sha256: str
    policy_version: str
    editorial_origin: str
    editorial_sha256: str
    scored_judgment_sha256: str
    runtime_manifest_sha: str
    opened_at_ms: int
    comparison_fingerprint: str
    asset_class: str
    grounded_assets: Any
    ingest_mode: str
    source_artifact_id: str | None
    source_published_at_ms: int | None


class LiquidationCandidateRow(TypedDict):
    """One admission-time deterministic liquidation fact offered through the App mapping seam."""

    source_key: str
    item_id: str
    fact_id: str
    symbol: str
    venue: str
    liquidated_position_side: str
    forced_order_side: str
    notional_usd: Decimal
    quantity: Decimal | None
    price: Decimal
    event_at_ms: int
    received_at_ms: int
    parser_version: str
    provider_record_identity: str
    symbol_contract_identity: str
    position_side_semantics: str
    quantity_semantics: str
    notional_semantics: str
    price_semantics: str
    completeness_assumption: str
    throttle_assumption: str
    source_contract_version: str
    source_contract_complete: bool
    ingest_mode: str


class InstrumentCandidateRow(TypedDict):
    """One catalogue row offered to the venue resolver."""

    venue: str
    venue_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None
    status: str
    last_seen_ms: int


ControlState = Literal["RUNNING", "CLOSE_ONLY", "PAUSED"]
TriggerKind = Literal["oi", "liquidation", "news"]
StrategyPermission = Literal["shadow", "paper", "live_reviewed"]
StrategyId = Literal[
    "oi_smart_money_momentum_v1",
    # Retained as an identity `strategy_from_manifest` still rebuilds; `capital_strategy_id` no longer
    # routes a new Case to it (#265 §5.1). Its rules — a 95% whale-profit floor inside the shared 1-6%
    # band — are not the ones the smart-money template describes, and reusing the id would make every
    # Case frozen under it replay under rules it was never decided by.
    #
    # A Case frozen before `trading_manifest_v6` is *not* replayable, and that is what the version bump
    # means rather than an oversight: `min_oi_value_usd` left both OI strategy configs when the floor
    # got its single owner, so an older `strategy_config` no longer satisfies `_exact_keys` and its
    # digest no longer matches. `_uses_current_news_generation` refuses those manifests first, so the
    # runner never mis-decodes one. Production holds no `oi_momentum_v1` case at all, and every
    # `news_oi_alignment_v1` case predating the cut is already terminal.
    "oi_momentum_v1",
    "news_oi_alignment_v1",
    "liquidation_continuation_shadow_v1",
    "liquidation_exhaustion_shadow_v1",
]
ExchangeId = Literal["binance", "hyperliquid"]
LiveExchangeId = ExchangeId
PolicyDecision = Literal["no_trade", "long", "short"]


def utc_day_key(now_ms: int) -> str:
    """Stable UTC budget key derived from an injected timestamp."""

    return datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


class CaseState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    NO_TRADE = "NO_TRADE"
    POLICY_REJECTED = "POLICY_REJECTED"
    INTENT_EMITTED = "INTENT_EMITTED"
    # Audit-only history from the retired Paper/OpenTrade writer.
    ORDER_PREPARED = "ORDER_PREPARED"
    BLOCKED = "BLOCKED"


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
    # When the deterministic verdict became durable, as opposed to when the frame was observed. The
    # two are separate stages and the gap between them is the one latency Trading does not own (#211).
    verdict_created_at_ms: int
    base_symbol: str
    venue: str

    oi_direction: Literal["rise", "fall"]
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int
    rank_in_window: int

    # The reader's verdict on the same frame, frozen into the manifest so a capital decision can be read
    # beside the judgment that accompanied it. Deliberately `str` rather than a `Literal`: it is no longer
    # an admission rule, and pinning the reader's decision vocabulary here would turn a News policy change
    # into a Trading validation failure — the exact coupling #264 removes.
    final_decision: str
    source_rule: str
    metric_version: str
    # The provider's own measurement contract, frozen into the manifest so a Case is a claim about a
    # *specific* interval rather than about "OI rose". `None` means the interval could not be proven —
    # the frame is still a usable fact, and a strategy that reads the interval must refuse it by name.
    source_strategy_id: str | None = None
    source_contract_version: str | None = None
    measurement_window_ms: int | None = None
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
        return oi_source_key(self.event_id, self.metric_version)


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
    program_version: Literal["news_semantic_program_v5"]
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


class LiquidationSourceContract(_Frozen):
    """What the upstream feed proves about one normalized liquidation record."""

    provider_record_identity: str
    symbol_contract_identity: str
    position_side_semantics: str
    quantity_semantics: str
    notional_semantics: str
    price_semantics: str
    completeness_assumption: str
    throttle_assumption: str
    source_contract_version: str
    complete: bool


class LiquidationTradeCandidate(_Frozen):
    """One normalized forced-flow fact. Its side fields describe the fill, never a forecast."""

    source_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_id: str
    fact_id: str
    base_symbol: str
    venue: Literal["binance", "hyperliquid"]
    liquidated_position_side: Literal["long", "short"]
    forced_order_side: Literal["buy", "sell"]
    notional_usd: Decimal = Field(gt=0)
    quantity: Decimal | None = Field(default=None, gt=0)
    price: Decimal = Field(gt=0)
    event_at_ms: int = Field(gt=0)
    received_at_ms: int = Field(gt=0)
    parser_version: str
    source_contract: LiquidationSourceContract

    @property
    def source_latency_ms(self) -> int:
        return max(0, self.received_at_ms - self.event_at_ms)


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


class OiMarketTrigger(_Frozen):
    kind: Literal["oi"] = "oi"
    source_key: str
    observed_at_ms: int
    persisted_at_ms: int
    venue: str


class NewsMarketTrigger(_Frozen):
    kind: Literal["news"] = "news"
    source_key: str
    observed_at_ms: int
    persisted_at_ms: int
    venue: None = None


class LiquidationMarketTrigger(_Frozen):
    kind: Literal["liquidation"] = "liquidation"
    source_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at_ms: int
    persisted_at_ms: int
    venue: Literal["binance", "hyperliquid"]


MarketTrigger = Annotated[
    OiMarketTrigger | NewsMarketTrigger | LiquidationMarketTrigger,
    Field(discriminator="kind"),
]


class LiquidationAggregate(_Frozen):
    window_ms: int = Field(gt=0)
    count: int = Field(ge=1)
    notional_usd: Decimal = Field(gt=0)
    long_notional_usd: Decimal = Field(ge=0)
    short_notional_usd: Decimal = Field(ge=0)
    long_count: int = Field(ge=0)
    short_count: int = Field(ge=0)
    dominant_liquidated_side: Literal["long", "short"] | None
    dominant_share_bps: int = Field(ge=0, le=10_000)
    dominant_count: int = Field(ge=0)
    dominant_notional_usd: Decimal = Field(ge=0)
    dominant_acceleration_bps: int | None = None
    source_refs: tuple[str, ...] = ()


class FrozenMarketContext(_Frozen):
    mark_price: Decimal = Field(gt=0)
    observed_at_ms: int
    pre_move_bps: int | None
    pre_move_lookback_ms: int = Field(gt=0)
    price_momentum_bps: int | None = None
    price_momentum_window_ms: int | None = Field(default=None, gt=0)
    displacement_bps: int | None = None
    displacement_window_ms: int | None = Field(default=None, gt=0)
    spread_bps: int | None = None
    depth_notional_usd: Decimal | None = None
    funding_bps: int | None = None


class FrozenStrategyContext(_Frozen):
    """Only point-in-time facts visible at the primary trigger's cutoff."""

    mode: Literal["paper"]
    oi: OiTradeCandidate | None = None
    news: NewsTradeCandidate | None = None
    liquidation: LiquidationTradeCandidate | None = None
    liquidation_aggregate: LiquidationAggregate | None = None
    regime: RegimeAssessment
    market: FrozenMarketContext
    news_decision: TradeDecision | None = None
    intensity_decelerating: bool | None = None
    oi_collapsing: bool | None = None
    price_stopped_extreme: bool | None = None
    liquidity_recovered: bool | None = None


class StrategyOutcome(_Frozen):
    decision: PolicyDecision
    rule: str
    setup: str
    invalidation: str
    expected_horizon: Literal["minutes", "hours", "none"]
    permission: StrategyPermission
    policy_version: str = TRADING_POLICY_VERSION


class TradingCaseManifest(_Frozen):
    """The frozen, content-addressed input to one decision. Nothing later than `cutoff_ms` may enter."""

    manifest_version: str = TRADING_MANIFEST_VERSION
    primary_trigger: MarketTrigger
    contexts: FrozenStrategyContext
    strategy_id: StrategyId
    strategy_version: str
    strategy_config: dict[str, bool | int | str]
    strategy_config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    underlying_key: str
    base_symbol: str
    cutoff_ms: int
    instrument: InstrumentRef

    @model_validator(mode="after")
    def _config_digest_matches_snapshot(self) -> TradingCaseManifest:
        if canonical_sha256(self.strategy_config) != self.strategy_config_digest:
            raise ValueError("trading_strategy_config_digest_mismatch")
        return self

    @property
    def trigger_kind(self) -> TriggerKind:
        return self.primary_trigger.kind

    @property
    def oi(self) -> OiTradeCandidate | None:
        return self.contexts.oi

    @property
    def news(self) -> NewsTradeCandidate | None:
        return self.contexts.news

    @property
    def regime(self) -> RegimeAssessment:
        return self.contexts.regime

    @property
    def mark_price(self) -> Decimal:
        return self.contexts.market.mark_price

    @property
    def pre_move_bps(self) -> int | None:
        return self.contexts.market.pre_move_bps

    def digest(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


__all__ = [
    "NO_TRADE_DECISION",
    "TRADING_COLD_WRITE_TIMEOUT_SECONDS",
    "TRADING_MANIFEST_VERSION",
    "TRADING_POLICY_VERSION",
    "TRADING_PROGRAM_VERSION",
    "Bar",
    "CaseState",
    "ControlState",
    "ExchangeId",
    "FrozenMarketContext",
    "FrozenStrategyContext",
    "InstrumentCandidateRow",
    "InstrumentRef",
    "LiquidationAggregate",
    "LiquidationCandidateRow",
    "LiquidationMarketTrigger",
    "LiquidationTradeCandidate",
    "LiveExchangeId",
    "MarketTrigger",
    "NewsCandidateRow",
    "NewsMarketTrigger",
    "NewsTradeCandidate",
    "OiCandidateRow",
    "OiMarketTrigger",
    "OiRegime",
    "OiTradeCandidate",
    "PolicyDecision",
    "PolicyOutcome",
    "RegimeAssessment",
    "StrategyId",
    "StrategyOutcome",
    "StrategyPermission",
    "TradeDecision",
    "TradingCaseManifest",
    "TriggerKind",
    "canonical_base_symbol",
    "canonical_sha256",
    "oi_source_key",
    "underlying_key",
    "utc_day_key",
]
