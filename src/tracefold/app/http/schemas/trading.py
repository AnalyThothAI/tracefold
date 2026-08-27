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
    """The 24 h funnel by the ledger's own grouping keys, plus the realised measure.

    `closed_realized_bps` sums only orders whose exit was *measured*: an operator-resolved close moved a
    position but nobody computed a return for it, and counting it turned one +150 bps winner beside three
    resolutions into a reported mean of 37.5.

    Rolling groupings retain their 24 h window. The `*_today` fields, `policy_allowed_today`, and
    `funnel_today` bind to the UTC `funnel_day_key`; an upper bound keeps a stale Workers day from silently
    becoming a multi-day count. `active_orders` is intentionally unbounded because unresolved exposure does
    not stop mattering after 24 h.
    """

    cases_by_state: dict[str, int] = Field(default_factory=dict)
    cases_by_trigger: dict[str, int] = Field(default_factory=dict)
    cases_by_strategy: dict[str, int] = Field(default_factory=dict)
    shadow_by_strategy: dict[str, int] = Field(default_factory=dict)
    shadow_by_rule: dict[str, int] = Field(default_factory=dict)
    shadow_cohorts: dict[str, TradingShadowCohortData] = Field(default_factory=dict)
    event_study_cohorts: list[TradingEventStudyCohortData] = Field(default_factory=list)
    liquidation_promotion_ready: bool = False
    liquidation_promotion_reason: str = ""
    orders_by_state: dict[str, int] = Field(default_factory=dict)
    closed_orders: int = 0
    closed_realized_bps: int = 0
    cases_today_by_state: dict[str, int] = Field(default_factory=dict)
    policy_allowed_today: int = 0
    # The rolling twin of the field above, so a funnel can be drawn on one clock (#273).
    policy_allowed_24h: int = 0
    closed_orders_today: int = 0
    active_orders: int = 0
    funnel_today: dict[str, int] = Field(default_factory=dict)
    funnel_day_key: str = ""
    # #264: the durable admission ledger, which is the only part of this document that survives the
    # UTC day roll and the only part a lane with zero cases and zero orders can still answer from.
    # `candidate_reasons_*` are keyed `stage:reason` from a closed vocabulary, never by symbol.
    candidate_counts_24h: dict[str, int] = Field(default_factory=dict)
    candidate_counts_7d: dict[str, int] = Field(default_factory=dict)
    candidate_reasons_24h: dict[str, int] = Field(default_factory=dict)
    candidate_reasons_7d: dict[str, int] = Field(default_factory=dict)
    latest_source_at_ms: int | None = None
    latest_gate_eligible_at_ms: int | None = None
    latest_case_created_at_ms: int | None = None
    latest_order_prepared_at_ms: int | None = None
    latest_position_opened_at_ms: int | None = None
    latest_position_closed_at_ms: int | None = None


class TradingGateEvidenceData(ExactApiSchema):
    """What one admission decision was taken on. Every key is code-owned and named here on purpose.

    Passing the stored document straight through would put the next evidence key in a browser without
    anyone deciding that it should, which is the same rule the order and case projections follow. The
    four measurements are always present past the source stage; the rest are the threshold or the
    provider detail that the specific rule failed on.
    """

    venue: str = ""
    oi_change_bps: int | None = None
    oi_value_usd: int | None = None
    whale_oi_ratio_bps: int | None = None
    whale_long_profit_bps: int | None = None
    rank_in_window: int | None = None
    source_decision: str = ""
    source_rule: str = ""
    # The number the rule compared against, so a threshold argument is settled from this one row.
    floor: int | None = None
    limit: int | None = None
    age_ms: int | None = None
    max_age_ms: int | None = None
    blacklist_reason: str = ""
    enabled: list[str] = Field(default_factory=list)
    rule: str = ""


class TradingGateConfigData(ExactApiSchema):
    """The Candidate Gate as the scanner holds it (#269).

    `floors` is the operator's settings document. This is the rule set that actually admits an OI fact,
    and its `config_digest` is the second half of the key every row in the admission ledger is filed
    under — so a console can say which configuration decided the frame it is showing, rather than
    comparing it against whichever number happens to be in settings now.
    """

    version: str
    config_digest: str
    max_age_ms: int
    max_rank_in_window: int
    min_oi_value_usd: int
    symbol_cooldown_ms: int
    venue_priority: list[str] = Field(default_factory=list)


class TradingStrategyConfigData(ExactApiSchema):
    """One versioned strategy and the numbers it executes.

    `config` is rendered as text per key on purpose: each strategy owns its own keys, mixing booleans,
    basis points and millisecond windows, and this surface reports them rather than interpreting them.
    """

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


class TradingOrderData(ExactApiSchema):
    """One economic intent and the case that authored it. Money is an exact decimal string, never a float."""

    order_id: str
    case_id: str
    event_id: str | None = None
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
    trigger_kind: str
    strategy_id: str
    strategy_version: str
    case_state: str
    regime: str | None = None
    policy_decision: str | None = None
    policy_reason: str | None = None
    # The same two frozen case facts `TradingCaseData` carries, on the half that got furthest (#282). An
    # order row is still a case row, and a reader asking "what was this decided on" should not get a worse
    # answer for a case that filled than for one that was refused.
    pre_move_bps: int | None = None
    strategy_config: dict[str, str] = Field(default_factory=dict)
    case_observed_at_ms: int | None = None


class TradingCaseData(ExactApiSchema):
    """A case that stopped before authoring an intent, and the rule it stopped on."""

    case_id: str
    event_id: str | None = None
    underlying_key: str
    base_symbol: str
    trigger_kind: str
    strategy_id: str
    strategy_version: str
    mode: str
    state: str
    regime: str | None = None
    policy_decision: str | None = None
    policy_reason: str | None = None
    # The frozen pre-move the two price rules are about, so a refusal can state its own number.
    pre_move_bps: int | None = None
    # And the thresholds this case was decided against, stringified like `/status` stringifies the
    # running ones. Empty for a case frozen before #273, which is its own honest answer.
    strategy_config: dict[str, str] = Field(default_factory=dict)
    observed_at_ms: int
    created_at_ms: int
    decided_at_ms: int | None = None


class TradingOrdersData(ExactApiSchema):
    orders: list[TradingOrderData] = Field(default_factory=list)
    cases_without_orders: list[TradingCaseData] = Field(default_factory=list)
    complete: bool
    window_hours: int
    measured_at_ms: int


class TradingGateDecisionData(ExactApiSchema):
    """One durable admission answer, for a page showing a whole window of frames at once (#269).

    `event_id` is null for a source whose key is not the deterministic OI contract. The row is still
    listed: the distributions in `/trading/status` count it, and omitting it here would make a page's
    own total disagree with the number printed above it.
    """

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
    # #264: why there is no case, when there is none. `null` means the lane has not evaluated this
    # source under any gate version — after a `gate_version` deploy that is the honest state, and it is
    # a different fact from a refusal the console would otherwise have had to invent.
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
    "TradingGateConfigData",
    "TradingGateData",
    "TradingGateDecisionData",
    "TradingGateEvidenceData",
    "TradingOrderData",
    "TradingOrdersData",
    "TradingReadinessData",
    "TradingShadowCohortData",
    "TradingStatusData",
    "TradingStrategyConfigData",
]
