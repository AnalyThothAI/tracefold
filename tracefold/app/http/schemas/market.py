"""Public shapes for the market read surface.

`parse_status`/`parse_error` and `notification_status`/`notification_reason` are two independent
pairs. A raw card that was delivered and a parsed card that was not are both ordinary, and a single
combined outcome field would have to misreport one of them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import ExactApiSchema

MarketKindLiteral = Literal["oi", "liquidation", "smart_money", "unknown_market"]


class NewsMarketObservationData(ExactApiSchema):
    """One provider record and whatever the parser could prove about it."""

    item_id: str
    market_kind: MarketKindLiteral
    source_strategy_id: str | None = None
    parse_status: Literal["parsed", "raw"]
    parse_error: str | None = None
    ingest_mode: str
    historical: bool = False
    group_key: str
    title: str
    event_at_ms: int
    received_at_ms: int
    available_at_ms: int | None = None
    provider: str | None = None
    source_venue: str | None = None
    raw_instrument: str | None = None
    symbol: str | None = None
    measurement_definition: str | None = None
    direction: str | None = None
    oi_change_bps: int | None = None
    oi_value_usd: int | None = None
    whale_long_profit_bps: int | None = None
    whale_oi_ratio_bps: int | None = None
    liquidated_position_side: str | None = None
    forced_order_side: str | None = None
    # Decimal figures cross the wire as their exact stored text. A JSON number would round a provider
    # notional the ledger holds precisely, and the console renders these, never computes with them.
    notional_usd: str | None = None
    price: str | None = None
    trader_label: str | None = None
    account_address: str | None = None
    action: str | None = None
    position_side: str | None = None
    pnl_usd: str | None = None
    # The second independent pair. With no attempt this says which rule is holding the observation --
    # `historical`, `merging`, `unprocessed` -- or that none is, because the alert round it belonged
    # to ended before a card spoke for it (`uncovered`). With an attempt it says what the send did.
    notification_status: str
    notification_reason: str
    # The notification group the loop assigned this observation to, and the card that spoke for it.
    # Deliberately not `group_key`: the display run above breaks when a smart-money account changes
    # action, and the notification group must not, because that change is the thing worth a card.
    notify_group_key: str | None = None
    delivery_key: str | None = None


class NewsMarketDeliveryData(ExactApiSchema):
    """One card: what it covered, what was attempted, and what came back.

    The receipt itself is not published -- only which provider answered. A receipt carries channel
    identifiers, and the console's question is "did this reach a reader", not "which message id".
    """

    delivery_key: str
    trigger_reason: Literal["first", "followup", "action_change", "raw"]
    trigger_item_id: str
    state: Literal["pending", "sending", "sent", "failed", "unknown", "unavailable"]
    attempts: int = 0
    covered_count: int = 0
    covered_from_ms: int | None = None
    covered_to_ms: int | None = None
    # The frozen snapshot: exactly what was sent, not a re-render of what would be sent now.
    card: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    receipt_provider: str | None = None
    first_attempt_at_ms: int | None = None
    last_attempt_at_ms: int | None = None
    next_attempt_at_ms: int | None = None
    settled_at_ms: int | None = None


class NewsMarketGroupData(ExactApiSchema):
    """One run of consecutive observations of the same group, collapsed to its newest member."""

    group_key: str
    market_kind: MarketKindLiteral
    observation_count: int
    first_event_at_ms: int
    last_event_at_ms: int
    latest: NewsMarketObservationData
    # Where the next page starts: the run's oldest member, not its newest.
    oldest_received_at_ms: int
    oldest_item_id: str
    notification_status: str
    notification_reason: str


class NewsMarketSourceData(ExactApiSchema):
    """What one kind did in the window: what arrived, and what a reader was told about it."""

    market_kind: MarketKindLiteral
    received: int = 0
    parsed: int = 0
    raw: int = 0
    groups: int = 0
    last_received_at_ms: int | None = None
    # Observations a card spoke for without being the record that triggered it: the noise reduction,
    # as a number rather than a claim.
    merged: int = 0
    sent: int = 0
    failed: int = 0
    unknown: int = 0
    last_sent_at_ms: int | None = None
    last_failed_at_ms: int | None = None
    last_unknown_at_ms: int | None = None


class NewsMarketFiltersData(ExactApiSchema):
    kind: str | None = None
    from_ms: int
    to_ms: int
    limit: int


class NewsMarketData(ExactApiSchema):
    groups: list[NewsMarketGroupData]
    next_cursor: str | None = None
    sources: list[NewsMarketSourceData]
    filters: NewsMarketFiltersData
    # True when one page's scan reached its bound, so a run's `observation_count` is a floor rather
    # than a total. `sources` is unbounded and stays exact either way.
    scan_truncated: bool = False


class NewsMarketItemData(ExactApiSchema):
    """One observation in full: the stored provider payload and its group's timeline."""

    observation: NewsMarketObservationData
    provider_params: dict[str, Any]
    description: str = ""
    raw_first_line: str = ""
    notification_status: str
    notification_reason: str
    # The card that spoke for this observation, if one did, and the observations it covered.
    notification_delivery: NewsMarketDeliveryData | None = None
    notification_covered_item_ids: list[str] = Field(default_factory=list)
    timeline: list[NewsMarketObservationData]


__all__ = [
    "NewsMarketData",
    "NewsMarketDeliveryData",
    "NewsMarketFiltersData",
    "NewsMarketGroupData",
    "NewsMarketItemData",
    "NewsMarketObservationData",
    "NewsMarketSourceData",
]
