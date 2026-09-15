"""Token net-buy episodes and auxiliary collection status (#641)."""

from __future__ import annotations

from typing import Literal

from tracefold.news import NetBuySnapshot

from .common import ExactApiSchema


class NewsWalletRosterMemberData(ExactApiSchema):
    """One followed wallet in the current roster version, and the two ranks that put it there.

    A member can hold both ranks and can hold either alone; `null` means "this list did not select
    this wallet", which is not the same as rank 0. Win rate is recorded and is deliberately not a
    selection criterion (#572 §3.2).
    """

    wallet: str
    handle: str
    followers: int = 0
    realized_pnl: float = 0.0
    closed_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float | None = None
    open_cost: float = 0.0
    rank_quality: int | None = None
    rank_whale: int | None = None
    provider: str
    monitoring_from_ms: int | None = None


class NewsWalletRosterData(ExactApiSchema):
    """The roster as one version: when it was taken, who was on it, and how the last refresh went.

    `quality_count` is the pool the 5m/30m thresholds are counted against; `whale_count` is observation
    background and never stands in for it. `supported_quality_count` is the subset whose monitoring
    already covers a whole fast window at the collection cutoff -- a wallet the list gained minutes ago
    cannot complete a quorum yet, and a page that counted it would promise a trigger that cannot fire.
    """

    version: int = 0
    taken_at_ms: int | None = None
    provider: str | None = None
    quality_count: int = 0
    whale_count: int = 0
    supported_quality_count: int = 0
    last_attempt_at_ms: int | None = None
    last_success_at_ms: int | None = None
    last_error: str | None = None
    members: list[NewsWalletRosterMemberData]


class NewsWalletTapeStateData(ExactApiSchema):
    """Where the tape has read to, and what its last turn did there.

    `noise_through_block` is the second position and never lags: it is how far the two discard counters
    have been taken, so a movement is counted once however many times the overlap re-offers it.
    """

    high_water_block: int = 0
    high_water_tx_index: int = 0
    roster_version: int = 0
    last_outcome: str = ""
    last_error: str | None = None
    last_success_at_ms: int | None = None
    updated_at_ms: int | None = None
    ignored_inbound_total: int = 0
    unknown_total: int = 0
    noise_through_block: int = 0
    noise_through_tx_index: int = 0
    detection_cutover_at_ms: int
    coverage_from_ms: int | None
    scanned_at_ms: int | None
    scanned_block: int | None
    scanned_log: int | None
    gap_at_ms: int | None


class NewsWalletThresholdsData(ExactApiSchema):
    """The two window quorums, and whether the current pool can reach either of them."""

    fast_n: int
    slow_n: int
    sufficient: bool


class NewsWalletFunnelData(ExactApiSchema):
    """Episodes to intents to sends over one window, and the reason most of the rest stopped at.

    Counts describe the stated window and are never accumulated totals. `unsent_reason` is the server's
    own reason string for the episodes that did not reach a channel, not a translated summary.
    """

    window_from_ms: int
    window_to_ms: int
    events: int
    intents: int
    sent: int
    unsent_reason: str | None
    unsent_reason_count: int


class NewsWalletsData(ExactApiSchema):
    roster: NewsWalletRosterData
    tape: NewsWalletTapeStateData | None
    thresholds: NewsWalletThresholdsData
    funnel: NewsWalletFunnelData
    collection_lagging: bool
    notifications_enabled: bool


class NewsWalletEventData(ExactApiSchema):
    episode_id: str
    chain_id: int
    token: str
    token_symbol: str | None
    trigger_tx_hash: str
    triggered_at_ms: int
    received_at_ms: int
    detected_at_ms: int
    last_effective_buy_at_ms: int
    ended_at_ms: int | None
    initial_snapshot: NetBuySnapshot
    latest_snapshot: NetBuySnapshot
    change_reason: str
    updated_at_ms: int
    notification_state: str
    notification_reason: str | None
    intent_at_ms: int | None
    first_attempt_at_ms: int | None
    settled_at_ms: int | None
    attempts: int
    reference_price: str | None
    reference_at_ms: int | None
    reference_source: str | None


class NewsWalletEventTotalsData(ExactApiSchema):
    total: int
    active: int
    sent: int


class NewsWalletEventsData(ExactApiSchema):
    events: list[NewsWalletEventData]
    totals: NewsWalletEventTotalsData
    next_cursor: str | None
    history_range: Literal["24h", "72h", "7d"]
    history_from_ms: int
    history_to_ms: int
    limit: int


class NewsWalletFillData(ExactApiSchema):
    chain_id: int
    tx_hash: str
    log_index: int
    block_number: int
    block_hash: str
    wallet: str
    token: str
    token_symbol: str | None
    token_decimals: int | None
    kind: Literal["buy", "sell", "transfer_out"]
    amount_raw: str
    usd: str | None
    usd_source: str | None
    event_at_ms: int
    received_at_ms: int
    classified_at_ms: int
    roster_version: int


class NewsWalletOutcomeData(ExactApiSchema):
    horizon: Literal["15m", "1h", "4h"]
    target_at_ms: int
    at_ms: int
    price: str | None
    source: str
    reference_price: str | None
    reference_at_ms: int | None
    status: Literal["comparable", "missing_reference", "unavailable", "late"]
    change_percent: str | None


class NewsWalletEventDetailData(ExactApiSchema):
    event: NewsWalletEventData
    fills: list[NewsWalletFillData]
    next_fills_cursor: str | None
    outcomes: list[NewsWalletOutcomeData]
