"""Read-only trading monitoring contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from tracefold.trading.stages import ExecutionStage

from .common import ExactApiSchema


class TradingDecisionRuntimeData(ExactApiSchema):
    """Analysis process liveness and the last durable Case for this deployment."""

    last_case_at_ms: int | None = None
    state: Literal["disabled", "unavailable", "model_unconfigured", "faulted", "running"]
    active_policy: str
    model_name: str | None = None
    program_sha: str | None = None
    fault_code: str | None = None
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


class TradingAssessmentData(ExactApiSchema):
    case_id: str
    program_sha: str
    route: str
    status: str
    forecast: dict[str, Any] | None = None
    drivers: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    started_at_ms: int
    ended_at_ms: int


class TradingPolicyActionData(ExactApiSchema):
    case_id: str
    program_sha: str
    policy_id: str
    policy_version: str
    calibrator_version: str
    action: Literal["long", "short", "abstain"]
    reason: str
    expected_r: str | None = None
    publish_status: str
    signal_id: str | None = None
    decided_at_ms: int


class TradingPaperLegData(ExactApiSchema):
    case_id: str
    side: Literal["long", "short"]
    geometry_version: str
    status: Literal["complete", "missing"]
    outcome: Literal["tp", "sl", "timeout"] | None = None
    reason: str | None = None
    anchor_at_ms: int | None = None
    exit_at_ms: int | None = None
    anchor_price: str | None = None
    exit_price: str | None = None
    gross_bps: str | None = None
    cost_bps: str | None = None
    net_r: str | None = None
    labeled_at_ms: int


class TradingCaseData(ExactApiSchema):
    case_id: str
    trigger_kind: Literal["oi", "catalyst"]
    asset_id: str
    native_symbol: str
    created_at_ms: int
    state: Literal["pending", "running", "complete", "failed"]
    failure_code: str | None = None
    decided_at_ms: int | None = None
    geometry_version: str | None = None
    view_sha256: str | None = None
    raw_snapshot_ref: str | None = None
    view: dict[str, Any] | None = None
    assessments: list[TradingAssessmentData] = Field(default_factory=list)
    policy_actions: list[TradingPolicyActionData] = Field(default_factory=list)
    paper_legs: list[TradingPaperLegData] = Field(default_factory=list)


class TradingCasesData(ExactApiSchema):
    cases: list[TradingCaseData] = Field(default_factory=list)
    total: int
    complete: bool


class TradingScoreboardPolicyData(ExactApiSchema):
    policy_id: str
    policy_version: str
    cases: int
    actions: int
    scored: int
    coverage: str
    average_r: str | None = None
    win_rate: str | None = None
    ci_low: str | None = None
    ci_high: str | None = None
    status: Literal["ok", "insufficient_data"]


class TradingReliabilityBinData(ExactApiSchema):
    bin: int
    count: int
    observed_tp_rate: str


class TradingScoreboardForecastData(ExactApiSchema):
    legs: int
    multiclass_brier: str | None = None
    log_loss: str | None = None
    brier_skill_score: str | None = None
    reliability: list[TradingReliabilityBinData] = Field(default_factory=list)
    status: Literal["ok", "insufficient_data"]


class TradingScoreboardProgramData(ExactApiSchema):
    program_sha: str
    route: str
    assessments: int
    failures: dict[str, int]
    policies: list[TradingScoreboardPolicyData]
    forecast: TradingScoreboardForecastData
    execution_deviation: dict[str, Any]


class TradingScoreboardData(ExactApiSchema):
    window: dict[str, int]
    funnel: dict[str, int]
    programs: list[TradingScoreboardProgramData]


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
