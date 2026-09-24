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


class TradingExecutionPositionData(ExactApiSchema):
    position_id: str
    instrument_id: str
    side: Literal["long", "short"]
    quantity: str
    entry_price: str
    mark_price: str | None = None
    unrealized_pnl_usd: str | None = None
    # Whether a non-terminal plan claims this instrument; exposure no plan claims blocks new entries.
    owned: bool
    # The reduce-only stop and take-profit resting against this position, as the Nautilus Cache
    # holds them; `None` where there is none.
    stop_trigger_price: str | None = None
    take_profit_trigger_price: str | None = None


class TradingExecutionOrderData(ExactApiSchema):
    client_order_id: str
    instrument_id: str
    state: Literal["open", "inflight"]
    leg: Literal["entry", "stop", "take_profit", "exit", "unknown"]
    quantity: str
    reduce_only: bool
    trigger_price: str | None = None
    owned: bool


class TradingExecutionAccountData(ExactApiSchema):
    """What the account holds, read from the Nautilus Cache the Runtime executes against (#680).

    Nautilus reconciles that Cache with the venue at start and every five seconds after; this is the
    Runtime's own picture, published whole. `complete` says every position could be marked and the
    balance was known, so equity and the drawdown are whole numbers.
    """

    equity_usd: str | None = None
    daily_drawdown_usd: str | None = None
    daily_drawdown_bps: int | None = None
    positions: list[TradingExecutionPositionData] = Field(default_factory=list, max_length=100)
    orders: list[TradingExecutionOrderData] = Field(default_factory=list, max_length=200)
    open_orders_count: int = Field(ge=0)
    inflight_orders_count: int = Field(ge=0)
    complete: bool


class TradingExecutionReadinessData(ExactApiSchema):
    """One field per operator question, and the CLI `tracefold trading status` block is this same dict.

    `execution_safe`, `startup_reconciled`, `reconciliation_age_ms` and `account_flat_proven` answered
    questions about the Runtime's private account proof, and went with it (#680): Nautilus reconciles
    the venue before the Strategy starts, so a fresh heartbeat is the freshness of `current_account`.
    """

    mode: Literal["disabled", "paper", "live"]
    account_slot: str
    alive: bool
    entries_armed: bool
    entry_block_reason: str | None = None
    entries_paused: bool = True
    emergency_halted: bool = False
    unexpected_exposure: bool = False
    protection_status: Literal["not_applicable", "protected", "unprotected"] = "not_applicable"
    routes_count: int = Field(default=0, ge=0)
    # The instant this projection stops being current: the Runtime heartbeat's freshness budget.
    # `None` when there is no Runtime row to age.
    facts_expire_at_ms: int | None = None
    current_account: TradingExecutionAccountData | None = None


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
    model_name: str | None = None
    prompt_sha: str | None = None
    started_at_ms: int | None = None
    ended_at_ms: int
    provider_status: str | None = None
    analysis_status: str
    error_code: str | None = None
    validation_errors: list[dict[str, str]] = Field(default_factory=list)
    physical_call_count: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_microusd: int | None = None
    cost_unknown_reason: str | None = None
    settled: bool
    physical_calls: list[TradingPhysicalModelCallData] = Field(default_factory=list)


class TradingWatchObservationData(ExactApiSchema):
    parent_case_id: str
    condition: dict[str, Any]
    status: str
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


class TradingCaseEvaluationData(ExactApiSchema):
    source: Literal["shadow_simulation", "paper_venue"]
    evaluation_version: str
    status: str
    reason: str | None = None
    decision_at_ms: int
    scheduled_at_ms: int
    due_at_ms: int
    decision_quote_ref: str | None = None
    planned_quote_ref: str | None = None
    mark_path_ref: str | None = None
    funding_ref: str | None = None
    venue_receipt_ref: str | None = None
    result: dict[str, Any] | None = None
    evaluated_at_ms: int | None = None


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
    analysis_evaluations: list[TradingCaseEvaluationData] = Field(default_factory=list)
    review_mode: Literal["none", "historical_timed", "event_wait", "research_note"] = "none"
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
    decision: TradingAnalysisDecisionData | None = None
    attempts: list[TradingAnalysisAttemptData] = Field(default_factory=list)


class TradingExecutionRowData(ExactApiSchema):
    """One entry identity's whole execution, folded from its plan and its own observations.

    `entry_id` is the identity the Runtime correlates the venue facts under: a Signal's `signal_id`,
    or the `command_id` of a manual entry, which `source` tells apart. A manual entry has no Case, so
    `case_id` is absent on those rows rather than invented.

    `realized_pnl_usd` and `fees_usd` are folded from the fill journal (#680): exit minus entry
    notional, signed by direction, less every commission the venue charged. Both are absent until the
    entry is fully closed and every fill carries a quote-currency commission.
    """

    source: Literal["signal", "manual"]
    entry_id: str
    case_id: str | None = None
    market_key: str
    direction: Literal["long", "short"]
    observed_at_ns: int
    disposition_reason: str | None = None
    # The venue's own words for a refused entry order (#604 T1).
    order_reject_reason: str | None = None
    fill_quantity: str | None = None
    fill_avg_price: str | None = None
    stop_trigger_price: str | None = None
    take_profit_trigger_price: str | None = None
    # The two instants a holding time is the distance between: the entry's first fill and the close of
    # the position it opened. `observed_at_ns` is when the Signal was written, which is neither.
    entry_filled_at_ns: int | None = None
    position_closed_at_ns: int | None = None
    exit_price: str | None = None
    realized_pnl_usd: str | None = None
    fees_usd: str | None = None
    exit_reason: str | None = None
    plan_status: str | None = None
    account_slot: str | None = None
    runtime_mode_at_creation: Literal["paper", "live"] | None = None
    instrument_id: str | None = None
    entry_client_order_id: str | None = None
    risk_budget_usd: str | None = None
    max_leverage_at_creation: int | None = None
    stop_distance_bps: int | None = None
    exit_policy_id: str | None = None
    take_profit_bps: int | None = None
    max_holding_ns: int | None = None
    pnl_known: bool
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
