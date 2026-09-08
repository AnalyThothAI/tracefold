"""Public reads for chain wallet research and its supporting tape state.

Buy candidates remain visible without a delivery. Their selection evidence and observed-price
outcomes come from the stored facts, independently of the roster and ingestion state. PostgreSQL
numeric amounts cross the wire as exact text; the browser does not reconstruct trading facts.
"""

from __future__ import annotations

from typing import Literal

from .common import ExactApiSchema

WalletCardKindLiteral = Literal["buy", "exit", "crowding", "digest"]
WalletBuyStageLiteral = Literal["first_observed", "new_position", "add", "reentry", "unknown"]
WalletFillKindLiteral = Literal["buy", "sell", "transfer_out"]


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


class NewsWalletRosterData(ExactApiSchema):
    """The roster as one version: when it was taken, and who was on it."""

    roster_version: int = 0
    taken_at_ms: int | None = None
    provider: str | None = None
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


class NewsWalletFillTotalData(ExactApiSchema):
    """What the tape stored in the window, per kind.

    `unpriced` counts trades whose cash leg was not the pinned stablecoin, so it is always zero on
    `transfer_out`: a movement with no swap has no cash leg at all, and calling it unpriced would report
    the tape's own classification as a pricing failure.
    """

    kind: WalletFillKindLiteral
    fills: int = 0
    usd: str = "0"
    unpriced: int = 0
    wallets: int = 0
    tokens: int = 0


class NewsWalletCardTotalData(ExactApiSchema):
    """What the rules opened in the window, per kind, and how much of it reached a reader."""

    kind: WalletCardKindLiteral
    cards: int = 0
    sent: int = 0
    last_event_at_ms: int | None = None


class NewsWalletsData(ExactApiSchema):
    """The page's header and roster: one roster version, one tape position, two windowed counts."""

    roster: NewsWalletRosterData
    tape: NewsWalletTapeStateData | None = None
    fills: list[NewsWalletFillTotalData]
    cards: list[NewsWalletCardTotalData]
    window_from_ms: int
    window_to_ms: int


class NewsWalletCardData(ExactApiSchema):
    """A retained observation, its selection evidence and frozen-reference price outcomes.

    A buy can have no delivery and still have outcomes. The paid entry price is distinct from the
    observed market price; unavailable or historically unrecorded references never invent returns.
    """

    item_id: str
    kind: WalletCardKindLiteral
    stage: WalletBuyStageLiteral | None = None
    selection_reason: str | None = None
    buy_count: int | None = None
    unpriced_buys: int | None = None
    observed_at_ms: int | None = None
    history_from_ms: int | None = None
    price_reference: str | None = None
    handle: str = ""
    wallet: str = ""
    token: str = ""
    token_symbol: str | None = None
    tone: str = ""
    ratio_bps: int | None = None
    basis: Literal["chain_balance", "site_reported"] | None = None
    closed: bool = False
    peer_wallets: int = 0
    premium_bps: int | None = None
    usd: str | None = None
    position_usd: str | None = None
    entry_price: str | None = None
    mark_price: str | None = None
    event_at_ms: int
    window_from_ms: int
    window_to_ms: int
    delivery_key: str | None = None
    delivery_state: Literal["pending", "sending", "sent", "failed", "unknown", "unavailable"] | None = None
    settled_at_ms: int | None = None
    outcome_15m_source: str | None = None
    return_15m_bps: int | None = None
    outcome_1h_source: str | None = None
    return_1h_bps: int | None = None
    outcome_4h_source: str | None = None
    return_4h_bps: int | None = None
    # Present on a digest and on nothing else: the sentences it was sent with, and whether the model
    # selected their material; the program renders the sentences.
    digest_lines: list[str] | None = None
    digest_model_used: bool | None = None


class NewsWalletFillData(ExactApiSchema):
    """A retained wallet/token action, whether or not it generated an observation."""

    chain_id: int
    tx_hash: str
    log_index: int
    block_number: int
    wallet: str
    token: str
    token_symbol: str | None
    token_decimals: int | None
    kind: WalletFillKindLiteral
    amount_raw: str
    usd: str | None
    event_at_ms: int


class NewsWalletCardsData(ExactApiSchema):
    """One bounded page of cards, newest first, inside the window the caller asked for."""

    cards: list[NewsWalletCardData]
    fills: list[NewsWalletFillData]
    window: str
    window_from_ms: int
    window_to_ms: int
    limit: int


__all__ = [
    "NewsWalletCardData",
    "NewsWalletCardTotalData",
    "NewsWalletCardsData",
    "NewsWalletFillData",
    "NewsWalletFillTotalData",
    "NewsWalletRosterData",
    "NewsWalletRosterMemberData",
    "NewsWalletTapeStateData",
    "NewsWalletsData",
]
