"""Current-quote adapters (#88 §4): one bounded batch request per provider source, normalized on the way in.

Each venue speaks its own dialect and this is where it stops. Binance publishes a traded last price plus a
rolling 24 h percentage; Hyperliquid publishes a book mid plus yesterday's close, from which the day change
is derived here. Both arrive as `ProviderQuote`, which declares the price kind's basis rather than letting a
derivative mid pass for a cash last price downstream.

V1 uses unauthenticated public REST on a five-second cadence, never a market socket. A WSS implementation
would satisfy this same interface and change nothing in persistence, HTTP or the browser.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from tracefold.news.pricing import ProviderQuote, parse_change_pct, parse_price

from .errors import VenueExpectedError
from .http import get_json, post_json, price_client

BINANCE_SPOT_BASE_URL: Final = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL: Final = "https://fapi.binance.com"
HYPERLIQUID_BASE_URL: Final = "https://api.hyperliquid.xyz"

# Binance charges by response breadth: an explicit `symbols=[...]` list is weight 2-40, the whole market is
# weight 80 (spot) / 42 (USD-M, measured). Past this many symbols the list form stops being the cheaper one,
# and the USD-M endpoint has no list form at all — one request for the market, filtered locally.
_SPOT_SYMBOL_LIST_MAX: Final = 100
_ROLLING_24H: Final = "rolling_24h"
_PROVIDER_DAY: Final = "provider_day"


async def fetch_binance_spot_quotes(
    symbols: Sequence[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BINANCE_SPOT_BASE_URL,
) -> tuple[ProviderQuote, ...]:
    wanted = _wanted(symbols)
    if not wanted:
        return ()
    params: dict[str, Any] = {}
    if len(wanted) <= _SPOT_SYMBOL_LIST_MAX:
        params["symbols"] = json.dumps(sorted(wanted), separators=(",", ":"))
    async with price_client(transport) as client:
        payload = await get_json(
            client, f"{base_url.rstrip('/')}/api/v3/ticker/24hr", venue="binance.spot", params=params
        )
    return _parse_binance(payload, venue="binance.spot", wanted=wanted)


async def fetch_binance_futures_quotes(
    symbols: Sequence[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BINANCE_FUTURES_BASE_URL,
) -> tuple[ProviderQuote, ...]:
    """USD-M has no `symbols=` list, so the whole market comes back once and is filtered here."""

    wanted = _wanted(symbols)
    if not wanted:
        return ()
    async with price_client(transport) as client:
        payload = await get_json(client, f"{base_url.rstrip('/')}/fapi/v1/ticker/24hr", venue="binance.perp")
    return _parse_binance(payload, venue="binance.perp", wanted=wanted)


async def fetch_hyperliquid_quotes(
    symbols: Sequence[str],
    *,
    venue: str,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = HYPERLIQUID_BASE_URL,
) -> tuple[ProviderQuote, ...]:
    """Main perps, spot pairs, or one HIP-3 builder DEX — the venue decides which request shape is right.

    Perp contexts are index-aligned with `meta.universe`; spot contexts are not and carry their own `coin`,
    which is exactly the `@107` / `PURR/USDC` market key the catalogue now stores (#88 §3).
    """

    wanted = _wanted(symbols)
    if not wanted:
        return ()
    url = f"{base_url.rstrip('/')}/info"
    spot = venue == "hl.spot"
    body: dict[str, Any] = {"type": "spotMetaAndAssetCtxs"} if spot else {"type": "metaAndAssetCtxs"}
    dex = venue.split(".", 1)[1] if venue.startswith("hl.") else ""
    if not spot and dex and dex != "perp":
        body["dex"] = dex
    async with price_client(transport) as client:
        payload = await post_json(client, url, body, venue=venue)
    if not isinstance(payload, Sequence) or len(payload) < 2:
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    meta, contexts = payload[0], payload[1]
    if not isinstance(contexts, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    if spot:
        return _parse_hyperliquid_spot(contexts, wanted=wanted, venue=venue)
    return _parse_hyperliquid_perp(meta, contexts, wanted=wanted, venue=venue)


def _wanted(symbols: Sequence[str]) -> set[str]:
    return {str(symbol).strip() for symbol in symbols if str(symbol).strip()}


def _parse_binance(payload: Any, *, venue: str, wanted: set[str]) -> tuple[ProviderQuote, ...]:
    rows = [payload] if isinstance(payload, Mapping) else payload
    if not isinstance(rows, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[ProviderQuote] = []
    for entry in rows:
        if not isinstance(entry, Mapping):
            continue
        venue_symbol = str(entry.get("symbol") or "").upper()
        if venue_symbol not in wanted:
            continue
        price = parse_price(entry.get("lastPrice"))
        if price is None:
            continue
        out.append(
            ProviderQuote(
                venue_symbol=venue_symbol,
                price=price,
                change_pct=_optional_float(entry.get("priceChangePercent")),
                change_basis=_ROLLING_24H,
                source_at_ms=_optional_int(entry.get("closeTime")),
            )
        )
    return tuple(out)


def _parse_hyperliquid_perp(
    meta: Any, contexts: Sequence[Any], *, wanted: set[str], venue: str
) -> tuple[ProviderQuote, ...]:
    universe = (meta or {}).get("universe") if isinstance(meta, Mapping) else None
    if not isinstance(universe, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[ProviderQuote] = []
    for entry, context in zip(universe, contexts, strict=False):
        if not isinstance(entry, Mapping) or not isinstance(context, Mapping):
            continue
        venue_symbol = str(entry.get("name") or "").strip()
        if venue_symbol not in wanted:
            continue
        price = parse_price(context.get("midPx"))
        if price is None:
            continue
        out.append(
            ProviderQuote(
                venue_symbol=venue_symbol,
                price=price,
                change_pct=parse_change_pct(price, context.get("prevDayPx")),
                change_basis=_PROVIDER_DAY,
            )
        )
    return tuple(out)


def _parse_hyperliquid_spot(contexts: Sequence[Any], *, wanted: set[str], venue: str) -> tuple[ProviderQuote, ...]:
    out: list[ProviderQuote] = []
    for context in contexts:
        if not isinstance(context, Mapping):
            continue
        venue_symbol = str(context.get("coin") or "").strip()
        if venue_symbol not in wanted:
            continue
        price = parse_price(context.get("midPx")) or parse_price(context.get("markPx"))
        if price is None:
            continue
        out.append(
            ProviderQuote(
                venue_symbol=venue_symbol,
                price=price,
                change_pct=parse_change_pct(price, context.get("prevDayPx")),
                change_basis=_PROVIDER_DAY,
            )
        )
    return tuple(out)


def _optional_float(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BINANCE_FUTURES_BASE_URL",
    "BINANCE_SPOT_BASE_URL",
    "HYPERLIQUID_BASE_URL",
    "fetch_binance_futures_quotes",
    "fetch_binance_spot_quotes",
    "fetch_hyperliquid_quotes",
]
