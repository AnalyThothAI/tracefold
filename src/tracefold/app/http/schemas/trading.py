"""Read-only capital-lane HTTP contract, one schema family per durable aggregate (#331).

    Source / Admission   `TradingGateData`, `TradingGateDecisionData`
    Case / Decision      `TradingCasesData`, `TradingCaseData`
    Intent / Outcome     `TradingIntentsData`, `TradingIntentData`
    runtime readiness    `TradingStatusData`

Nothing crosses. A Case carries its own frozen policy checks and no execution lifecycle; an Intent
carries its lifecycle and only a `case_id` back-reference; the status surface carries readiness and
bounded durable totals and no threshold at all.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ExactApiSchema

GateStatus = Literal["DEFERRED", "REJECTED", "RESEARCH_ONLY", "CASE_CREATED", "EXPIRED"]
# `routing` is retained for rows written before #331; the current stages are the other five.
GateStage = Literal["source", "venue", "eligibility", "capability", "routing", "market_context", "freeze"]


# ---------------------------------------------------------------------------- runtime readiness
class TradingBudgetData(ExactApiSchema):
    """What one thesis may cost. The lane's bound is serialisation, not a daily count (#348).

    `max_entries_per_utc_day` is gone rather than set to some larger number: there is no daily count any
    more, and publishing a ceiling nobody enforces is worse than publishing none.
    """

    target_notional_usd: str


class TradingReadinessData(ExactApiSchema):
    enabled: bool
    control: Literal["RUNNING", "CLOSE_ONLY", "PAUSED"]
    execution_authority: Literal["nautilus"] = "nautilus"
    execution_environment: Literal["BINANCE_USDM_DEMO"] = "BINANCE_USDM_DEMO"
    active_capability_snapshot_sha256: str | None = None
    active_capability_included_count: int = Field(ge=0)
    blacklist_revision: int = Field(ge=0)
    credentials_configured: bool
    engine_ready: bool
    engine_readiness_reason: str | None = None
    unexpected_exposure: bool
    heartbeat_at_ms: int | None = None


class TradingRuntimeCountsData(ExactApiSchema):
    """Bounded aggregation over durable rows. No funnel, no per-poll counter, no threshold."""

    day_key: str = ""
    active_intents: int = 0
    entries_today: int = 0
    closed_intents_today: int = 0
    cases_24h: int = 0
    intents_24h: int = 0
    latest_case_created_at_ms: int | None = None
    latest_intent_emitted_at_ms: int | None = None
    latest_entry_fenced_at_ms: int | None = None
    latest_position_opened_at_ms: int | None = None
    latest_position_closed_at_ms: int | None = None


class TradingPolicyIdentityData(ExactApiSchema):
    """Which policy the lane would freeze onto a *new* Case. Never applied to an existing one."""

    policy_id: str
    policy_version: str
    config_digest: str
    config: dict[str, str] = Field(default_factory=dict)


class TradingStatusData(ExactApiSchema):
    budget: TradingBudgetData
    readiness: TradingReadinessData
    policy: TradingPolicyIdentityData
    counts: TradingRuntimeCountsData
    window_hours: int
    measured_at_ms: int


# ---------------------------------------------------------------------------- Source / Admission
class TradingGateEvidenceData(ExactApiSchema):
    venue: str = ""
    oi_change_bps: int | None = None
    oi_value_usd: int | None = None
    whale_oi_ratio_bps: int | None = None
    whale_long_profit_bps: int | None = None
    rank_in_window: int | None = None
    source_decision: str = ""
    source_rule: str = ""
    floor: int | None = None
    limit: int | None = None
    age_ms: int | None = None
    max_age_ms: int | None = None
    since_close_ms: int | None = None
    cooldown_ms: int | None = None
    blacklist_reason: str = ""
    live_exchange_id: str = ""
    lane_full: str = ""
    enabled: list[str] = Field(default_factory=list)
    rule: str = ""


class TradingGateConfigData(ExactApiSchema):
    version: str
    config_digest: str
    max_age_ms: int
    min_oi_value_usd: int
    live_exchange_id: str


class TradingGateDecisionData(ExactApiSchema):
    source_key: str
    event_id: str | None = None
    underlying_key: str | None = None
    base_symbol: str = ""
    trigger_kind: str
    source_observed_at_ms: int
    research_only: bool = False
    gate_status: GateStatus | None = None
    gate_stage: GateStage | None = None
    gate_reason: str | None = None
    gate_retryable: bool | None = None
    gate_version: str | None = None
    gate_config_digest: str | None = None
    gate_evidence: TradingGateEvidenceData | None = None
    gate_first_evaluated_at_ms: int | None = None
    gate_last_evaluated_at_ms: int | None = None
    gate_attempt_count: int | None = None
    case_id: str | None = None


class TradingGateData(ExactApiSchema):
    config: TradingGateConfigData
    decisions: list[TradingGateDecisionData] = Field(default_factory=list)
    status_counts_24h: dict[str, int] = Field(default_factory=dict)
    reason_counts_24h: dict[str, int] = Field(default_factory=dict)
    latest_source_at_ms: int | None = None
    latest_gate_eligible_at_ms: int | None = None
    complete: bool
    window_hours: int
    measured_at_ms: int


class TradingGateSourceData(ExactApiSchema):
    """One Source's admission answer, plus the Case it authored if it authored one.

    `joinable` is `false` when the question cannot be asked at all: only the deterministic OI lane's
    source key is reconstructible from an Event id.
    """

    event_id: str
    joinable: bool
    decision: TradingGateDecisionData | None = None


# ---------------------------------------------------------------------------- Case / Decision
class TradingPolicyCheckData(ExactApiSchema):
    """One frozen condition: what was required, what was measured, and whether it passed."""

    check: str
    operator: str
    threshold: str
    measured: str | None = None
    passed: bool


class TradingCaseData(ExactApiSchema):
    case_id: str
    event_id: str | None = None
    underlying_key: str
    base_symbol: str
    provider_symbol: str | None = None
    trigger_kind: str
    manifest_version: str | None = None
    policy_id: str
    policy_version: str
    policy_config_digest: str
    # The thresholds this Case was frozen against, never the ones configured now.
    policy_config: dict[str, str] = Field(default_factory=dict)
    policy_checks: list[TradingPolicyCheckData] = Field(default_factory=list)
    state: str
    policy_decision: str | None = None
    policy_reason: str | None = None
    mark_price: str | None = None
    pre_move_bps: int | None = None
    oi_change_bps: int | None = None
    oi_value_usd: int | None = None
    whale_oi_ratio_bps: int | None = None
    whale_long_profit_bps: int | None = None
    observed_at_ms: int
    created_at_ms: int
    decided_at_ms: int | None = None
    intent_id: str | None = None


class TradingCasesData(ExactApiSchema):
    cases: list[TradingCaseData] = Field(default_factory=list)
    state_counts_24h: dict[str, int] = Field(default_factory=dict)
    reason_counts_24h: dict[str, int] = Field(default_factory=dict)
    complete: bool
    window_hours: int
    measured_at_ms: int


# ---------------------------------------------------------------------------- Intent / Outcome
class TradingIntentData(ExactApiSchema):
    intent_id: str
    intent_version: Literal["trade_intent_v1", "trade_intent_v2"]
    case_id: str
    event_id: str | None = None
    underlying_key: str
    base_symbol: str
    execution_environment: Literal["BINANCE_USDM_DEMO"]
    execution_capability_snapshot_sha256: str | None = None
    blacklist_revision_at_emission: int | None = None
    blacklist_snapshot_sha256_at_emission: str | None = None
    instrument_id: str
    side: Literal["long"]
    target_notional_usd: str
    reference_price: str
    valid_until_ms: int
    execution_state: Literal["PENDING", "IN_FLIGHT", "OPEN_PROTECTED", "MANUAL_REVIEW", "TERMINAL"]
    execution_phase: Literal["ENTRY", "PROTECTION", "EXIT"] | None = None
    terminal_outcome: Literal["EXPIRED", "REJECTED", "CLOSED_FLAT"] | None = None
    reason_code: str | None = None
    actual_quantity: str | None = None
    protected_quantity: str | None = None
    avg_entry_price: str | None = None
    avg_exit_price: str | None = None
    stop_price: str | None = None
    entry_fenced_at_ms: int | None = None
    opened_at_ms: int | None = None
    protected_at_ms: int | None = None
    closed_at_ms: int | None = None
    flat_verified_at_ms: int | None = None
    realized_pnl_amount: str | None = None
    realized_pnl_currency: str | None = None
    commissions_by_currency: dict[str, str] | None = None
    created_at_ms: int
    updated_at_ms: int
    policy_id: str
    policy_version: str


class TradingIntentsData(ExactApiSchema):
    intents: list[TradingIntentData] = Field(default_factory=list)
    state_counts_24h: dict[str, int] = Field(default_factory=dict)
    outcome_counts_24h: dict[str, int] = Field(default_factory=dict)
    reason_counts_24h: dict[str, int] = Field(default_factory=dict)
    complete: bool
    window_hours: int
    measured_at_ms: int


__all__ = [name for name in globals() if name.startswith("Trading")]
