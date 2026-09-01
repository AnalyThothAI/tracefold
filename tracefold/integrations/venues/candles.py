"""Historical candle adapters (#88 §6): closed trade-price bars, normalized to one interval convention.

Both providers report a candle's end inclusively (`closeTime` / `T` are one millisecond short of the next
open). Normalizing to an exclusive `close_at_ms = open + interval` here means the domain's "last candle
closed at or before this instant" never has to know whose off-by-one it is looking at.

Only trade prices: no mark, oracle, index or mid history is mixed into `reaction_v1`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx

from tracefold.news.market_review.pricing import CANDLE_INTERVAL, CANDLE_INTERVAL_MS, Candle, parse_price

from .errors import VenueExpectedError
from .http import get_json, post_json, price_client

BINANCE_SPOT_BASE_URL: Final = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL: Final = "https://fapi.binance.com"
HYPERLIQUID_BASE_URL: Final = "https://api.hyperliquid.xyz"
OKX_BASE_URL: Final = "https://www.okx.com"
LIGHTER_BASE_URL: Final = "https://mainnet.zklighter.elliot.ai"
BITGET_BASE_URL: Final = "https://api.bitget.com"

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
    if interval == CANDLE_INTERVAL:
        return await _fetch_binance_reaction_candles(
            venue_symbol,
            venue=venue,
            start_ms=start_ms,
            end_ms=end_ms,
            transport=transport,
            spot_base_url=spot_base_url,
            futures_base_url=futures_base_url,
        )
    interval_ms = _interval_ms(interval)
    payload = await _fetch_binance_payload(
        venue_symbol,
        venue=venue,
        start_ms=start_ms,
        end_ms=end_ms,
        interval=interval,
        interval_ms=interval_ms,
        transport=transport,
        spot_base_url=spot_base_url,
        futures_base_url=futures_base_url,
    )
    out: list[Candle] = []
    for entry in payload:
        if not isinstance(entry, Sequence) or isinstance(entry, str | bytes) or len(entry) < 5:
            continue
        open_at_ms, close = _optional_int(entry[0]), parse_price(entry[4])
        if open_at_ms is not None and close is not None:
            out.append(Candle(open_at_ms=open_at_ms, close_at_ms=open_at_ms + interval_ms, close=close))
    return tuple(out)


async def _fetch_binance_reaction_candles(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    transport: httpx.AsyncBaseTransport | None,
    spot_base_url: str,
    futures_base_url: str,
) -> tuple[Candle, ...]:
    """The `reaction_v1` interval, which reads O/H/L/V it does not keep.

    Only `close` reaches a `Candle`, but a bar whose high is below its own open or close is not a bar
    that lost precision — it is a row the provider did not mean, and taking its close would put a price
    into a reaction measurement that no trade ever printed. Parsing all six fields is how that row is
    recognized, so the general-interval branch below (which asks for five) stays a different function
    rather than becoming a looser version of this one.
    """

    payload = await _fetch_binance_payload(
        venue_symbol,
        venue=venue,
        start_ms=start_ms,
        end_ms=end_ms,
        interval=CANDLE_INTERVAL,
        interval_ms=CANDLE_INTERVAL_MS,
        transport=transport,
        spot_base_url=spot_base_url,
        futures_base_url=futures_base_url,
    )
    out: list[Candle] = []
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
        out.append(Candle(open_at_ms=open_at_ms, close_at_ms=open_at_ms + CANDLE_INTERVAL_MS, close=close))
    return tuple(out)


async def _fetch_binance_payload(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    interval_ms: int,
    transport: httpx.AsyncBaseTransport | None,
    spot_base_url: str,
    futures_base_url: str,
) -> Sequence[Any]:
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
    return payload


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
    interval_ms = _interval_ms(interval)
    payload = await _fetch_hyperliquid_payload(
        venue_symbol,
        venue=venue,
        start_ms=start_ms,
        end_ms=end_ms,
        interval=interval,
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
        out.append(Candle(open_at_ms=open_at_ms, close_at_ms=open_at_ms + interval_ms, close=close))
    return tuple(out)


async def _fetch_hyperliquid_payload(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    interval: str,
    transport: httpx.AsyncBaseTransport | None,
    base_url: str,
) -> Sequence[Any]:
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
    return payload


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


async def fetch_lighter_candles(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    interval: str = CANDLE_INTERVAL,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = LIGHTER_BASE_URL,
) -> tuple[Candle, ...]:
    interval_ms = _interval_ms(interval)
    try:
        market_id = int(venue_symbol)
    except ValueError:
        raise VenueExpectedError("venue_symbol_invalid", venue=venue) from None
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base_url.rstrip('/')}/api/v1/candles",
            venue=venue,
            params={
                "market_id": market_id,
                "resolution": interval,
                "start_timestamp": int(start_ms),
                "end_timestamp": int(end_ms),
                "count_back": min(500, _limit_for(start_ms, end_ms, interval_ms=interval_ms)),
            },
        )
    if not isinstance(payload, Mapping) or int(payload.get("code") or 0) != 200:
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    rows = payload.get("c")
    if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return _mapping_candles(rows, venue=venue, interval_ms=interval_ms, time_key="t", close_key="c")


async def fetch_bitget_candles(
    venue_symbol: str,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    interval: str = CANDLE_INTERVAL,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BITGET_BASE_URL,
) -> tuple[Candle, ...]:
    interval_ms = _interval_ms(interval)
    category = "SPOT" if venue == "bitget.spot" else "USDT-FUTURES"
    api_interval = "1m" if interval == "1m" else "5m"
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base_url.rstrip('/')}/api/v3/market/history-candles",
            venue=venue,
            params={
                "category": category,
                "symbol": str(venue_symbol).upper(),
                "interval": api_interval,
                "startTime": int(start_ms),
                "endTime": int(end_ms),
                "limit": min(100, _limit_for(start_ms, end_ms, interval_ms=interval_ms)),
            },
        )
    if not isinstance(payload, Mapping) or str(payload.get("code") or "") != "00000":
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    rows = payload.get("data")
    if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[Candle] = []
    for entry in rows:
        if not isinstance(entry, Sequence) or isinstance(entry, str | bytes) or len(entry) < 5:
            continue
        open_at_ms, close = _optional_int(entry[0]), parse_price(entry[4])
        if open_at_ms is not None and close is not None:
            out.append(Candle(open_at_ms=open_at_ms, close_at_ms=open_at_ms + interval_ms, close=close))
    return tuple(out)


def _mapping_candles(
    rows: Sequence[Any], *, venue: str, interval_ms: int, time_key: str, close_key: str
) -> tuple[Candle, ...]:
    out: list[Candle] = []
    for entry in rows:
        if not isinstance(entry, Mapping):
            continue
        open_at_ms, close = _optional_int(entry.get(time_key)), parse_price(entry.get(close_key))
        if open_at_ms is not None and close is not None:
            out.append(Candle(open_at_ms=open_at_ms, close_at_ms=open_at_ms + interval_ms, close=close))
    if not out and rows:
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
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
    "fetch_binance_candles",
    "fetch_bitget_candles",
    "fetch_hyperliquid_candles",
    "fetch_lighter_candles",
    "fetch_okx_candles",
]
