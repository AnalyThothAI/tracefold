"""Immutable market admission facts shared by the parser and its single writer."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .source_contracts import MarketKind


class MarketObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str
    kind: MarketKind
    source_id: str
    source_item_key: str
    source_strategy_id: str
    provider_metadata: dict[str, Any]
    provider_params: dict[str, Any]
    title: str
    raw_first_line: str
    description: str
    ingest_mode: Literal["live", "recovery"]
    parse_status: Literal["parsed", "raw"]
    parse_error: str | None
    event_at_ms: int
    received_at_ms: int
    available_at_ms: int | None = None
    provider: str | None = None
    source_venue: str | None = None
    raw_instrument: str | None = None
    symbol: str | None = None
    parser_version: str | None = None
    source_contract_version: str | None = None
    historical: bool = False
    oi_event_id: str | None = None
    measurement_definition: str | None = None
    measurement_window_ms: int | None = Field(default=None, gt=0)
    direction: Literal["rise", "fall"] | None = None
    oi_change_bps: int | None = None
    oi_value_usd: int | None = None
    whale_long_profit_bps: int | None = None
    whale_oi_ratio_bps: int | None = None
    liquidated_position_side: Literal["long", "short"] | None = None
    forced_order_side: Literal["buy", "sell"] | None = None
    notional_usd: Decimal | None = Field(default=None, gt=0)
    price: Decimal | None = Field(default=None, gt=0)
    trader_label: str | None = None
    account_address: str | None = None
    action: Literal["open", "close"] | None = None
    position_side: Literal["long", "short"] | None = None
    pnl_usd: Decimal | None = None
