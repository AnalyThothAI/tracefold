"""Public trade-history adapters used for delivery-time price anchors.

Binance can address the last aggregate trade at an arbitrary millisecond timestamp. Hyperliquid and OKX
only expose a recent-trade window through their small public REST endpoints, so callers filter that window and
fall back to closed one-minute candles when the requested timestamp is no longer present.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from tracefold.news.market_review.pricing import Trade, parse_price

from .errors import VenueExpectedError
from .http import get_json, post_json, price_client

BINANCE_SPOT_BASE_URL: Final = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL: Final = "https://fapi.binance.com"
HYPERLIQUID_BASE_URL: Final = "https://api.hyperliquid.xyz"
OKX_BASE_URL: Final = "https://www.okx.com"
LIGHTER_BASE_URL: Final = "https://mainnet.zklighter.elliot.ai"
BITGET_BASE_URL: Final = "https://api.bitget.com"


async def fetch_binance_trade_before(
    venue_symbol: str,
    *,
    venue: str,
    target_ms: int,
    transport: httpx.AsyncBaseTransport | None = None,
    spot_base_url: str = BINANCE_SPOT_BASE_URL,
    futures_base_url: str = BINANCE_FUTURES_BASE_URL,
) -> tuple[Trade, ...]:
    """Ask Binance for exactly the latest aggregate trade no later than ``target_ms``."""

    spot = venue == "binance.spot"
    base = (spot_base_url if spot else futures_base_url).rstrip("/")
    path = "/api/v3/aggTrades" if spot else "/fapi/v1/aggTrades"
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base}{path}",
            venue=venue,
            params={"symbol": str(venue_symbol).upper(), "endTime": int(target_ms), "limit": 1},
        )
    return _parse_trades(payload, venue=venue, price_key="p", time_key="T")


async def fetch_hyperliquid_recent_trades(
    venue_symbol: str,
    *,
    venue: str,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = HYPERLIQUID_BASE_URL,
) -> tuple[Trade, ...]:
    async with price_client(transport) as client:
        payload = await post_json(
            client,
            f"{base_url.rstrip('/')}/info",
            {"type": "recentTrades", "coin": str(venue_symbol)},
            venue=venue,
        )
    return _parse_trades(payload, venue=venue, price_key="px", time_key="time")


async def fetch_okx_recent_trades(
    venue_symbol: str,
    *,
    venue: str,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = OKX_BASE_URL,
) -> tuple[Trade, ...]:
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base_url.rstrip('/')}/api/v5/market/history-trades",
            venue=venue,
            params={"instId": str(venue_symbol), "limit": 100},
        )
    if not isinstance(payload, Mapping) or str(payload.get("code") or "") != "0":
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return _parse_trades(payload.get("data"), venue=venue, price_key="px", time_key="ts")


async def fetch_lighter_recent_trades(
    venue_symbol: str,
    *,
    venue: str,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = LIGHTER_BASE_URL,
) -> tuple[Trade, ...]:
    try:
        market_id = int(venue_symbol)
    except ValueError:
        raise VenueExpectedError("venue_symbol_invalid", venue=venue) from None
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base_url.rstrip('/')}/api/v1/recentTrades",
            venue=venue,
            params={"market_id": market_id, "limit": 100},
        )
    if not isinstance(payload, Mapping) or int(payload.get("code") or 0) != 200:
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return _parse_trades(payload.get("trades"), venue=venue, price_key="price", time_key="timestamp")


async def fetch_bitget_recent_trades(
    venue_symbol: str,
    *,
    venue: str,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BITGET_BASE_URL,
) -> tuple[Trade, ...]:
    category = "SPOT" if venue == "bitget.spot" else "USDT-FUTURES"
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base_url.rstrip('/')}/api/v3/market/fills",
            venue=venue,
            params={"category": category, "symbol": str(venue_symbol).upper(), "limit": 100},
        )
    if not isinstance(payload, Mapping) or str(payload.get("code") or "") != "00000":
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return _parse_trades(payload.get("data"), venue=venue, price_key="price", time_key="ts")


def _parse_trades(payload: Any, *, venue: str, price_key: str, time_key: str) -> tuple[Trade, ...]:
    if isinstance(payload, str | bytes) or not isinstance(payload, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[Trade] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        traded_at_ms = _optional_int(entry.get(time_key))
        price = parse_price(entry.get(price_key))
        if traded_at_ms is None or price is None:
            continue
        out.append(Trade(traded_at_ms=traded_at_ms, price=price))
    return tuple(out)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "OKX_BASE_URL",
    "fetch_binance_trade_before",
    "fetch_bitget_recent_trades",
    "fetch_hyperliquid_recent_trades",
    "fetch_lighter_recent_trades",
    "fetch_okx_recent_trades",
]
