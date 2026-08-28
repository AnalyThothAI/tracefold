"""Read-only Case -> Intent -> Outcome HTTP contract."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ExactApiSchema


class TradingBudgetData(ExactApiSchema):
    target_notional_usd: str
    max_entries_per_utc_day: Literal[1] = 1


class TradingReadinessData(ExactApiSchema):
    enabled: bool
    control: Literal["RUNNING", "CLOSE_ONLY", "PAUSED"]
    execution_authority: Literal["nautilus"] = "nautilus"
    execution_environment: Literal["BINANCE_USDM_DEMO"] = "BINANCE_USDM_DEMO"
    instrument_id: Literal["SOLUSDT-PERP.BINANCE"] = "SOLUSDT-PERP.BINANCE"
    credentials_configured: bool
    engine_ready: bool
    engine_readiness_reason: str | None = None
    unexpected_exposure: bool
    heartbeat_at_ms: int | None = None


class TradingFloorsData(ExactApiSchema):
    min_whale_long_profit_bps: int
    min_oi_value_usd: str
    min_price_move_bps: int
    max_price_move_bps: int
    lookback_ms: int


class TradingBootstrapData(ExactApiSchema):
    mean_bps: int
    lower_95_bps: int
    upper_95_bps: int


class TradingHorizonData(ExactApiSchema):
    measured: int = 0
    missing: int = 0
    bootstrap: TradingBootstrapData | None = None


class TradingShadowCohortData(ExactApiSchema):
    evaluated: int
    completed: int
    mean_return_bps: int | None = None
    holdout: int = 0
    source_contract_complete: int = 0
    coverage_bps: int = 0
    mean_source_latency_ms: int | None = None
    duplicate_rate_bps: int | None = None
    horizons: dict[str, TradingHorizonData] = Field(default_factory=dict)
    mfe_mean_bps: int | None = None
    mae_mean_bps: int | None = None
    exit_by_reason: dict[str, int] = Field(default_factory=dict)
    net_ex_funding_bootstrap: TradingBootstrapData | None = None
    missing_data: dict[str, int] = Field(default_factory=dict)
    promotion_ready: bool = False
    promotion_reasons: list[str] = Field(default_factory=list)


class TradingEventStudyCohortData(TradingShadowCohortData):
    cohort_key: str
    strategy_id: str
    venue: str
    liquidity_bucket: str


class TradingCountsData(ExactApiSchema):
    cases_by_state: dict[str, int] = Field(default_factory=dict)
    cases_by_trigger: dict[str, int] = Field(default_factory=dict)
    cases_by_strategy: dict[str, int] = Field(default_factory=dict)
    shadow_by_strategy: dict[str, int] = Field(default_factory=dict)
    shadow_by_rule: dict[str, int] = Field(default_factory=dict)
    shadow_cohorts: dict[str, TradingShadowCohortData] = Field(default_factory=dict)
    event_study_cohorts: list[TradingEventStudyCohortData] = Field(default_factory=list)
    liquidation_promotion_ready: bool = False
    liquidation_promotion_reason: str = ""
    intents_by_state: dict[str, int] = Field(default_factory=dict)
    outcomes_by_state: dict[str, int] = Field(default_factory=dict)
    cases_today_by_state: dict[str, int] = Field(default_factory=dict)
    policy_allowed_today: int = 0
    policy_allowed_24h: int = 0
    entries_today: int = 0
    closed_intents_today: int = 0
    active_intents: int = 0
    funnel_today: dict[str, int] = Field(default_factory=dict)
    funnel_day_key: str = ""
    candidate_counts_24h: dict[str, int] = Field(default_factory=dict)
    candidate_counts_7d: dict[str, int] = Field(default_factory=dict)
    candidate_reasons_24h: dict[str, int] = Field(default_factory=dict)
    candidate_reasons_7d: dict[str, int] = Field(default_factory=dict)
    latest_source_at_ms: int | None = None
    latest_gate_eligible_at_ms: int | None = None
    latest_case_created_at_ms: int | None = None
    latest_intent_emitted_at_ms: int | None = None
    latest_entry_fenced_at_ms: int | None = None
    latest_position_opened_at_ms: int | None = None
    latest_position_closed_at_ms: int | None = None


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
    blacklist_reason: str = ""
    enabled: list[str] = Field(default_factory=list)
    rule: str = ""


class TradingGateConfigData(ExactApiSchema):
    version: str
    config_digest: str
    max_age_ms: int
    max_rank_in_window: int
    min_oi_value_usd: int
    symbol_cooldown_ms: int
    venue_priority: list[str] = Field(default_factory=list)


class TradingStrategyConfigData(ExactApiSchema):
    strategy_id: str
    strategy_version: str
    config_digest: str
    permission: str
    trigger_kinds: list[str] = Field(default_factory=list)
    config: dict[str, str] = Field(default_factory=dict)


class TradingStatusData(ExactApiSchema):
    budget: TradingBudgetData
    readiness: TradingReadinessData
    floors: TradingFloorsData
    gate: TradingGateConfigData
    strategies: list[TradingStrategyConfigData] = Field(default_factory=list)
    counts: TradingCountsData
    window_hours: int
    measured_at_ms: int


class TradingCaseData(ExactApiSchema):
    case_id: str
    event_id: str | None = None
    underlying_key: str
    base_symbol: str
    trigger_kind: str
    strategy_id: str
    strategy_version: str
    state: str
    regime: str | None = None
    policy_decision: str | None = None
    policy_reason: str | None = None
    pre_move_bps: int | None = None
    strategy_config: dict[str, str] = Field(default_factory=dict)
    regime_reason: str | None = None
    observed_at_ms: int
    created_at_ms: int
    decided_at_ms: int | None = None


class TradingIntentData(ExactApiSchema):
    intent_id: str
    case_id: str
    event_id: str | None = None
    underlying_key: str
    base_symbol: str
    execution_environment: Literal["BINANCE_USDM_DEMO"]
    instrument_id: Literal["SOLUSDT-PERP.BINANCE"]
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
    opened_at_ms: int | None = None
    protected_at_ms: int | None = None
    closed_at_ms: int | None = None
    flat_verified_at_ms: int | None = None
    realized_pnl_amount: str | None = None
    realized_pnl_currency: str | None = None
    commissions_by_currency: dict[str, str] | None = None
    created_at_ms: int
    updated_at_ms: int
    trigger_kind: str
    strategy_id: str
    strategy_version: str
    case_state: str
    regime: str | None = None
    policy_decision: str | None = None
    policy_reason: str | None = None
    pre_move_bps: int | None = None
    strategy_config: dict[str, str] = Field(default_factory=dict)
    regime_reason: str | None = None
    case_observed_at_ms: int | None = None


class TradingIntentsData(ExactApiSchema):
    intents: list[TradingIntentData] = Field(default_factory=list)
    cases_without_intents: list[TradingCaseData] = Field(default_factory=list)
    complete: bool
    window_hours: int
    measured_at_ms: int


class TradingGateDecisionData(ExactApiSchema):
    source_key: str
    event_id: str | None = None
    underlying_key: str | None = None
    base_symbol: str = ""
    trigger_kind: str
    source_observed_at_ms: int
    gate_status: Literal["DEFERRED", "REJECTED", "CASE_CREATED", "EXPIRED"] | None = None
    gate_stage: Literal["source", "eligibility", "routing", "market_context", "freeze"] | None = None
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
    decisions: list[TradingGateDecisionData] = Field(default_factory=list)
    complete: bool
    window_hours: int
    measured_at_ms: int


class TradingEventCaseData(ExactApiSchema):
    event_id: str
    joinable: bool
    gate_status: Literal["DEFERRED", "REJECTED", "CASE_CREATED", "EXPIRED"] | None = None
    gate_stage: Literal["source", "eligibility", "routing", "market_context", "freeze"] | None = None
    gate_reason: str | None = None
    gate_retryable: bool | None = None
    gate_version: str | None = None
    gate_config_digest: str | None = None
    gate_evidence: TradingGateEvidenceData | None = None
    gate_first_evaluated_at_ms: int | None = None
    gate_last_evaluated_at_ms: int | None = None
    gate_attempt_count: int | None = None
    case: TradingCaseData | None = None
    intent: TradingIntentData | None = None


__all__ = [name for name in globals() if name.startswith("Trading")]
