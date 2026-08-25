"""The capital lane's read-only surface (#207 PR-W4, #104, #185).

Two endpoints, both reads, both the same facts the CLI's `trading status` and `trading cases` report — one
answer per question, not a second implementation of it.

What is deliberately absent is the point of the file. `trading_orders.payload` is the frozen provider request
body and `trading_cases.manifest` is the frozen decision input: neither reaches the browser. Neither does
`account_ref` or `remote_order_id`, which name things outside this system. And there is no field, anywhere,
that a page could turn into a switch: this surface has no writes, and `live_ready` is reported, never
offered.

Every state string is the ledger's own. `ACKNOWLEDGED` is the venue answering, not a fill; `OPEN` is the only
state that has proven both a real position and a native stop covering it. Collapsing them into "已成交" would
be the console asserting something the ledger does not (#185 P0-3).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import ExactApiSchema


class TradingBudgetData(ExactApiSchema):
    """The mandate, as configured. Fixed size, fixed stop, fixed maximum hold — there is no sizing model."""

    notional_usd: str
    stop_loss_bps: int
    max_hold_ms: int
    nominal_daily_stop_loss_usd: str
    max_orders_per_day: int
    orders_today: int


class TradingReadinessData(ExactApiSchema):
    """Whether this deployment could trade for real, and how far that is from proven.

    `live_ready` is never `true` from a read: a serve process cannot observe another process's startup and
    canary result, so it reports `not_proven` rather than guessing. The console renders the word.
    """

    enabled: bool
    mode: Literal["paper", "live_reviewed", "live_bounded"]
    control: str
    execution_backend: str
    execution_configured: bool
    live_mode_supported: bool
    live_ready: bool
    live_readiness: str
    venues: list[str] = Field(default_factory=list)


class TradingFloorsData(ExactApiSchema):
    """The capital lane's own thresholds — a different set from the News gates, never merged with them."""

    min_whale_long_profit_bps: int
    min_oi_value_usd: str
    min_price_move_bps: int
    max_price_move_bps: int
    lookback_ms: int


class TradingCountsData(ExactApiSchema):
    """The 24 h funnel by the ledger's own grouping keys, plus the realised measure.

    `closed_realized_bps` sums only orders whose exit was *measured*: an operator-resolved close moved a
    position but nobody computed a return for it, and counting it turned one +150 bps winner beside three
    resolutions into a reported mean of 37.5.
    """

    cases_by_state: dict[str, int] = Field(default_factory=dict)
    cases_by_kind: dict[str, int] = Field(default_factory=dict)
    orders_by_state: dict[str, int] = Field(default_factory=dict)
    closed_orders: int = 0
    closed_realized_bps: int = 0
    funnel_24h: dict[str, int] = Field(default_factory=dict)


class TradingStatusData(ExactApiSchema):
    budget: TradingBudgetData
    readiness: TradingReadinessData
    floors: TradingFloorsData
    counts: TradingCountsData
    window_hours: int
    measured_at_ms: int


class TradingOrderData(ExactApiSchema):
    """One economic intent and the case that authored it. Money is an exact decimal string, never a float."""

    order_id: str
    case_id: str
    underlying_key: str
    base_symbol: str
    exchange_id: str
    provider_symbol: str
    mode: str
    side: Literal["buy", "sell"]
    notional_usd: str
    quantity: str
    entry_reference: str
    stop_price: str
    take_profit_price: str | None = None
    state: str
    state_reason: str | None = None
    provider_attempt_count: int = 0
    exit_attempt_total: int = 0
    filled_quantity: str | None = None
    average_price: str | None = None
    exit_price: str | None = None
    exit_reason: str | None = None
    realized_bps: int | None = None
    position_opened_at_ms: int | None = None
    position_closed_at_ms: int | None = None
    must_close_at_ms: int | None = None
    created_at_ms: int
    updated_at_ms: int
    case_kind: str
    case_state: str
    regime: str | None = None
    policy_decision: str | None = None
    policy_reason: str | None = None
    case_observed_at_ms: int | None = None


class TradingCaseData(ExactApiSchema):
    """A case that stopped before authoring an intent, and the rule it stopped on."""

    case_id: str
    underlying_key: str
    base_symbol: str
    case_kind: str
    mode: str
    state: str
    regime: str | None = None
    policy_decision: str | None = None
    policy_reason: str | None = None
    observed_at_ms: int
    created_at_ms: int
    decided_at_ms: int | None = None


class TradingOrdersData(ExactApiSchema):
    orders: list[TradingOrderData] = Field(default_factory=list)
    cases_without_orders: list[TradingCaseData] = Field(default_factory=list)
    window_hours: int
    measured_at_ms: int


class TradingEventCaseData(ExactApiSchema):
    """Whether one News Event became a case, for the Event detail's 成案 badge (#207 PR-W4).

    `joinable` is the field that keeps this honest. Only the deterministic OI lane's source key is
    reconstructible from an `event_id` (`oi:{event_id}:{metric_version}`); the model lane's is a content hash
    of an artifact and a fingerprint (#154), which no Event id rebuilds. For a model-lane Event the answer is
    `joinable: false` — "this cannot be asked", which is a different fact from `case: null`, "it was asked
    and the answer is no". Rendering them the same would tell a reader the lane declined an Event it never
    saw.
    """

    event_id: str
    joinable: bool
    case: TradingCaseData | None = None
    order_id: str | None = None
    order_state: str | None = None
    order_state_reason: str | None = None
    side: Literal["buy", "sell"] | None = None
    notional_usd: str | None = None
    entry_reference: str | None = None
    stop_price: str | None = None
    exit_price: str | None = None
    exit_reason: str | None = None
    realized_bps: int | None = None
    position_opened_at_ms: int | None = None
    position_closed_at_ms: int | None = None


__all__ = [
    "TradingBudgetData",
    "TradingCaseData",
    "TradingCountsData",
    "TradingEventCaseData",
    "TradingFloorsData",
    "TradingOrderData",
    "TradingOrdersData",
    "TradingReadinessData",
    "TradingStatusData",
]
