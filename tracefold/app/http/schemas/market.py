"""Public shapes for the market read surface.

`parse_status`/`parse_error` and `notification_status`/`notification_reason` are two independent
pairs. A raw card that was delivered and a parsed card that was not are both ordinary, and a single
combined outcome field would have to misreport one of them.
"""

from __future__ import annotations

from typing import Any, Literal

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


class NewsMarketGroupData(ExactApiSchema):
    """One run of consecutive observations of the same group, collapsed to its newest member."""

    group_key: str
    market_kind: MarketKindLiteral
    observation_count: int
    first_event_at_ms: int
    last_event_at_ms: int
    latest: NewsMarketObservationData
    notification_status: str
    notification_reason: str


class NewsMarketSourceData(ExactApiSchema):
    market_kind: MarketKindLiteral
    received: int = 0
    parsed: int = 0
    raw: int = 0
    groups: int = 0
    last_received_at_ms: int | None = None


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
    notifications_connected: bool = False


class NewsMarketItemData(ExactApiSchema):
    """One observation in full: the stored provider payload and its group's timeline."""

    observation: NewsMarketObservationData
    provider_params: dict[str, Any]
    description: str = ""
    raw_first_line: str = ""
    notification_status: str
    notification_reason: str
    timeline: list[NewsMarketObservationData]
    notifications_connected: bool = False


__all__ = [
    "NewsMarketData",
    "NewsMarketFiltersData",
    "NewsMarketGroupData",
    "NewsMarketItemData",
    "NewsMarketObservationData",
    "NewsMarketSourceData",
]
