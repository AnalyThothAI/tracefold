"""Historical candle adapters (#88 §6): closed trade-price bars, normalized to one interval convention.

Both providers report a candle's end inclusively (`closeTime` / `T` are one millisecond short of the next
open). Normalizing to an exclusive `close_at_ms = open + interval` here means the domain's "last candle
closed at or before this instant" never has to know whose off-by-one it is looking at.

Only trade prices: no mark, oracle, index or mid history is mixed into `reaction_v1`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx

from tracefold.news.market_review.pricing import CANDLE_INTERVAL, CANDLE_INTERVAL_MS, Candle, parse_price

from .errors import VenueExpectedError
from .http import get_json, post_json, price_client

BINANCE_SPOT_BASE_URL: Final = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL: Final = "https://fapi.binance.com"
HYPERLIQUID_BASE_URL: Final = "https://api.hyperliquid.xyz"

# Binance caps a klines page at 1000 (spot) / 1500 (USD-M); one merged 4 h window is 49 five-minute bars, so
# the cap is only reached by a backfill range and the request is truncated rather than paged.
_BINANCE_LIMIT_MAX: Final = 1000


@dataclass(frozen=True, slots=True)
class VenueBar:
    """Provider OHLCV normalized to exclusive close time; no execution semantics."""

    open_at_ms: int
    close_at_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


async def fetch_binance_candles(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    transport: httpx.AsyncBaseTransport | None = None,
    spot_base_url: str = BINANCE_SPOT_BASE_URL,
    futures_base_url: str = BINANCE_FUTURES_BASE_URL,
) -> tuple[Candle, ...]:
    bars = await fetch_binance_bars(
        venue_symbol,
        venue=venue,
        start_ms=start_ms,
        end_ms=end_ms,
        transport=transport,
        spot_base_url=spot_base_url,
        futures_base_url=futures_base_url,
    )
    return tuple(Candle(open_at_ms=bar.open_at_ms, close_at_ms=bar.close_at_ms, close=bar.close) for bar in bars)


async def fetch_binance_bars(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    transport: httpx.AsyncBaseTransport | None = None,
    spot_base_url: str = BINANCE_SPOT_BASE_URL,
    futures_base_url: str = BINANCE_FUTURES_BASE_URL,
) -> tuple[VenueBar, ...]:
    spot = venue == "binance.spot"
    base = (spot_base_url if spot else futures_base_url).rstrip("/")
    path = "/api/v3/klines" if spot else "/fapi/v1/klines"
    params = {
        "symbol": str(venue_symbol).upper(),
        "interval": CANDLE_INTERVAL,
        "startTime": int(start_ms),
        "endTime": int(end_ms),
        "limit": _limit_for(start_ms, end_ms),
    }
    async with price_client(transport) as client:
        payload = await get_json(client, f"{base}{path}", venue=venue, params=params)
    if not isinstance(payload, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[VenueBar] = []
    for entry in payload:
        if not isinstance(entry, Sequence) or isinstance(entry, str | bytes) or len(entry) < 6:
            continue
        open_at_ms = _optional_int(entry[0])
        prices = tuple(parse_price(entry[index]) for index in range(1, 5))
        volume = _nonnegative_decimal(entry[5])
        if open_at_ms is None or any(price is None for price in prices) or volume is None:
            continue
        open_price, high, low, close = prices
        if open_price is None or high is None or low is None or close is None:
            continue
        if high < max(open_price, close) or low > min(open_price, close):
            continue
        out.append(
            VenueBar(
                open_at_ms=open_at_ms,
                close_at_ms=open_at_ms + CANDLE_INTERVAL_MS,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    return tuple(out)


async def fetch_hyperliquid_candles(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = HYPERLIQUID_BASE_URL,
) -> tuple[Candle, ...]:
    payload = await _fetch_hyperliquid_payload(
        venue_symbol,
        venue=venue,
        start_ms=start_ms,
        end_ms=end_ms,
        transport=transport,
        base_url=base_url,
    )
    out: list[Candle] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        open_at_ms, close = _optional_int(entry.get("t")), parse_price(entry.get("c"))
        if open_at_ms is None or close is None:
            continue
        out.append(Candle(open_at_ms=open_at_ms, close_at_ms=open_at_ms + CANDLE_INTERVAL_MS, close=close))
    return tuple(out)


async def fetch_hyperliquid_bars(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = HYPERLIQUID_BASE_URL,
) -> tuple[VenueBar, ...]:
    """`coin` is the market key: `BTC` on the main perp, `@107` on spot, `xyz:AAPL` on a builder DEX."""

    payload = await _fetch_hyperliquid_payload(
        venue_symbol,
        venue=venue,
        start_ms=start_ms,
        end_ms=end_ms,
        transport=transport,
        base_url=base_url,
    )
    out: list[VenueBar] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        open_at_ms = _optional_int(entry.get("t"))
        open_price = parse_price(entry.get("o"))
        high = parse_price(entry.get("h"))
        low = parse_price(entry.get("l"))
        close = parse_price(entry.get("c"))
        volume = _nonnegative_decimal(entry.get("v"))
        if open_at_ms is None or None in (open_price, high, low, close, volume):
            continue
        if (
            not isinstance(open_price, Decimal)
            or not isinstance(high, Decimal)
            or not isinstance(low, Decimal)
            or not isinstance(close, Decimal)
            or not isinstance(volume, Decimal)
        ):
            continue
        if high < max(open_price, close) or low > min(open_price, close):
            continue
        out.append(
            VenueBar(
                open_at_ms=open_at_ms,
                close_at_ms=open_at_ms + CANDLE_INTERVAL_MS,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    return tuple(out)


async def _fetch_hyperliquid_payload(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    transport: httpx.AsyncBaseTransport | None,
    base_url: str,
) -> Sequence[Any]:
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": str(venue_symbol),
            "interval": CANDLE_INTERVAL,
            "startTime": int(start_ms),
            "endTime": int(end_ms),
        },
    }
    async with price_client(transport) as client:
        payload = await post_json(client, f"{base_url.rstrip('/')}/info", body, venue=venue)
    if not isinstance(payload, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return payload


def _limit_for(start_ms: int, end_ms: int) -> int:
    span = max(0, int(end_ms) - int(start_ms))
    return max(1, min(_BINANCE_LIMIT_MAX, span // CANDLE_INTERVAL_MS + 2))


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _nonnegative_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


__all__ = [
    "BINANCE_FUTURES_BASE_URL",
    "BINANCE_SPOT_BASE_URL",
    "HYPERLIQUID_BASE_URL",
    "VenueBar",
    "fetch_binance_bars",
    "fetch_binance_candles",
    "fetch_hyperliquid_bars",
    "fetch_hyperliquid_candles",
]
