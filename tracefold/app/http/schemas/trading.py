"""Read-only Source, Case, Signal, Observation, and readiness contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import ExactApiSchema

GateStatus = Literal["DEFERRED", "REJECTED", "RESEARCH_ONLY", "CASE_CREATED", "EXPIRED"]
GateStage = Literal["source", "venue", "eligibility", "capability", "catalog", "routing", "market_context", "freeze"]


class TradingDecisionRuntimeData(ExactApiSchema):
    state: Literal["DISABLED", "STARTING", "RUNNING", "FAULTED"]
    heartbeat_at_ms: int | None = None
    reason: str | None = None


class TradingExecutionReadinessData(ExactApiSchema):
    mode: Literal["disabled", "paper", "live"]
    profile_id: str
    account_slot: str
    ready: bool
    reason: Literal["disabled", "activation_not_available_before_433e"]


class TradingRuntimeCountsData(ExactApiSchema):
    cases_24h: int = 0
    signals_24h: int = 0
    no_trade_24h: int = 0
    blocked_24h: int = 0
    cases_open: int = 0
    signals_unexpired: int = 0


class TradingAlphaIdentityData(ExactApiSchema):
    policy_id: str
    policy_version: str
    config_digest: str
    contract_sha256: str
    config: dict[str, str] = Field(default_factory=dict)


class TradingStatusData(ExactApiSchema):
    decision: TradingDecisionRuntimeData
    execution: TradingExecutionReadinessData
    alpha: TradingAlphaIdentityData
    counts: TradingRuntimeCountsData
    window_hours: int
    measured_at_ms: int


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
    holds: str = ""
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
    source_venues: list[str]


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
    event_id: str
    joinable: bool
    decision: TradingGateDecisionData | None = None


class TradingPolicyCheckData(ExactApiSchema):
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
    market_key: str | None = None
    source_venue: str | None = None
    trigger_kind: str
    manifest_version: str | None = None
    policy_id: str
    policy_version: str
    policy_config_digest: str
    policy_config: dict[str, str] = Field(default_factory=dict)
    policy_checks: list[TradingPolicyCheckData] = Field(default_factory=list)
    state: str
    policy_decision: Literal["long", "no_trade", "not_run"]
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


class TradingCasesData(ExactApiSchema):
    cases: list[TradingCaseData] = Field(default_factory=list)
    state_counts_24h: dict[str, int] = Field(default_factory=dict)
    reason_counts_24h: dict[str, int] = Field(default_factory=dict)
    complete: bool
    next_cursor: str | None = None
    window_hours: int
    measured_at_ms: int


class TradingSignalData(ExactApiSchema):
    seq: int
    signal_id: str
    case_id: str
    alpha_contract_sha256: str
    market_key: str
    direction: Literal["long", "short"]
    observed_at_ns: int
    expires_at_ns: int
    expired: bool
    evidence_sha256: str
    alpha_metadata: dict[str, str | int | bool] = Field(default_factory=dict)


class TradingSignalsData(ExactApiSchema):
    signals: list[TradingSignalData] = Field(default_factory=list)
    complete: bool
    next_cursor: str | None = None
    window_hours: int
    measured_at_ms: int


class TradingExecutionObservationData(ExactApiSchema):
    seq: int
    event_id: str
    runtime_profile_id: str
    runtime_release: str
    execution_strategy: str
    signal_id: str | None = None
    command_id: str | None = None
    normalized_kind: str
    occurred_at_ns: int
    observed_at_ns: int
    native_identity_references: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    payload_digest: str


class TradingExecutionObservationsData(ExactApiSchema):
    observations: list[TradingExecutionObservationData] = Field(default_factory=list)
    complete: bool
    next_cursor: str | None = None
    window_hours: int
    measured_at_ms: int


class TradingOperatorIntentData(ExactApiSchema):
    seq: int
    command_id: str
    target_profile_id: str
    action: Literal["pause_entries", "resume_entries", "emergency_halt", "flatten", "manual_entry"]
    scope: str
    reason: str
    operator_identity: str
    requested_at_ns: int
    expires_at_ns: int
    expired: bool
    confirmed: bool
    market_key: str | None = None
    direction: Literal["long", "short"] | None = None
    disposition: str | None = None
    disposition_reason: str | None = None


class TradingOperatorIntentsData(ExactApiSchema):
    commands: list[TradingOperatorIntentData] = Field(default_factory=list)
    complete: bool
    next_cursor: str | None = None
    window_hours: int
    measured_at_ms: int


__all__ = [name for name in globals() if name.startswith("Trading")]
