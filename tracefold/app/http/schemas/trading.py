"""Read-only capital-lane HTTP contract, one schema family per durable aggregate (#331).

    Source / Admission   `TradingGateData`, `TradingGateDecisionData`
    Case / Decision      `TradingCasesData`, `TradingCaseData`
    Intent / Outcome     `TradingIntentsData`, `TradingIntentData`
    orthogonal runtime   `TradingStatusData`

Nothing crosses. A Case carries its own frozen policy and Capital attribution with no execution
lifecycle; an Intent carries its lifecycle and only a `case_id` back-reference; status carries
Decision, Capital, per-binding redacted facts, and bounded durable totals.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ExactApiSchema

GateStatus = Literal["DEFERRED", "REJECTED", "RESEARCH_ONLY", "CASE_CREATED", "EXPIRED"]
# `capability` and `routing` are read-only history from earlier Admission contracts. Current writers
# use the remaining stages, and the 0327 database trigger rejects every new `capability` byte.
GateStage = Literal["source", "venue", "eligibility", "capability", "catalog", "routing", "market_context", "freeze"]


# ---------------------------------------------------------------------------- Decision / Capital / binding runtime
class TradingBudgetData(ExactApiSchema):
    """What one thesis may cost. The lane's bound is serialisation, not a daily count (#348).

    `max_entries_per_utc_day` is gone rather than set to some larger number: there is no daily count any
    more, and publishing a ceiling nobody enforces is worse than publishing none.
    """

    target_notional_usd: str


class TradingDecisionRuntimeData(ExactApiSchema):
    state: Literal["DISABLED", "STARTING", "RUNNING", "FAULTED"]
    heartbeat_at_ms: int | None = None
    reason: str | None = None


class TradingCapitalRuntimeData(ExactApiSchema):
    control: Literal["RUNNING", "CLOSE_ONLY", "PAUSED"]
    blacklist_revision: int = Field(ge=0)
    arm_epoch: int = Field(ge=1)


class TradingBindingRuntimeData(ExactApiSchema):
    binding: Literal["BINANCE_USDM", "HYPERLIQUID_PERP"]
    execution_enabled: bool
    execution_environment: Literal["demo"] | None = None
    credential_state: Literal["unconfigured", "configured", "invalid"]
    credential_fingerprint: str | None = None
    runtime_state: Literal["stopped", "starting", "ready", "stale", "faulted"]
    account_state: Literal["unknown", "reconciled_flat", "exposure_present"]
    account_generation: int = Field(ge=0)
    catalog_state: Literal["missing", "ready", "stale", "error"]
    catalog_snapshot_sha256: str | None = None
    catalog_captured_at_ms: int | None = None
    capability_state: Literal["missing", "ready", "stale", "error"]
    capability_snapshot_sha256: str | None = None
    capability_compiled_at_ms: int | None = None
    capability_compile_error: str | None = None
    execution_binding_sha256: str | None = None
    active_arm_receipt_sha256: str | None = None
    heartbeat_at_ms: int | None = None
    reason: str | None = None


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


class TradingNautilusRuntimePlanData(ExactApiSchema):
    decision: Literal["blocked", "optional", "required"]
    reason: str
    execution_environment: Literal["binance_usdm_demo"]
    enabled_bindings: list[Literal["BINANCE_USDM"]]
    disabled_bindings: list[Literal["HYPERLIQUID_PERP"]]
    ready: bool
    readiness_reason: str


class TradingStatusData(ExactApiSchema):
    budget: TradingBudgetData
    decision: TradingDecisionRuntimeData
    capital: TradingCapitalRuntimeData
    nautilus: TradingNautilusRuntimePlanData
    bindings: list[TradingBindingRuntimeData]
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
    age_ms: int | None = None
    max_age_ms: int | None = None
    # Which side holds the busy issuer, for the merged `underlying_busy` refusal (#348).
    holds: str = ""
    # Read-only history. `limit`, `since_close_ms` and `cooldown_ms` were written by
    # `rank_above_limit` and `cooldown`, both retired by #348. No writer can produce them again, but
    # the ledger keeps 90 days of rows that carry them and this schema is `extra="forbid"` — dropping
    # the fields would make every one of those rows a 500 instead of a readable historical answer.
    limit: int | None = None
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
    source_native_bindings: dict[str, str]


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
    policy_decision: Literal["long", "no_trade", "not_run"]
    policy_reason: str | None = None
    capital_disposition: Literal["allowed", "blocked", "not_applicable"]
    capital_reason: str | None = None
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
    capital_reason_counts_24h: dict[str, int] = Field(default_factory=dict)
    complete: bool
    next_cursor: str | None = None
    window_hours: int
    measured_at_ms: int


# ---------------------------------------------------------------------------- Intent / Outcome
class TradingIntentData(ExactApiSchema):
    intent_id: str
    intent_version: Literal["trade_intent_v1", "trade_intent_v2", "trade_intent_v3"]
    case_id: str
    event_id: str | None = None
    underlying_key: str
    base_symbol: str
    execution_environment: Literal["BINANCE_USDM_DEMO"] | None = None
    source_venue: Literal["binance.usdm", "hyperliquid.perp"] | None = None
    source_identity: str | None = None
    canonical_asset: str | None = None
    binding: Literal["BINANCE_USDM", "HYPERLIQUID_PERP"] | None = None
    account_generation: int | None = None
    execution_binding_sha256: str | None = None
    venue_catalog_snapshot_sha256: str | None = None
    execution_capability_snapshot_sha256: str | None = None
    capability_entry_id: str | None = None
    provider_instrument_id: str | None = None
    settlement_asset: str | None = None
    intent_policy_sha256: str | None = None
    execution_policy_sha256: str | None = None
    quote_contract_sha256: str | None = None
    protection_contract_sha256: str | None = None
    capital_authorization_receipt_sha256: str | None = None
    blacklist_revision_at_emission: int | None = None
    blacklist_snapshot_sha256_at_emission: str | None = None
    instrument_id: str
    side: Literal["long"]
    target_notional_usd: str | None = None
    target_notional: str | None = None
    max_risk_amount: str | None = None
    risk_currency: str | None = None
    leverage: int | None = None
    economic_lifecycle_id: str | None = None
    entry_leg_id: str | None = None
    protection_leg_id: str | None = None
    close_leg_id: str | None = None
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
    funding_by_currency: dict[str, str] | None = None
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
    next_cursor: str | None = None
    window_hours: int
    measured_at_ms: int


# ---------------------------------------------------------------------------- Capability partition
class TradingCapabilityBindingData(ExactApiSchema):
    binding: Literal["BINANCE_USDM", "HYPERLIQUID_PERP"]
    capability_state: Literal["missing", "ready", "stale", "error"]
    snapshot_sha256: str | None = None
    catalog_snapshot_sha256: str | None = None
    catalog_instrument_count: int = 0
    included_count: int = 0
    excluded_count: int = 0
    partition_sha256: str | None = None
    compiled_at_ms: int | None = None
    compile_error: str | None = None
    last_known_good: bool = False


class TradingCapabilityEntryData(ExactApiSchema):
    binding: Literal["BINANCE_USDM", "HYPERLIQUID_PERP"]
    catalog_entry_id: str
    disposition: Literal["included", "excluded"]
    provider_instrument_id: str
    instrument_id: str | None = None
    canonical_asset: str | None = None
    canonical_namespace: str | None = None
    settlement_asset: str | None = None
    price_increment: str | None = None
    size_increment: str | None = None
    min_quantity: str | None = None
    min_notional: str | None = None
    exclusion_reason: str | None = None


class TradingCapabilitiesData(ExactApiSchema):
    bindings: list[TradingCapabilityBindingData] = Field(default_factory=list)
    entries: list[TradingCapabilityEntryData] = Field(default_factory=list)
    complete: bool
    next_cursor: str | None = None
    measured_at_ms: int


# ---------------------------------------------------------------------------- Capital evidence
class TradingSettlementRiskLimitData(ExactApiSchema):
    settlement_asset: Literal["USDT", "USDC"]
    max_planned_risk_amount: str
    max_realized_loss_amount: str
    fee_slippage_reserve_bps: int


class TradingAuthorityEvidenceData(ExactApiSchema):
    binding: Literal["BINANCE_USDM", "HYPERLIQUID_PERP"]
    status: Literal["absent", "active", "expired", "revoked", "invalid"]
    active_arm_receipt_sha256: str | None = None
    arm_expires_at_ms: int | None = None
    grant_sha256: str | None = None
    grant_expires_at_ms: int | None = None
    risk_policy_sha256: str | None = None
    risk_policy_expires_at_ms: int | None = None
    approved_release: str | None = None
    settlement_limits: list[TradingSettlementRiskLimitData] = Field(default_factory=list)


class TradingCapitalLifecycleEvidenceData(ExactApiSchema):
    reservation_sha256: str
    authorization_receipt_sha256: str
    case_id: str
    intent_id: str
    economic_lifecycle_id: str
    binding: Literal["BINANCE_USDM", "HYPERLIQUID_PERP"]
    settlement_asset: Literal["USDT", "USDC"]
    risk_policy_sha256: str
    grant_sha256: str
    arm_receipt_sha256: str
    risk_day_start_ms: int
    risk_day_end_ms: int
    target_notional: str
    initial_planned_risk_amount: str
    current_planned_risk_amount: str
    risk_status: Literal["RESERVED", "FENCED", "OPEN", "MANUAL_REVIEW", "RELEASED", "SETTLED"]
    attempt_consumed: bool
    attempt_day_start_ms: int | None = None
    attempt_day_end_ms: int | None = None
    settlement_known: bool
    execution_state: Literal["PENDING", "IN_FLIGHT", "OPEN_PROTECTED", "MANUAL_REVIEW", "TERMINAL"]
    execution_phase: Literal["ENTRY", "PROTECTION", "EXIT"] | None = None
    terminal_outcome: Literal["EXPIRED", "REJECTED", "CLOSED_FLAT"] | None = None
    reason_code: str | None = None
    flat_verified_at_ms: int | None = None
    updated_at_ms: int


class TradingEvidenceData(ExactApiSchema):
    authorities: list[TradingAuthorityEvidenceData] = Field(default_factory=list)
    lifecycles: list[TradingCapitalLifecycleEvidenceData] = Field(default_factory=list)
    complete: bool
    next_cursor: str | None = None
    measured_at_ms: int


__all__ = [name for name in globals() if name.startswith("Trading")]
