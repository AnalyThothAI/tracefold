"""Historical candle adapters (#88 §6): closed trade-price bars, normalized to one interval convention.

Both providers report a candle's end inclusively (`closeTime` / `T` are one millisecond short of the next
open). Normalizing to an exclusive `close_at_ms = open + interval` here means the domain's "last candle
closed at or before this instant" never has to know whose off-by-one it is looking at.

Only trade prices: no mark, oracle, index or mid history is mixed into `reaction_v1`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from tracefold.news.market_review.pricing import CANDLE_INTERVAL, CANDLE_INTERVAL_MS, Candle, parse_price

from .errors import VenueExpectedError
from .http import get_json, post_json, price_client

BINANCE_SPOT_BASE_URL: Final = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL: Final = "https://fapi.binance.com"
HYPERLIQUID_BASE_URL: Final = "https://api.hyperliquid.xyz"
OKX_BASE_URL: Final = "https://www.okx.com"

# Binance caps a klines page at 1000 (spot) / 1500 (USD-M); one merged 4 h window is 49 five-minute bars, so
# the cap is only reached by a backfill range and the request is truncated rather than paged.
_BINANCE_LIMIT_MAX: Final = 1000
_INTERVAL_MS: Final = {"1m": 60_000, CANDLE_INTERVAL: CANDLE_INTERVAL_MS}


async def fetch_binance_candles(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    interval: str = CANDLE_INTERVAL,
    transport: httpx.AsyncBaseTransport | None = None,
    spot_base_url: str = BINANCE_SPOT_BASE_URL,
    futures_base_url: str = BINANCE_FUTURES_BASE_URL,
) -> tuple[Candle, ...]:
    interval_ms = _interval_ms(interval)
    spot = venue == "binance.spot"
    base = (spot_base_url if spot else futures_base_url).rstrip("/")
    path = "/api/v3/klines" if spot else "/fapi/v1/klines"
    params = {
        "symbol": str(venue_symbol).upper(),
        "interval": interval,
        "startTime": int(start_ms),
        "endTime": int(end_ms),
        "limit": _limit_for(start_ms, end_ms, interval_ms=interval_ms),
    }
    async with price_client(transport) as client:
        payload = await get_json(client, f"{base}{path}", venue=venue, params=params)
    if not isinstance(payload, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[Candle] = []
    for entry in payload:
        if not isinstance(entry, Sequence) or isinstance(entry, str | bytes) or len(entry) < 5:
            continue
        open_at_ms, close = _optional_int(entry[0]), parse_price(entry[4])
        if open_at_ms is None or close is None:
            continue
        out.append(Candle(open_at_ms=open_at_ms, close_at_ms=open_at_ms + interval_ms, close=close))
    return tuple(out)


async def fetch_hyperliquid_candles(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    interval: str = CANDLE_INTERVAL,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = HYPERLIQUID_BASE_URL,
) -> tuple[Candle, ...]:
    """`coin` is the market key: `BTC` on the main perp, `@107` on spot, `xyz:AAPL` on a builder DEX."""

    interval_ms = _interval_ms(interval)
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": str(venue_symbol),
            "interval": interval,
            "startTime": int(start_ms),
            "endTime": int(end_ms),
        },
    }
    async with price_client(transport) as client:
        payload = await post_json(client, f"{base_url.rstrip('/')}/info", body, venue=venue)
    if not isinstance(payload, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[Candle] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        open_at_ms, close = _optional_int(entry.get("t")), parse_price(entry.get("c"))
        if open_at_ms is None or close is None:
            continue
        out.append(Candle(open_at_ms=open_at_ms, close_at_ms=open_at_ms + interval_ms, close=close))
    return tuple(out)


async def fetch_okx_candles(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    interval: str = CANDLE_INTERVAL,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = OKX_BASE_URL,
) -> tuple[Candle, ...]:
    interval_ms = _interval_ms(interval)
    bar = "1m" if interval == "1m" else "5m"
    params = {
        "instId": str(venue_symbol).upper(),
        "bar": bar,
        # OKX names this cursor `after`: it returns candles older than the supplied timestamp.
        "after": int(end_ms),
        "limit": min(300, _limit_for(start_ms, end_ms, interval_ms=interval_ms)),
    }
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base_url.rstrip('/')}/api/v5/market/history-candles",
            venue=venue,
            params=params,
        )
    if not isinstance(payload, Mapping) or str(payload.get("code") or "") != "0":
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    rows = payload.get("data")
    if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[Candle] = []
    for entry in rows:
        if not isinstance(entry, Sequence) or isinstance(entry, str | bytes) or len(entry) < 9:
            continue
        open_at_ms, close = _optional_int(entry[0]), parse_price(entry[4])
        if open_at_ms is None or close is None or str(entry[8]) != "1":
            continue
        if open_at_ms + interval_ms < int(start_ms) or open_at_ms > int(end_ms):
            continue
        out.append(Candle(open_at_ms=open_at_ms, close_at_ms=open_at_ms + interval_ms, close=close))
    return tuple(out)


def _limit_for(start_ms: int, end_ms: int, *, interval_ms: int) -> int:
    span = max(0, int(end_ms) - int(start_ms))
    return max(1, min(_BINANCE_LIMIT_MAX, span // int(interval_ms) + 2))


def _interval_ms(interval: str) -> int:
    try:
        return _INTERVAL_MS[str(interval)]
    except KeyError as exc:
        raise ValueError("candle_interval_unsupported") from exc


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BINANCE_FUTURES_BASE_URL",
    "BINANCE_SPOT_BASE_URL",
    "HYPERLIQUID_BASE_URL",
    "fetch_binance_candles",
    "fetch_hyperliquid_candles",
    "fetch_okx_candles",
]
