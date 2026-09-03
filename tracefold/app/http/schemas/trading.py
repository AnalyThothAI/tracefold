"""Trading read contracts plus the bounded operator-command request and receipt."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from tracefold.trading import CommandStage, ExecutionStage

from .common import ExactApiSchema

# The admission ledger's two closed vocabularies, exactly as `trading_candidate_gate_status_check` and
# `trading_candidate_gate_stage_check` admit them and `AdmissionStatus` / `AdmissionStage` write them.
# `GET /api/trading/gate/{event_id}` reads one stored row with no time bound, so a value here that the
# database can hold but this Literal cannot would turn opening that Event into a 500.
GateStatus = Literal["DEFERRED", "REJECTED", "CASE_CREATED", "EXPIRED"]
GateStage = Literal["source", "venue", "eligibility", "market_context", "freeze"]


class TradingDecisionRuntimeData(ExactApiSchema):
    """When the Signal lane last froze a Case; `None` means it has not frozen one yet (#520)."""

    last_case_at_ms: int | None = None


class TradingExecutionPositionData(ExactApiSchema):
    position_id: str
    instrument_id: str
    side: Literal["long", "short"]
    quantity: str
    entry_price: str
    mark_price: str | None = None
    unrealized_pnl_usd: str | None = None
    owned: bool
    protection_status: Literal["protected", "pending", "unprotected", "unknown"]
    protection_quantity: str | None = None
    protection_trigger_price: str | None = None
    protection_full_coverage: bool


class TradingExecutionOrderData(ExactApiSchema):
    client_order_id: str
    instrument_id: str
    state: Literal["open", "inflight"]
    leg: Literal["entry", "exit", "protection", "unknown"]
    quantity: str
    reduce_only: bool
    trigger_price: str | None = None
    owned: bool


class TradingExecutionAccountData(ExactApiSchema):
    observed_at_ns: int
    market_observed_at_ns: int | None = None
    equity_usd: str | None = None
    day_start_equity_usd: str | None = None
    daily_drawdown_usd: str | None = None
    daily_drawdown_bps: int | None = None
    aggregate_risk_usd: str | None = None
    positions: list[TradingExecutionPositionData] = Field(default_factory=list, max_length=100)
    orders: list[TradingExecutionOrderData] = Field(default_factory=list, max_length=200)
    open_orders_count: int = Field(ge=0)
    inflight_orders_count: int = Field(ge=0)
    unknown_orders_count: int = Field(ge=0)
    complete: bool
    truncated: bool = False
    audit_healthy: bool = True
    audit_failure_reason: str | None = None


class TradingExecutionReadinessData(ExactApiSchema):
    mode: Literal["disabled", "paper", "live"]
    account_slot: str
    alive: bool
    execution_safe: bool
    entries_armed: bool
    entry_block_reason: str | None = None
    runtime_release: str | None = None
    config_sha256: str | None = None
    runtime_revision: str | None = None
    image_digest: str | None = None
    credential_fingerprint: str | None = None
    lifecycle_state: Literal["starting", "running", "stopping", "stopped", "failed"] | None = None
    heartbeat_at_ns: int | None = None
    reconciliation_observed_at_ns: int | None = None
    reconciliation_age_ms: int | None = None
    startup_reconciled: bool = False
    entries_paused: bool = True
    emergency_halted: bool = False
    unexpected_exposure: bool = False
    account_flat: bool = False
    account_flat_proven: bool = False
    positions_count: int = Field(default=0, ge=0)
    open_orders_count: int = Field(default=0, ge=0)
    protection_status: Literal["not_applicable", "protected", "pending", "unprotected", "unknown"] = "unknown"
    routes_count: int = Field(default=0, ge=0)
    # The instant this projection stops being current: the earlier of the heartbeat and private
    # reconciliation freshness budgets. `None` when there is no Runtime row to age.
    facts_expire_at_ms: int | None = None
    current_account: TradingExecutionAccountData | None = None


class TradingRuntimeCountsData(ExactApiSchema):
    cases_24h: int = 0
    signals_24h: int = 0


class TradingStatusData(ExactApiSchema):
    decision: TradingDecisionRuntimeData
    execution: TradingExecutionReadinessData
    counts: TradingRuntimeCountsData
    window_hours: int
    measured_at_ms: int


# Every key any writer has ever put in `evidence`, because the admission ledger keeps rows for 90 days.
# `source_decision`, `source_rule`, `blacklist_reason`, `cooldown_ms`, `since_close_ms`, `limit` and
# `live_exchange_id` have no writer left — the first two went with the News judgment fields in #510 —
# and they stay declared because this model forbids extras and stored rows still carry them. A row
# written before a rule retired must keep rendering; that is what makes the ledger a ledger. The note
# is a comment rather than a docstring so it does not become a public OpenAPI description.
class TradingGateEvidenceData(ExactApiSchema):
    venue: str = ""
    oi_change_bps: int | None = None
    oi_value_usd: int | None = None
    whale_oi_ratio_bps: int | None = None
    whale_long_profit_bps: int | None = None
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
    market_key: str
    direction: Literal["long", "short"]
    observed_at_ns: int
    expires_at_ns: int
    expired: bool
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
    account_slot: str
    runtime_release: str
    execution_strategy: str
    signal_id: str | None = None
    command_id: str | None = None
    normalized_kind: str
    occurred_at_ns: int
    observed_at_ns: int
    native_identity_references: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class TradingExecutionObservationsData(ExactApiSchema):
    observations: list[TradingExecutionObservationData] = Field(default_factory=list)
    complete: bool
    next_cursor: str | None = None
    window_hours: int
    measured_at_ms: int


class TradingOperatorIntentData(ExactApiSchema):
    seq: int
    command_id: str
    account_slot: str
    action: Literal["pause_entries", "resume_entries", "emergency_halt", "flatten", "manual_entry"]
    scope: str
    reason: str
    operator_identity: str
    requested_at_ns: int
    expires_at_ns: int
    expired: bool
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


class TradingExecutionRowData(ExactApiSchema):
    """One entry identity's whole execution, folded from its own observations (#528 PR-1, PR-3).

    `entry_id` is the identity the Runtime correlates the venue facts under: a Signal's `signal_id`,
    or the `command_id` of a manual entry, which `source` tells apart. A manual entry has no Case, so
    `case_id` is absent on those rows rather than invented.
    """

    source: Literal["signal", "manual"]
    entry_id: str
    case_id: str | None = None
    market_key: str
    direction: Literal["long", "short"]
    observed_at_ns: int
    disposition: Literal["accepted", "rejected"] | None = None
    disposition_reason: str | None = None
    order_status: str | None = None
    fill_quantity: str | None = None
    fill_avg_price: str | None = None
    stop_trigger_price: str | None = None
    position_status: str | None = None
    exit_price: str | None = None
    realized_pnl_usd: str | None = None
    exit_reason: Literal["stop_filled", "flatten", "unclaimed_flatten"] | None = None
    stage: ExecutionStage
    last_observed_at_ns: int


class TradingExecutionCommandRowData(ExactApiSchema):
    """One operator Command's progress, read from its `control_disposition` alone."""

    command_id: str
    action: Literal["pause_entries", "resume_entries", "emergency_halt", "flatten", "manual_entry"]
    reason: str
    requested_at_ns: int
    operator_identity: str
    stage: CommandStage


class TradingExecutionsData(ExactApiSchema):
    executions: list[TradingExecutionRowData] = Field(default_factory=list)
    commands: list[TradingExecutionCommandRowData] = Field(default_factory=list)
    complete: bool
    window_hours: int
    measured_at_ms: int


class TradingOperatorCommandRequestData(ExactApiSchema):
    request_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    requested_at_ms: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=512)


class TradingOperatorCommandReceiptData(ExactApiSchema):
    command_id: str
    seq: int
    requested_at_ns: int
    disposition: Literal["awaiting_runtime"]
    reason: str | None = None
    truth: Literal["intent_recorded_not_runtime_or_venue"]


__all__ = [name for name in globals() if name.startswith("Trading")]
