"""Current-quote adapters (#304): one bounded batch request per provider source, normalized on the way in.

Each venue speaks its own dialect and this is where it stops. Both publish a price and a reference the day
change is measured against — Hyperliquid's `prevDayPx` in the same request, Binance's rolling-window
`openPrice` only from the wider `ticker/24hr`. Every turn fetches Binance's narrow current endpoint first;
a due wider day read runs only after the current snapshot is committed and enriches the next natural turn.
Both arrive as `ProviderQuote`, which declares the price kind's basis rather than letting a derivative mid
pass for a cash last price downstream.

Unauthenticated public REST, never a market socket. That is a recorded decision with a measurement and a
promotion criterion behind it (#109, `docs/ARCHITECTURE.md`), not an unexamined default.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from tracefold.news.market_review.pricing import ProviderQuote, parse_price

from .errors import VenueExpectedError
from .http import get_json, post_json, price_client

BINANCE_SPOT_BASE_URL: Final = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL: Final = "https://fapi.binance.com"
HYPERLIQUID_BASE_URL: Final = "https://api.hyperliquid.xyz"
OKX_BASE_URL: Final = "https://www.okx.com"

# `ticker/24hr` charges by response breadth: an explicit `symbols=[...]` list is weight 2-40, the whole
# market is weight 80 (spot) / 42 (USD-M, measured). Past this many symbols the list form stops being the
# cheaper one. `ticker/price` is a flat weight 4 at any list length (measured at n=20/100/120), so there the
# list is always worth sending — it is 5 kB against 171 kB. The USD-M endpoints have no list form at all:
# one request for the market, filtered locally.
_DAY_SYMBOL_LIST_MAX: Final = 100
_ROLLING_24H: Final = "rolling_24h"
_PROVIDER_DAY: Final = "provider_day"


async def fetch_binance_spot_quotes(
    symbols: Sequence[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BINANCE_SPOT_BASE_URL,
) -> tuple[ProviderQuote, ...]:
    """Mandatory current prices. Weight 4 whatever the list length, so always ask by name."""

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


async def fetch_binance_spot_day_quotes(
    symbols: Sequence[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BINANCE_SPOT_BASE_URL,
) -> tuple[ProviderQuote, ...]:
    """The same prices plus the 24 h window's open, which is what the day change is measured against."""

    wanted = _wanted(symbols)
    if not wanted:
        return ()
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base_url.rstrip('/')}/api/v3/ticker/24hr",
            venue="binance.spot",
            params=_symbols_param(wanted, max_list=_DAY_SYMBOL_LIST_MAX),
        )
    return _parse_binance_day(payload, venue="binance.spot", wanted=wanted)


