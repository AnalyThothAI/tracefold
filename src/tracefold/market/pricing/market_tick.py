from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

MarketTickTargetType = Literal["chain_token", "cex_symbol"]
MarketTickSourceTier = Literal["tier1_ws", "tier2_poll", "tier3_inline"]
MarketTickSourceProvider = Literal["binance_cex_rest", "gmgn_dex_quote"]
DEX_QUOTE_SOURCE_PROVIDERS: frozenset[MarketTickSourceProvider] = frozenset({"gmgn_dex_quote"})
EventCaptureMethod = Literal["tier1_ws", "tier2_poll", "tier3_inline", "unavailable"]


@dataclass(frozen=True, slots=True)
class MarketTick:
    tick_id: str
    target_type: MarketTickTargetType
    target_id: str
    chain: str | None
    token_address: str | None
    exchange: str | None
    instrument: str | None
    pricefeed_id: str | None
    source_tier: MarketTickSourceTier
    source_provider: MarketTickSourceProvider
    observed_at_ms: int
    received_at_ms: int
    price_usd: Decimal
    liquidity_usd: Decimal | None
    volume_24h_usd: Decimal | None
    market_cap_usd: Decimal | None
    holders: int | None
    created_at_ms: int
    open_interest_usd: Decimal | None = None
    raw_payload_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EnrichedEventCapture:
    event_id: str
    intent_id: str
    resolution_id: str
    target_type: MarketTickTargetType
    target_id: str
    t_event_ms: int
    tick_observed_at_ms: int | None
    tick_id: str | None
    tick_lag_ms: int | None
    capture_method: EventCaptureMethod
    capture_reason: str
    created_at_ms: int
