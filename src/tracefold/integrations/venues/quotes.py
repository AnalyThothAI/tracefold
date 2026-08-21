"""Current-quote adapters (#88 §4): one bounded batch request per provider source, normalized on the way in.

Each venue speaks its own dialect and this is where it stops. Binance publishes a traded last price and a
rolling 24 h percentage from *two* endpoints, read here on two cadences (#109); Hyperliquid publishes a book
mid plus yesterday's close in one request, from which the day change is derived here. Both arrive as
`ProviderQuote`, which declares the price kind's basis rather than letting a derivative mid pass for a cash
last price downstream.

Unauthenticated public REST, never a market socket. That is a recorded decision with a measurement and a
promotion criterion behind it (#109, `docs/ARCHITECTURE.md`), not an unexamined default.
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
    """Price only; the day change is a different question on a different cadence (`..._changes` below)."""

    wanted = _wanted(symbols)
    if not wanted:
        return ()
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base_url.rstrip('/')}/api/v3/ticker/price",
            venue="binance.spot",
            params=_symbols_param(wanted),
        )
    return _parse_binance_price(payload, venue="binance.spot", wanted=wanted)


async def fetch_binance_futures_quotes(
    symbols: Sequence[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BINANCE_FUTURES_BASE_URL,
) -> tuple[ProviderQuote, ...]:
    """USD-M has no `symbols=` list, so the whole market comes back once and is filtered here.

    `ticker/price` rather than `ticker/24hr`: measured 45.5 kB against 270 kB for the same market, because
    92% of the bigger payload is fields we do not display, for symbols nobody asked about (#109).
    """

    wanted = _wanted(symbols)
    if not wanted:
        return ()
    async with price_client(transport) as client:
        payload = await get_json(client, f"{base_url.rstrip('/')}/fapi/v1/ticker/price", venue="binance.perp")
    return _parse_binance_price(payload, venue="binance.perp", wanted=wanted)


async def fetch_binance_spot_changes(
    symbols: Sequence[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BINANCE_SPOT_BASE_URL,
) -> dict[str, float]:
    """The rolling 24 h percentage, keyed by venue symbol. Not a quote — the loop merges it into one."""

    wanted = _wanted(symbols)
    if not wanted:
        return {}
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base_url.rstrip('/')}/api/v3/ticker/24hr",
            venue="binance.spot",
            params=_symbols_param(wanted),
        )
    return _parse_binance_changes(payload, venue="binance.spot", wanted=wanted)


async def fetch_binance_futures_changes(
    symbols: Sequence[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BINANCE_FUTURES_BASE_URL,
) -> dict[str, float]:
    wanted = _wanted(symbols)
    if not wanted:
        return {}
    async with price_client(transport) as client:
        payload = await get_json(client, f"{base_url.rstrip('/')}/fapi/v1/ticker/24hr", venue="binance.perp")
    return _parse_binance_changes(payload, venue="binance.perp", wanted=wanted)


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


def _symbols_param(wanted: set[str]) -> dict[str, Any]:
    """Ask for only what we need where the endpoint allows it; otherwise take the market and filter here."""

    if len(wanted) > _SPOT_SYMBOL_LIST_MAX:
        return {}
    return {"symbols": json.dumps(sorted(wanted), separators=(",", ":"))}


def _binance_rows(payload: Any, *, venue: str) -> Sequence[Any]:
    """A one-symbol request answers with an object; a list request answers with a list. Both are rows."""

    rows = [payload] if isinstance(payload, Mapping) else payload
    if not isinstance(rows, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return rows


def _parse_binance_price(payload: Any, *, venue: str, wanted: set[str]) -> tuple[ProviderQuote, ...]:
    """`ticker/price` carries symbol, price, and on USD-M the venue's own timestamp — nothing else.

    `change_pct` is left unset rather than guessed: the loop merges in whatever its slower change fetch last
    returned, and a symbol it has never covered shows a price with no percentage instead of a stale one.
    """

    out: list[ProviderQuote] = []
    for entry in _binance_rows(payload, venue=venue):
        if not isinstance(entry, Mapping):
            continue
        venue_symbol = str(entry.get("symbol") or "").upper()
        if venue_symbol not in wanted:
            continue
        price = parse_price(entry.get("price"))
        if price is None:
            continue
        out.append(
            ProviderQuote(
                venue_symbol=venue_symbol,
                price=price,
                change_basis=_ROLLING_24H,
                source_at_ms=_optional_int(entry.get("time")),
            )
        )
    return tuple(out)


def _parse_binance_changes(payload: Any, *, venue: str, wanted: set[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for entry in _binance_rows(payload, venue=venue):
        if not isinstance(entry, Mapping):
            continue
        venue_symbol = str(entry.get("symbol") or "").upper()
        change = _optional_float(entry.get("priceChangePercent"))
        if venue_symbol in wanted and change is not None:
            out[venue_symbol] = change
    return out


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
    "fetch_binance_futures_changes",
    "fetch_binance_futures_quotes",
    "fetch_binance_spot_changes",
    "fetch_binance_spot_quotes",
    "fetch_hyperliquid_quotes",
]
