"""Identity and artifact contract for the bounded OI BAR replay."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .candidate.blacklist import BlacklistSnapshotV1
from .candidate.routing import signal_exchange_id
from .capabilities import ExecutionCapabilitySnapshotV1
from .contracts import (
    Bar,
    FrozenMarketContext,
    FrozenStrategyContext,
    InstrumentRef,
    OiTradeCandidate,
    canonical_sha256,
    underlying_key,
)
from .decision.regime import RegimePolicy, assess, pre_move_bps, select_bar
from .execution_policy import EXECUTION_POLICY_SHA256, TARGET_NOTIONAL_CEILING_USD
from .research.oi_replay import PENDING_MARKET_CONTEXT, OiReplayOutcome
from .strategy.root import TradingStrategy

BAR_FIDELITY_VERSION: Final[Literal["bar_fidelity_v1"]] = "bar_fidelity_v1"
_VENUE_BY_EXCHANGE = {"binance": "binance.perp", "hyperliquid": "hl.perp"}


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReplayBarV1(_Frozen):
    venue: Literal["binance.perp", "hl.perp"]
    instrument_id: str
    open_at_ms: int
    close_at_ms: int
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_prices(self) -> Self:
        if (
            self.close_at_ms <= self.open_at_ms
            or self.high < max(self.open, self.close)
            or self.low > min(self.open, self.close)
        ):
            raise ValueError("replay_bar_invalid")
        return self


class ReplayExecutionIntentV1(_Frozen):
    replay_intent_version: Literal["replay_execution_intent_v1"] = "replay_execution_intent_v1"
    source_identity: str
    case_or_decision_identity: str
    strategy_identity: str
    scenario_venue: Literal["binance.perp", "hl.perp"]
    instrument_id: str
    underlying_key: str
    side: Literal["long"] = "long"
    risk_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_capability_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ts_event: int
    ts_init: int

    @property
    def replay_intent_id(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class ReplayScenarioCapabilityV1(_Frozen):
    capability_version: Literal["replay_scenario_capability_v1"] = "replay_scenario_capability_v1"
    venue: Literal["binance.perp", "hl.perp"]
    instrument_id: str
    native_symbol: str
    base_currency: str
    quote_currency: str
    price_precision: int = Field(ge=0, le=18)
    size_precision: int = Field(ge=0, le=18)
    price_increment: Decimal = Field(gt=0)
    size_increment: Decimal = Field(gt=0)
    min_quantity: Decimal | None = Field(default=None, ge=0)
    min_notional: Decimal | None = Field(default=None, ge=0)
    provenance: Literal["execution_capability_snapshot", "bar_model"]

    @property
    def capability_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class ReplayTerminalOutcomeV1(_Frozen):
    source_identity: str
    strategy_identity: str
    scenario_venue: Literal["binance.perp", "hl.perp"] | None = None
    instrument_id: str | None = None
    counterfactual: bool = False
    decision: Literal["NO_TRADE", "DIRECTIONAL", "SKIPPED"]
    decision_reason: str
    capital_admission: Literal["ELIGIBLE", "DENIED", "NOT_APPLICABLE"]
    capital_reason: str | None = None
    execution: Literal["CLOSED", "REJECTED", "MISSING_MARKET_DATA", "NOT_APPLICABLE"]
    execution_reason: str
    replay_intent: ReplayExecutionIntentV1 | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    quantity: Decimal | None = None
    gross_result: Decimal | None = None
    fees: Decimal | None = None
    funding: Decimal | None = None
    net_excluding_funding: Decimal | None = None
    net_including_funding: Decimal | None = None
    mfe_bps: int | None = None
    mae_bps: int | None = None


class ReplaySpecV1(_Frozen):
    spec_version: Literal["replay_spec_v1"] = "replay_spec_v1"
    run_kind: Literal["oi_decision_replay"] = "oi_decision_replay"
    start_ms: int
    end_ms: int
    source_query_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_slice_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_universe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_capability_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_scenarios_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blacklist_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_gate_version: str
    candidate_gate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    regime_lookback_ms: int = Field(gt=0)
    regime_min_price_move_bps: int = Field(ge=0)
    regime_max_price_move_bps: int = Field(gt=0)
    regime_bar_gap_tolerance_ms: int = Field(ge=0)
    target_notional_usd: Decimal = Field(gt=0, le=TARGET_NOTIONAL_CEILING_USD)
    strategy_identities: list[dict[str, str]]
    intent_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    app_revision: str
    app_image_digest: str
    nautilus_version: Literal["1.231.0"] = "1.231.0"
    nautilus_wheel_identity: str
    fidelity: Literal["bar_fidelity_v1"] = BAR_FIDELITY_VERSION
    venue_scenarios: list[dict[str, str]]
    fee_model: dict[str, str]
    funding_model: dict[str, str]
    fill_model: dict[str, str]
    slippage_model: dict[str, str]
    latency_model: dict[str, str]

    @model_validator(mode="after")
    def validate_regime_band(self) -> Self:
        if self.regime_max_price_move_bps <= self.regime_min_price_move_bps:
            raise ValueError("replay_regime_band_invalid")
        return self

    @property
    def run_id(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class ReplayReceiptV1(_Frozen):
    run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at_ms: int
    terminal_status: Literal["SUCCEEDED"] = "SUCCEEDED"
    artifact_path: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_count: int = Field(ge=0)
    directional_count: int = Field(ge=0)
    terminal_outcome_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_spec_identity(self) -> Self:
        if self.run_id != self.spec_sha256:
            raise ValueError("replay_receipt_spec_identity_invalid")
        return self


class ReplayArtifactV1(_Frozen):
    artifact_version: Literal["oi_bar_replay_artifact_v1"] = "oi_bar_replay_artifact_v1"
    run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: ReplaySpecV1
    blacklist_snapshot_payload: BlacklistSnapshotV1
    source_facts: list[dict[str, Any]]
    market_slices: list[dict[str, Any]]
    outcomes: list[ReplayTerminalOutcomeV1]
    summary: dict[str, Any]

    @model_validator(mode="after")
    def validate_run_identity(self) -> Self:
        if self.run_id != self.spec.run_id:
            raise ValueError("replay_artifact_run_identity_invalid")
        if self.blacklist_snapshot_payload.snapshot_sha256 != self.spec.blacklist_snapshot_sha256:
            raise ValueError("replay_artifact_blacklist_identity_invalid")
        if canonical_sha256(self.source_facts) != self.spec.source_facts_sha256:
            raise ValueError("replay_artifact_source_identity_invalid")
        if canonical_sha256(self.market_slices) != self.spec.market_slice_sha256:
            raise ValueError("replay_artifact_market_identity_invalid")
        return self


@dataclass(frozen=True, slots=True)
class ReplayScenarioRequestV1:
    source: OiTradeCandidate
    venue: Literal["binance.perp", "hl.perp"]


@dataclass(frozen=True, slots=True)
class DirectionalReplayPlan:
    source: OiTradeCandidate
    instrument: InstrumentRef
    venue: Literal["binance.perp", "hl.perp"]
    instrument_id: str


@dataclass(frozen=True, slots=True)
class ReplayMarketSlice:
    plan: DirectionalReplayPlan
    bars: list[ReplayBarV1]
    reason: str | None
    start_ms: int
    end_ms: int

    def artifact_row(self) -> dict[str, Any]:
        return {
            "source_identity": self.plan.source.source_key,
            "venue": self.plan.venue,
            "instrument_id": self.plan.instrument_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "reason": self.reason,
            "bars": [bar.model_dump(mode="json") for bar in self.bars],
        }


@dataclass(frozen=True, slots=True)
class BarEpisodeResult:
    execution: str
    reason: str
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    quantity: Decimal | None = None
    gross_result: Decimal | None = None
    fees: Decimal | None = None
    net_excluding_funding: Decimal | None = None
    mfe_bps: int | None = None
    mae_bps: int | None = None


class BarEpisodeRunner(Protocol):
    def __call__(
        self,
        *,
        intent: ReplayExecutionIntentV1,
        capability: ReplayScenarioCapabilityV1,
        bars: list[ReplayBarV1],
        reference_price: Decimal,
        target_notional: Decimal,
    ) -> BarEpisodeResult: ...


def replay_strategy_identity(strategy: TradingStrategy) -> str:
    return canonical_sha256(
        {
            "strategy_id": strategy.strategy_id,
            "strategy_version": strategy.strategy_version,
            "strategy_config_sha256": strategy.config_digest,
        }
    )


def plan_replay_source(
    outcome: OiReplayOutcome,
    parsed: Mapping[str, OiTradeCandidate],
    *,
    strategy: TradingStrategy,
    requested_venues: tuple[str, ...],
) -> ReplayScenarioRequestV1 | ReplayTerminalOutcomeV1:
    strategy_identity = replay_strategy_identity(strategy)
    if outcome.stage != PENDING_MARKET_CONTEXT:
        decision = "NO_TRADE" if outcome.stage == "strategy" else "SKIPPED"
        return ReplayTerminalOutcomeV1(
            source_identity=outcome.source_key,
            strategy_identity=strategy_identity,
            decision=cast(Literal["NO_TRADE", "SKIPPED"], decision),
            decision_reason=outcome.reason if decision == "NO_TRADE" else _skip_reason(outcome.stage, outcome.reason),
            capital_admission="NOT_APPLICABLE",
            execution="NOT_APPLICABLE",
            execution_reason="strategy_not_directional",
        )
    source = parsed.get(outcome.source_key)
    exchange = None if source is None else signal_exchange_id(source.venue)
    venue = None if exchange is None else _VENUE_BY_EXCHANGE[exchange]
    if source is None or venue is None or venue not in requested_venues:
        return ReplayTerminalOutcomeV1(
            source_identity=outcome.source_key,
            strategy_identity=strategy_identity,
            decision="SKIPPED",
            decision_reason="venue_ambiguous" if venue is None else "strategy_not_applicable",
            capital_admission="NOT_APPLICABLE",
            execution="NOT_APPLICABLE",
            execution_reason="strategy_not_applicable",
        )
    return ReplayScenarioRequestV1(
        source=source,
        venue=cast(Literal["binance.perp", "hl.perp"], venue),
    )


def unresolved_replay_instrument(
    request: ReplayScenarioRequestV1,
    *,
    strategy: TradingStrategy,
) -> ReplayTerminalOutcomeV1:
    return ReplayTerminalOutcomeV1(
        source_identity=request.source.source_key,
        strategy_identity=replay_strategy_identity(strategy),
        scenario_venue=request.venue,
        decision="SKIPPED",
        decision_reason="instrument_unresolved",
        capital_admission="NOT_APPLICABLE",
        execution="NOT_APPLICABLE",
        execution_reason="instrument_unresolved",
    )


def evaluate_replay_market_slices(
    slices: list[ReplayMarketSlice],
    *,
    strategy: TradingStrategy,
    snapshot: ExecutionCapabilitySnapshotV1,
    blacklist: BlacklistSnapshotV1,
    run_episode: BarEpisodeRunner,
    regime_policy: RegimePolicy,
    target_notional: Decimal,
) -> list[ReplayTerminalOutcomeV1]:
    return [
        _market_outcome(
            item,
            strategy=strategy,
            snapshot=snapshot,
            blacklist=blacklist,
            run_episode=run_episode,
            regime_policy=regime_policy,
            target_notional=target_notional,
        )
        for item in slices
    ]


def _market_outcome(
    item: ReplayMarketSlice,
    *,
    strategy: TradingStrategy,
    snapshot: ExecutionCapabilitySnapshotV1,
    blacklist: BlacklistSnapshotV1,
    run_episode: BarEpisodeRunner,
    regime_policy: RegimePolicy,
    target_notional: Decimal,
) -> ReplayTerminalOutcomeV1:
    plan = item.plan
    strategy_identity = replay_strategy_identity(strategy)
    if item.reason is not None:
        return _market_missing(plan, strategy_identity, item.reason)
    close_bars = [Bar(open_at_ms=bar.open_at_ms, close_at_ms=bar.close_at_ms, close=bar.close) for bar in item.bars]
    anchor = select_bar(
        close_bars,
        target_ms=plan.source.observed_at_ms,
        gap_tolerance_ms=regime_policy.bar_gap_tolerance_ms,
    )
    move = pre_move_bps(close_bars, anchor_at_ms=plan.source.observed_at_ms, policy=regime_policy)
    if anchor is None or move is None:
        return _market_missing(plan, strategy_identity, "outside_bar_coverage")
    regime = assess(oi_direction=plan.source.oi_direction, move=move, policy=regime_policy)
    decision = strategy.evaluate(
        FrozenStrategyContext(
            mode="paper",
            oi=plan.source,
            regime=regime,
            market=FrozenMarketContext(
                mark_price=anchor.close,
                observed_at_ms=plan.source.observed_at_ms,
                pre_move_bps=move,
                pre_move_lookback_ms=regime_policy.lookback_ms,
            ),
        )
    )
    capital, capital_reason = _capital_admission(plan, snapshot, blacklist)
    if decision.decision != "long":
        return ReplayTerminalOutcomeV1(
            source_identity=plan.source.source_key,
            strategy_identity=strategy_identity,
            scenario_venue=plan.venue,
            instrument_id=plan.instrument_id,
            decision="NO_TRADE",
            decision_reason=decision.rule,
            capital_admission="NOT_APPLICABLE",
            execution="NOT_APPLICABLE",
            execution_reason="strategy_not_directional",
        )
    capability = _scenario_capability(plan, item.bars, snapshot)
    replay_intent = ReplayExecutionIntentV1(
        source_identity=plan.source.source_key,
        case_or_decision_identity=canonical_sha256(
            {
                "source": plan.source.model_dump(mode="json"),
                "strategy_identity": strategy_identity,
                "decision": decision.model_dump(mode="json"),
                "market_cutoff_ms": plan.source.observed_at_ms,
            }
        ),
        strategy_identity=strategy_identity,
        scenario_venue=plan.venue,
        instrument_id=plan.instrument_id,
        underlying_key=underlying_key(plan.source.base_symbol),
        risk_policy_sha256=EXECUTION_POLICY_SHA256,
        scenario_capability_sha256=capability.capability_sha256,
        ts_event=plan.source.observed_at_ms,
        ts_init=plan.source.verdict_created_at_ms,
    )
    episode = run_episode(
        intent=replay_intent,
        capability=capability,
        bars=item.bars,
        reference_price=anchor.close,
        target_notional=target_notional,
    )
    return ReplayTerminalOutcomeV1(
        source_identity=plan.source.source_key,
        strategy_identity=strategy_identity,
        scenario_venue=plan.venue,
        instrument_id=plan.instrument_id,
        decision="DIRECTIONAL",
        decision_reason=decision.rule,
        capital_admission=capital,
        capital_reason=capital_reason,
        execution=cast(Any, episode.execution),
        execution_reason=episode.reason,
        replay_intent=replay_intent,
        entry_price=episode.entry_price,
        exit_price=episode.exit_price,
        quantity=episode.quantity,
        gross_result=episode.gross_result,
        fees=episode.fees,
        funding=None,
        net_excluding_funding=episode.net_excluding_funding,
        net_including_funding=None,
        mfe_bps=episode.mfe_bps,
        mae_bps=episode.mae_bps,
    )


def _market_missing(
    plan: DirectionalReplayPlan,
    strategy_identity: str,
    reason: str,
) -> ReplayTerminalOutcomeV1:
    return ReplayTerminalOutcomeV1(
        source_identity=plan.source.source_key,
        strategy_identity=strategy_identity,
        scenario_venue=plan.venue,
        instrument_id=plan.instrument_id,
        decision="SKIPPED",
        decision_reason=reason,
        capital_admission="NOT_APPLICABLE",
        execution="MISSING_MARKET_DATA",
        execution_reason=reason,
    )


def _capital_admission(
    plan: DirectionalReplayPlan,
    snapshot: ExecutionCapabilitySnapshotV1,
    blacklist: BlacklistSnapshotV1,
) -> tuple[Literal["ELIGIBLE", "DENIED", "NOT_APPLICABLE"], str | None]:
    if plan.venue != "binance.perp":
        return "NOT_APPLICABLE", "research_only_venue"
    if any(row.underlying_key == underlying_key(plan.source.base_symbol) for row in blacklist.active_rows):
        return "DENIED", "blacklisted"
    capability = snapshot.included.get(plan.instrument_id)
    if capability is None or capability.underlying_key != underlying_key(plan.source.base_symbol):
        return "DENIED", "instrument_not_in_capability_snapshot"
    return "ELIGIBLE", None


def _scenario_capability(
    plan: DirectionalReplayPlan,
    bars: list[ReplayBarV1],
    snapshot: ExecutionCapabilitySnapshotV1,
) -> ReplayScenarioCapabilityV1:
    capital = snapshot.included.get(plan.instrument_id)
    if capital is not None:
        price_precision = capital.price_precision
        size_precision = capital.size_precision
        price_increment = Decimal(capital.price_increment)
        size_increment = Decimal(capital.size_increment)
        min_quantity = None if capital.min_quantity is None else Decimal(capital.min_quantity)
        min_notional = None if capital.min_notional is None else Decimal(capital.min_notional)
        quote_currency = capital.quote_currency
        provenance: Literal["execution_capability_snapshot", "bar_model"] = "execution_capability_snapshot"
    else:
        price_precision = min(
            8,
            max(_decimal_places(value) for bar in bars for value in (bar.open, bar.high, bar.low, bar.close)),
        )
        size_precision = 8
        price_increment = Decimal(1).scaleb(-price_precision)
        size_increment = Decimal(1).scaleb(-size_precision)
        min_quantity = None
        min_notional = None
        quote_currency = plan.instrument.quote_asset or ("USDT" if plan.venue == "binance.perp" else "USDC")
        provenance = "bar_model"
    return ReplayScenarioCapabilityV1(
        venue=plan.venue,
        instrument_id=plan.instrument_id,
        native_symbol=plan.instrument.provider_symbol,
        base_currency=plan.instrument.base_symbol,
        quote_currency=quote_currency,
        price_precision=price_precision,
        size_precision=size_precision,
        price_increment=price_increment,
        size_increment=size_increment,
        min_quantity=min_quantity,
        min_notional=min_notional,
        provenance=provenance,
    )


def _decimal_places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("replay_bar_price_non_finite")
    return max(0, -exponent)


def _skip_reason(stage: str, reason: str) -> str:
    if stage == "source":
        return f"source_{reason}"
    if stage == "gate":
        return f"gate_{reason}"
    return reason


def summarize_replay_outcomes(outcomes: list[ReplayTerminalOutcomeV1]) -> dict[str, Any]:
    decision = Counter(row.decision for row in outcomes)
    capital = Counter(row.capital_admission for row in outcomes)
    execution = Counter(row.execution for row in outcomes)
    reasons = Counter(row.execution_reason for row in outcomes)
    coverage = Counter(
        f"{row.scenario_venue or 'none'}:{row.instrument_id or 'none'}:{row.execution_reason}" for row in outcomes
    )
    closed = [row for row in outcomes if row.execution == "CLOSED"]
    return {
        "source_count": len(outcomes),
        "decision_counts": dict(sorted(decision.items())),
        "capital_admission_counts": dict(sorted(capital.items())),
        "execution_counts": dict(sorted(execution.items())),
        "execution_reasons": dict(sorted(reasons.items())),
        "coverage": dict(sorted(coverage.items())),
        "closed_trades": len(closed),
        "gross_result": str(sum((row.gross_result or Decimal(0) for row in closed), Decimal(0))),
        "fees": str(sum((row.fees or Decimal(0) for row in closed), Decimal(0))),
        "funding": None,
        "net_excluding_funding": str(sum((row.net_excluding_funding or Decimal(0) for row in closed), Decimal(0))),
        "net_including_funding": None,
        "stop_rate": _rate(closed, "stop"),
        "max_holding_rate": _rate(closed, "max_holding"),
        "portfolio_drawdown": None,
        "fidelity_limitations": [
            "bar OHLC execution is not live MARK_PRICE stop or order-book fill parity",
            "funding unavailable; it is null, never zero",
            "no portfolio concurrency or drawdown model",
        ],
    }


def _rate(rows: list[ReplayTerminalOutcomeV1], reason: str) -> str | None:
    if not rows:
        return None
    return str(Decimal(sum(row.execution_reason == reason for row in rows)) / Decimal(len(rows)))


def canonical_artifact_bytes(artifact: ReplayArtifactV1) -> bytes:
    return json.dumps(
        artifact.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def verify_replay_artifact_bytes(payload: bytes, *, expected_sha256: str) -> None:
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError("replay_artifact_corrupt")
    try:
        ReplayArtifactV1.model_validate_json(payload)
    except ValueError as exc:
        raise RuntimeError("replay_artifact_corrupt") from exc


__all__ = [
    "BAR_FIDELITY_VERSION",
    "ReplayArtifactV1",
    "ReplayBarV1",
    "ReplayExecutionIntentV1",
    "ReplayReceiptV1",
    "ReplayScenarioCapabilityV1",
    "ReplaySpecV1",
    "ReplayTerminalOutcomeV1",
    "canonical_artifact_bytes",
    "verify_replay_artifact_bytes",
]
