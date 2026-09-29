"""Read-only trading monitoring contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from tracefold.trading.stages import ExecutionStage

from .common import ExactApiSchema


class TradingDecisionRuntimeData(ExactApiSchema):
    """Analysis process liveness and the last durable Case for this deployment."""

    last_case_at_ms: int | None = None
    state: Literal["disabled", "unavailable", "model_unconfigured", "running"]
    active_policy: str
    model_name: str | None = None
    publish_signals: bool
    config_digest: str | None = None
    heartbeat_at_ms: int | None = None


class TradingSignedPositionData(ExactApiSchema):
    symbol: str
    positionSide: str | None = None
    positionAmt: str
    entryPrice: str | None = None
    markPrice: str | None = None
    unRealizedProfit: str | None = None
    owned: bool


class TradingSignedOrderData(ExactApiSchema):
    symbol: str
    clientOrderId: str
    side: str | None = None
    origQty: str | None = None
    reduceOnly: bool | None = None
    status: str | None = None
    owned: bool


class TradingSignedAlgoData(ExactApiSchema):
    symbol: str
    clientAlgoId: str
    orderType: str | None = None
    triggerPrice: str | None = None
    quantity: str | None = None
    closePosition: bool | None = None
    reduceOnly: bool | None = None
    algoStatus: str | None = None
    owned: bool


class TradingSignedAccountData(ExactApiSchema):
    """Bounded projection of one complete signed DEMO REST account read."""

    observed_at_ns: int
    equity_usdt: str
    positions_total: int = Field(ge=0)
    orders_total: int = Field(ge=0)
    algos_total: int = Field(ge=0)
    complete: bool
    positions: list[TradingSignedPositionData] = Field(max_length=100)
    orders: list[TradingSignedOrderData] = Field(max_length=100)
    algos: list[TradingSignedAlgoData] = Field(max_length=100)


class TradingExecutionReadinessData(ExactApiSchema):
    """Executor heartbeat, admission state and the last signed DEMO account read."""

    configured_connection: Literal["LIVE", "DEMO", "TESTNET", "SDK_DEFAULT"]
    connection: Literal["DEMO"] | None = None
    connection_observed_at_ms: int | None = None
    account_slot: str
    alive: bool
    entries_armed: bool
    entry_block_reason: str | None = None
    entries_paused: bool = True
    emergency_halted: bool = False
    unexpected_exposure: bool = False
    last_error: str | None = None
    heartbeat_at_ms: int | None = None
    facts_expire_at_ms: int | None = None
    facts_remaining_ms: int | None = None
    last_full_reconcile_at_ms: int | None = None
    signed_account: TradingSignedAccountData | None = None


class TradingStatusData(ExactApiSchema):
    """The desk's RISK block, and nothing beside it.

    `counts` carried a `cases_24h` and a `signals_24h` that cost two `count(*)` on every 15 s poll of
    every route and were rendered only in the chrome figures #537 PR-5 deleted. The window and the
    measurement clock went with them: this projection publishes the instant it expires, which is the
    only clock a reader compares (#537 PR-5).
    """

    decision: TradingDecisionRuntimeData
    execution: TradingExecutionReadinessData


class TradingPolicyCheckData(ExactApiSchema):
    check: str
    operator: str
    threshold: str
    measured: str | None = None
    passed: bool


class TradingAnalysisDecisionData(ExactApiSchema):
    decision_id: str
    policy_id: str
    policy_version: str
    assessment_ref: str | None = None
    action: str
    decision: dict[str, Any]
    publish_status: str
    publish_reason: str | None = None
    decided_at_ms: int
    valid_until_ms: int


class TradingAnalysisOutcomeData(ExactApiSchema):
    axis: str
    horizon_seconds: int
    label_version: str
    status: str
    return_bps: str | None = None
    available_at_ms: int
    labeled_at_ms: int | None = None
    path_ref: str | None = None


class TradingPhysicalModelCallData(ExactApiSchema):
    claim_attempt: int
    call_index: int
    status: str
    phase: str | None = None
    endpoint: str | None = None
    requested_model: str | None = None
    served_model: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    timeout_ms: int | None = None
    remaining_deadline_ms: int | None = None
    reserved_cost_microusd: int | None = None
    request_ref: str | None = None
    response_ref: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_microusd: int | None = None
    cost_unknown_reason: str | None = None


class TradingAnalysisAttemptData(ExactApiSchema):
    case_id: str
    claim_attempt: int
    brief_ref: str | None = None
    evidence_ref: str | None = None
    assessment_ref: str | None = None
    final_manifest_ref: str | None = None
    termination_reason: str | None = None
    model_name: str | None = None
    prompt_sha: str | None = None
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    provider_status: str | None = None
    analysis_status: str
    error_code: str | None = None
    validation_errors: list[dict[str, str]] = Field(default_factory=list)
    physical_call_count: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_microusd: int | None = None
    known_cost_microusd: int = 0
    unknown_cost_calls: int = 0
    cost_upper_estimate_microusd: int | None = None
    cost_unknown_reason: str | None = None
    settled: bool
    physical_calls: list[TradingPhysicalModelCallData] = Field(default_factory=list)


class TradingWatchObservationData(ExactApiSchema):
    parent_case_id: str
    trigger_id: str
    condition: dict[str, Any]
    status: str
    trigger_side: str | None = None
    last_observation_status: str | None = None
    last_observed_at_ms: int | None = None
    last_observation_ref: str | None = None
    last_observed_value: str | None = None
    next_check_at_ms: int
    expires_at_ms: int
    child_case_id: str | None = None
    created_at_ms: int
    updated_at_ms: int


class TradingRootChainCaseData(ExactApiSchema):
    case_id: str
    run_kind: str | None = None
    recheck_seq: int | None = None
    state: str
    analysis_status: str | None = None
    created_at_ms: int
    decided_at_ms: int | None = None
    action: str | None = None
    publish_status: str | None = None
    side: str | None = None


class TradingCaseData(ExactApiSchema):
    """One frozen Case, as the drawer behind `?case=<id>` renders it.

    The four measured OI numbers here were a second copy of what `policy_checks` already carries with
    the threshold each was measured against, `policy_version` a second copy of `policy_id`, and
    `policy_decision` a required Literal over a nullable column -- exactly the shape that turned a
    stored `NULL` into a 500 on a read route (#532, #537 PR-5). `policy_config` was the same duplicate
    one level up: the frozen dictionary it published is where `policy_checks[].threshold` comes from,
    so every number that was actually tested is already on the row beside what it was measured against,
    and `policy_config_digest` still identifies the whole set (#604 T3). `state` and `policy_reason`
    are the terminal answer; `base_symbol` is the identity the drawer titles itself with.
    """

    case_id: str
    latest_case_id: str | None = None
    source_item_id: str | None = None
    event_id: str | None = None
    base_symbol: str
    trigger_kind: str | None = None
    market_key: str | None = None
    manifest_version: str | None = None
    # The manifest's own policy identity. Nullable because the manifest is the only writer of it and a
    # Case whose manifest names no policy must render as that, not 500 the route (#532, #537 PR-3).
    policy_id: str | None = None
    policy_config_digest: str | None = None
    policy_checks: list[TradingPolicyCheckData] = Field(default_factory=list)
    state: str
    policy_reason: str | None = None
    mark_price: str | None = None
    pre_move_bps: int | None = None
    observed_at_ms: int
    created_at_ms: int
    decided_at_ms: int | None = None
    trigger_id: str | None = None
    target_asset_id: str | None = None
    target_selection: dict[str, Any] | None = None
    entry_scope_id: str | None = None
    mapping_semantics_digest: str | None = None
    analysis_status: str | None = None
    analysis_action: str | None = None
    analysis_publish_status: str | None = None
    analysis_side: str | None = None
    evidence_ref: str | None = None
    analysis_decision: TradingAnalysisDecisionData | None = None
    analysis_outcomes: list[TradingAnalysisOutcomeData] = Field(default_factory=list)
    analysis_attempts: list[TradingAnalysisAttemptData] = Field(default_factory=list)
    watch_observation: TradingWatchObservationData | None = None
    root_chain: list[TradingRootChainCaseData] = Field(default_factory=list)
    review_mode: Literal["none", "event_wait", "research_note"] = "none"
    run_kind: str | None = None
    recheck_seq: int | None = None
    root_expires_at_ms: int | None = None


class TradingAdmissionCountData(ExactApiSchema):
    """How many frames admission answered this way in the window.

    A count, not a row: #589 PR-2 deleted a `decisions[]` that published one object per frame with its
    whole evidence blob, 400 of them on every 15 s poll, and nothing rendered them. This is the
    distribution the desk's funnel draws its top from -- at most a dozen `(status, reason)` pairs
    whatever the window holds -- and no frame identity, evidence or Case link travels with it.
    `reason` is nullable because the ledger's own column is.
    """

    status: str
    reason: str | None = None
    count: int = Field(ge=0)


class TradingDecisionCountData(ExactApiSchema):
    """Agent outcome and publication status for Cases created in the 24 h window."""

    action: str
    publish_status: str
    count: int = Field(ge=0)


class TradingCasesData(ExactApiSchema):
    """The Case behind `?case_id=<id>`, plus the three durable 24 h distributions.

    There is no `next_cursor` and no cursor parameter: the desk opens one Case at a time from
    `?case=<id>` and renders one 24 h count card, and no reader ever asked for a second page (#537 PR-5).
    `cases` is that one Case or nothing at all: without `case_id` it is empty, because the unconditional
    100-row page this route used to send on every poll was rendered by nothing and could not reach the
    `NO_TRADE` Cases an operator most wants to open (#604 T3). `complete` still says the answer was not
    truncated, which for a primary-key read it never is.
    """

    cases: list[TradingCaseData] = Field(default_factory=list)
    total: int = 0
    next_cursor: str | None = None
    window_from_ms: int = 0
    window_to_ms: int = 0
    state_counts_24h: dict[str, int] = Field(default_factory=dict)
    decision_counts_24h: list[TradingDecisionCountData] = Field(default_factory=list)
    admission_counts_24h: list[TradingAdmissionCountData] = Field(default_factory=list, max_length=64)
    complete: bool
    window_hours: int


class TradingAnalysisReplayData(ExactApiSchema):
    case_id: str
    status: str
    selected_attempt: int | None = None
    source_fact: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    assessment: dict[str, Any] | None = None
    final_manifest: dict[str, Any] | None = None
    tool_observations: list[dict[str, Any]] = Field(default_factory=list)
    decision: TradingAnalysisDecisionData | None = None
    attempts: list[TradingAnalysisAttemptData] = Field(default_factory=list)


class TradingExecutionRowData(ExactApiSchema):
    """One disposition or plan, folded from the executor's durable venue evidence."""

    source: Literal["signal", "manual"]
    entry_id: str
    case_id: str | None = None
    market_key: str
    direction: Literal["long", "short"]
    observed_at_ns: int
    disposition_reason: str | None = None
    fill_quantity: str | None = None
    fill_avg_price: str | None = None
    stop_trigger_price: str | None = None
    take_profit_trigger_price: str | None = None
    entry_filled_at_ns: int | None = None
    position_closed_at_ns: int | None = None
    exit_price: str | None = None
    realized_pnl_usd: str | None = None
    fees_usd: str | None = None
    net_pnl_usd: str | None = None
    exit_reason: str | None = None
    pnl_status: Literal["pending", "complete", "evidence_incomplete"] | None = None
    plan_status: str | None = None
    account_slot: str | None = None
    instrument_id: str | None = None
    entry_client_order_id: str | None = None
    entry_error_code: int | None = None
    stop_distance_bps: int | None = None
    take_profit_bps: int | None = None
    max_holding_ns: int | None = None
    duration_ns: int | None = None
    stage: ExecutionStage


class TradingRealizedTotalsData(ExactApiSchema):
    """What this account slot has realized, over the current UTC day and over its whole ledger.

    Both sums fold the fill journal of every plan the slot opened and closed, manual entries included,
    because a manual entry is a retained trade in this account. A closed plan whose fills cannot yield
    a result is counted as missing rather than as zero. Decimal strings, like every other money field.
    """

    realized_known_today_usd: str | None
    realized_known_total_usd: str | None
    net_known_today_usd: str | None
    net_known_total_usd: str | None
    net_known_today: int = Field(ge=0)
    net_known_total: int = Field(ge=0)
    net_missing_today: int = Field(ge=0)
    net_missing_total: int = Field(ge=0)
    pnl_known_today: int = Field(ge=0)
    pnl_known_total: int = Field(ge=0)
    pnl_missing_today: int = Field(ge=0)
    pnl_missing_total: int = Field(ge=0)
    closed_today: int = Field(ge=0)
    closed_total: int = Field(ge=0)


class TradingExecutionsData(ExactApiSchema):
    executions: list[TradingExecutionRowData] = Field(default_factory=list)
    totals: TradingRealizedTotalsData
    complete: bool


__all__ = [name for name in globals() if name.startswith("Trading")]
