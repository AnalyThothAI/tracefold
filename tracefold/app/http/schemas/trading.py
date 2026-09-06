"""Trading read contracts plus the bounded operator-command request and receipt."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from tracefold.trading import CommandStage, ExecutionStage

from .common import ExactApiSchema


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
    """What the account holds, as the Runtime's own private reconciliation last saw it.

    Its two observation clocks, the day-start equity baseline and the `truncated` flag were published
    beside these and rendered nowhere: the desk ages the whole projection against `facts_expire_at_ms`,
    reads the drawdown the Runtime already measured against that baseline, and a truncated snapshot is
    already not `complete` (#537 PR-5).
    """

    equity_usd: str | None = None
    daily_drawdown_usd: str | None = None
    daily_drawdown_bps: int | None = None
    aggregate_risk_usd: str | None = None
    positions: list[TradingExecutionPositionData] = Field(default_factory=list, max_length=100)
    orders: list[TradingExecutionOrderData] = Field(default_factory=list, max_length=200)
    open_orders_count: int = Field(ge=0)
    inflight_orders_count: int = Field(ge=0)
    unknown_orders_count: int = Field(ge=0)
    complete: bool
    audit_healthy: bool = True
    audit_failure_reason: str | None = None


class TradingExecutionReadinessData(ExactApiSchema):
    """One field per operator question, and the CLI `tracefold trading status` block is this same dict.

    The two raw observation clocks (`heartbeat_at_ns`, `reconciliation_observed_at_ns`) went with the
    ages derived from them: the desk compares `facts_expire_at_ms` against its own clock and prints
    `reconciliation_age_ms`, both already measured here. `positions_count` / `open_orders_count` said
    what `current_account` carries row by row, and raw `account_flat` said what the venue had not yet
    proven -- `account_flat_proven` is the answer an operator acts on (#537 PR-5).
    """

    mode: Literal["disabled", "paper", "live"]
    account_slot: str
    alive: bool
    execution_safe: bool
    entries_armed: bool
    entry_block_reason: str | None = None
    reconciliation_age_ms: int | None = None
    startup_reconciled: bool = False
    entries_paused: bool = True
    emergency_halted: bool = False
    unexpected_exposure: bool = False
    account_flat_proven: bool = False
    protection_status: Literal["not_applicable", "protected", "pending", "unprotected", "unknown"] = "unknown"
    routes_count: int = Field(default=0, ge=0)
    # The instant this projection stops being current: the earlier of the heartbeat and private
    # reconciliation freshness budgets. `None` when there is no Runtime row to age.
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


class TradingCaseData(ExactApiSchema):
    """One frozen Case, as the drawer behind `?case=<id>` renders it.

    The four measured OI numbers here were a second copy of what `policy_checks` already carries with
    the threshold each was measured against, `policy_version` a second copy of `policy_id`, and
    `policy_decision` a required Literal over a nullable column -- exactly the shape that turned a
    stored `NULL` into a 500 on a read route (#532, #537 PR-5). `state` and `policy_reason` are the
    terminal answer; `base_symbol` is the identity the drawer titles itself with.
    """

    case_id: str
    event_id: str | None = None
    base_symbol: str
    market_key: str | None = None
    manifest_version: str | None = None
    # The manifest's own policy identity. Nullable because the manifest is the only writer of it and a
    # Case whose manifest names no policy must render as that, not 500 the route (#532, #537 PR-3).
    policy_id: str | None = None
    policy_config_digest: str | None = None
    policy_config: dict[str, str] = Field(default_factory=dict)
    policy_checks: list[TradingPolicyCheckData] = Field(default_factory=list)
    state: str
    policy_reason: str | None = None
    mark_price: str | None = None
    pre_move_bps: int | None = None
    observed_at_ms: int
    created_at_ms: int
    decided_at_ms: int | None = None


class TradingCasesData(ExactApiSchema):
    """One bounded page of Cases plus the two durable 24 h distributions.

    There is no `next_cursor` and no cursor parameter: the desk opens one Case at a time from
    `?case=<id>` and renders one 24 h count card, and no reader ever asked for a second page (#537 PR-5).
    """

    cases: list[TradingCaseData] = Field(default_factory=list)
    state_counts_24h: dict[str, int] = Field(default_factory=dict)
    reason_counts_24h: dict[str, int] = Field(default_factory=dict)
    complete: bool
    window_hours: int


class TradingExecutionRowData(ExactApiSchema):
    """One entry identity's whole execution, folded from its own observations (#528 PR-1, PR-3).

    `entry_id` is the identity the Runtime correlates the venue facts under: a Signal's `signal_id`,
    or the `command_id` of a manual entry, which `source` tells apart. A manual entry has no Case, so
    `case_id` is absent on those rows rather than invented; the desk links the ones that have one to
    the Case drawer.

    `order_status`, `position_status` and the `accepted` / `rejected` split are the inputs `stage` is
    derived from, and `stage` is what the table renders: publishing all four let a reader compare a
    venue word against the server's own answer about the same row (#537 PR-5). `last_observed_at_ns`
    was a second clock beside `observed_at_ns` that no column printed.
    """

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
    exit_price: str | None = None
    realized_pnl_usd: str | None = None
    exit_reason: Literal["stop_filled", "flatten", "unclaimed_flatten"] | None = None
    stage: ExecutionStage


class TradingExecutionCommandRowData(ExactApiSchema):
    """One operator Command's progress, read from its `control_disposition` alone.

    Action, stage and clock: the ACT block's ledger rows. `operator_identity` is the constant
    `operator-console` for every browser write and `reason` is the text the same operator just typed
    into the field above the ledger (#537 PR-5).
    """

    command_id: str
    action: Literal["pause_entries", "resume_entries", "emergency_halt", "flatten", "manual_entry"]
    requested_at_ns: int
    stage: CommandStage


class TradingExecutionsData(ExactApiSchema):
    executions: list[TradingExecutionRowData] = Field(default_factory=list)
    commands: list[TradingExecutionCommandRowData] = Field(default_factory=list)
    complete: bool


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