async def fetch_binance_futures_day_quotes(
    symbols: Sequence[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BINANCE_FUTURES_BASE_URL,
) -> tuple[ProviderQuote, ...]:
    wanted = _wanted(symbols)
    if not wanted:
        return ()
    async with price_client(transport) as client:
        payload = await get_json(client, f"{base_url.rstrip('/')}/fapi/v1/ticker/24hr", venue="binance.perp")
    return _parse_binance_day(payload, venue="binance.perp", wanted=wanted)


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


async def fetch_okx_quotes(
    symbols: Sequence[str],
    *,
    venue: str,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = OKX_BASE_URL,
) -> tuple[ProviderQuote, ...]:
    """OKX includes last price and its 24-hour open in the same public ticker response."""

    wanted = _wanted(symbols)
    if not wanted:
        return ()
    inst_type = "SPOT" if venue == "okx.spot" else "SWAP"
    async with price_client(transport) as client:
        payload = await get_json(
            client,
            f"{base_url.rstrip('/')}/api/v5/market/tickers",
            venue=venue,
            params={"instType": inst_type},
        )
    if not isinstance(payload, Mapping) or str(payload.get("code") or "") != "0":
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    rows = payload.get("data")
    if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[ProviderQuote] = []
    for entry in rows:
        if not isinstance(entry, Mapping):
            continue
        venue_symbol = str(entry.get("instId") or "").upper()
        if venue_symbol not in wanted:
            continue
        price = parse_price(entry.get("last"))
        if price is None:
            continue
        reference = parse_price(entry.get("open24h"))
        out.append(
            ProviderQuote(
                venue_symbol=venue_symbol,
                price=price,
                change_basis=_ROLLING_24H,
                reference_price=reference,
                source_at_ms=_optional_int(entry.get("ts")),
            )
        )
    return tuple(out)


def _wanted(symbols: Sequence[str]) -> set[str]:
    return {str(symbol).strip() for symbol in symbols if str(symbol).strip()}


def _symbols_param(wanted: set[str], *, max_list: int | None = None) -> dict[str, Any]:
    """Ask for only what we need, unless the list would cost more than the whole market."""

    if max_list is not None and len(wanted) > max_list:
        return {}
    return {"symbols": json.dumps(sorted(wanted), separators=(",", ":"))}


def _binance_rows(payload: Any, *, venue: str) -> Sequence[Any]:
    """Every request this module makes answers with a list. A string is a Sequence and is not one of these.

    Without the `str` guard a proxy or CDN answering 200 with a JSON-encoded error body iterates character by
    character to an empty result, which the loop then reports as the benign `venue_payload_empty` instead of
    naming the payload as invalid.
    """

    if isinstance(payload, str | bytes) or not isinstance(payload, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return payload


def _parse_binance_price(payload: Any, *, venue: str, wanted: set[str]) -> tuple[ProviderQuote, ...]:
    """`ticker/price` carries symbol, price, and on USD-M the venue's own timestamp — nothing else.

    No reference price, so no percentage: the loop derives the day change from the reference its last day
    read cached. A symbol no day read has covered yet shows a price with no percentage, never a borrowed one.
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


def _parse_binance_day(payload: Any, *, venue: str, wanted: set[str]) -> tuple[ProviderQuote, ...]:
    """`ticker/24hr` answers both questions at once: the price, and the window open it is measured against.

    `openPrice` is the rolling window's first trade — `priceChangePercent` is exactly `lastPrice/openPrice-1`.
    Carrying the reference rather than the percentage is what lets the cheap turns in between stay truthful:
    the percentage is recomputed from each turn's own price, so it can never disagree with the number beside
    it. Only the reference is allowed to age, and a 24 h window's open moves 0.023% per 20 s turn.
    """

    out: list[ProviderQuote] = []
    for entry in _binance_rows(payload, venue=venue):
        if not isinstance(entry, Mapping):
            continue
        venue_symbol = str(entry.get("symbol") or "").upper()
        if venue_symbol not in wanted:
            continue
        price = parse_price(entry.get("lastPrice"))
        if price is None:
            continue
        reference = parse_price(entry.get("openPrice"))
        out.append(
            ProviderQuote(
                venue_symbol=venue_symbol,
                price=price,
                change_basis=_ROLLING_24H,
                reference_price=reference,
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
        reference = parse_price(context.get("prevDayPx"))
        out.append(
            ProviderQuote(
                venue_symbol=venue_symbol,
                price=price,
                change_basis=_PROVIDER_DAY,
                reference_price=reference,
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
        reference = parse_price(context.get("prevDayPx"))
        out.append(
            ProviderQuote(
                venue_symbol=venue_symbol,
                price=price,
                change_basis=_PROVIDER_DAY,
                reference_price=reference,
            )
        )
    return tuple(out)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BINANCE_FUTURES_BASE_URL",
    "BINANCE_SPOT_BASE_URL",
    "HYPERLIQUID_BASE_URL",
    "OKX_BASE_URL",
    "fetch_binance_futures_day_quotes",
    "fetch_binance_futures_quotes",
    "fetch_binance_spot_day_quotes",
    "fetch_binance_spot_quotes",
    "fetch_hyperliquid_quotes",
    "fetch_okx_quotes",
]
