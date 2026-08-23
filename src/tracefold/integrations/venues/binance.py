"""Binance instrument catalogue: spot pairs and USD-M perpetuals.

The USD-M futures catalogue is the interesting half — besides crypto it carries the equity/commodity perps
(``UNITREEUSDT``, ``OPENAIUSDT``, ``TENCENTUSDT``, ``CLUSDT``, ``MRVLUSDT``). Querying spot alone understates
coverage by roughly a factor of three, which is exactly the mistake that made an earlier review conclude Binance
could price only a third of our symbols.

Binance labels those contracts itself: ``contractType: TRADIFI_PERPETUAL`` plus an ``underlyingType`` of
``EQUITY`` / ``CN_EQUITY`` / ``HK_EQUITY`` / ``KR_EQUITY`` / ``COMMODITY`` / ``PREMARKET``. Dropping that field and
letting the venue default decide put 81 of the 169 TradFi contracts (JPM, GS, KO, SPY, TENCENT, HK1810, XAU…) into
the universe as ``crypto`` (#89). Translating the venue's vocabulary into ours is the adapter's job — the domain
side (``classify()``) never learns Binance's field names.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from tracefold.news.market_review.instruments import (
    Instrument,
    InstrumentClass,
    classify,
    is_valid_symbol,
    strip_quote_suffix,
)

from .errors import VenueExpectedError

log = logging.getLogger("tracefold.news.venues")

BINANCE_SPOT_BASE_URL: Final = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL: Final = "https://fapi.binance.com"
_TIMEOUT_SECONDS: Final = 20.0
# exchangeInfo is a large document (spot is ~17 MB); cap it so a pathological response cannot exhaust memory.
_MAX_BYTES: Final = 48 * 1024 * 1024
_SPOT_QUOTES: Final = frozenset({"USDT", "USDC", "FDUSD"})
# `underlyingType` values that are unambiguously not crypto. `COIN` and `INDEX` are deliberately absent: Binance's
# only two INDEX perps are crypto gauges (`BTCDOMUSDT`, `ALLUSDT`), so both fall through to `classify()`.
_DECLARED_CLASS: Final[Mapping[str, InstrumentClass]] = {
    "EQUITY": "equity",
    "CN_EQUITY": "equity",
    "HK_EQUITY": "equity",
    "KR_EQUITY": "equity",
    "COMMODITY": "commodity",
    "PREMARKET": "pre_ipo",
}


async def fetch_binance_instruments(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    spot_base_url: str = BINANCE_SPOT_BASE_URL,
    futures_base_url: str = BINANCE_FUTURES_BASE_URL,
) -> tuple[Instrument, ...]:
    """Spot (USDT/USDC/FDUSD quoted) plus USD-M perpetuals, both filtered to actively trading symbols."""

    out: list[Instrument] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_TIMEOUT_SECONDS), follow_redirects=False, transport=transport
    ) as client:
        spot = await _get(client, f"{spot_base_url.rstrip('/')}/api/v3/exchangeInfo", venue="binance.spot")
        out.extend(_parse(spot, venue="binance.spot", quote_filter=_SPOT_QUOTES))
        futures = await _get(client, f"{futures_base_url.rstrip('/')}/fapi/v1/exchangeInfo", venue="binance.perp")
        out.extend(_parse(futures, venue="binance.perp", quote_filter=None))
    return tuple(out)


def _parse(payload: Mapping[str, Any], *, venue: str, quote_filter: frozenset[str] | None) -> tuple[Instrument, ...]:
    symbols = payload.get("symbols")
    if not isinstance(symbols, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[Instrument] = []
    seen: set[str] = set()
    for entry in symbols:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("status") or "") != "TRADING":
            continue
        quote = str(entry.get("quoteAsset") or "").upper()
        if quote_filter is not None and quote not in quote_filter:
            continue
        venue_symbol = str(entry.get("symbol") or "").upper()
        base = str(entry.get("baseAsset") or "").upper() or strip_quote_suffix(venue_symbol, quote_asset=quote)
        if not venue_symbol or not is_valid_symbol(base) or venue_symbol in seen:
            continue
        seen.add(venue_symbol)
        declared = _declared_class(entry)
        out.append(
            Instrument(
                venue=venue,
                venue_symbol=venue_symbol,
                base_symbol=base,
                instrument_class=declared or classify(base, venue=venue),
                quote_asset=quote or None,
            )
        )
    if not out:
        raise VenueExpectedError("venue_payload_empty", venue=venue)
    return tuple(out)


def _declared_class(entry: Mapping[str, Any]) -> InstrumentClass | None:
    """What Binance says this contract is, when it says anything we understand.

    A TradFi contract whose `underlyingType` we do not have a mapping for (Binance adds `JP_EQUITY`, say) must not
    fall through to `classify()`, whose default for `binance.*` is `crypto` — that would silently recreate #89 for
    exactly the new listings nobody is watching. The venue calling it TradFi is the reliable half, so an unmapped
    TradFi underlying reads as `equity` and says so in the log.
    """

    if str(entry.get("contractType") or "").upper() != "TRADIFI_PERPETUAL":
        return None
    underlying = str(entry.get("underlyingType") or "").upper()
    declared = _DECLARED_CLASS.get(underlying)
    if declared is None:
        log.warning(
            "binance tradfi contract with an unmapped underlyingType symbol=%s underlying=%s",
            entry.get("symbol"),
            underlying or "-",
        )
        return "equity"
    return declared


async def _get(client: httpx.AsyncClient, url: str, *, venue: str) -> Mapping[str, Any]:
    try:
        response = await client.get(url)
    except httpx.TimeoutException:
        raise VenueExpectedError("venue_timeout", venue=venue) from None
    except httpx.HTTPError:
        raise VenueExpectedError("venue_http_error", venue=venue) from None
    if response.status_code in {403, 451}:
        raise VenueExpectedError("venue_blocked", venue=venue, status_code=response.status_code)
    if response.status_code == 429:
        raise VenueExpectedError("venue_rate_limited", venue=venue, status_code=response.status_code)
    if response.status_code >= 400:
        raise VenueExpectedError("venue_http_error", venue=venue, status_code=response.status_code)
    if len(response.content) > _MAX_BYTES:
        raise VenueExpectedError("venue_payload_too_large", venue=venue)
    try:
        payload = response.json()
    except ValueError:
        raise VenueExpectedError("venue_payload_invalid", venue=venue) from None
    if not isinstance(payload, Mapping):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    return payload


__all__ = ["BINANCE_FUTURES_BASE_URL", "BINANCE_SPOT_BASE_URL", "fetch_binance_instruments"]
