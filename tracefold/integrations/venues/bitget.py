"""Bitget public spot and USDT-perpetual catalogues for exact ticker lookup."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from tracefold.news.market_review.instruments import Instrument, classify, is_valid_symbol

from .errors import VenueExpectedError
from .http import get_json, price_client

BITGET_BASE_URL: Final = "https://api.bitget.com"


async def fetch_bitget_instruments(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = BITGET_BASE_URL,
) -> tuple[Instrument, ...]:
    out: list[Instrument] = []
    async with price_client(transport) as client:
        for category, venue in (("USDT-FUTURES", "bitget.perp"), ("SPOT", "bitget.spot")):
            payload = await get_json(
                client,
                f"{base_url.rstrip('/')}/api/v3/market/instruments",
                venue=venue,
                params={"category": category},
            )
            out.extend(_parse(payload, venue=venue))
    if not out:
        raise VenueExpectedError("venue_payload_empty", venue="bitget")
    return tuple(out)


def _parse(payload: Any, *, venue: str) -> tuple[Instrument, ...]:
    if not isinstance(payload, Mapping) or str(payload.get("code") or "") != "00000":
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    rows = payload.get("data")
    if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[Instrument] = []
    seen: set[str] = set()
    for entry in rows:
        if not isinstance(entry, Mapping) or str(entry.get("status") or "") != "online":
            continue
        venue_symbol = str(entry.get("symbol") or "").strip().upper()
        raw_base = str(entry.get("baseCoin") or "").strip()
        reality = str(entry.get("isReality") or "").lower() == "yes"
        base = (raw_base[1:] if reality and raw_base[:1].lower() == "r" else raw_base).upper()
        quote = str(entry.get("quoteCoin") or "").strip().upper()
        if not venue_symbol or venue_symbol in seen or not is_valid_symbol(base):
            continue
        seen.add(venue_symbol)
        symbol_type = str(entry.get("symbolType") or "").lower()
        instrument_class = "equity" if symbol_type == "stock" else classify(base, venue="binance.perp")
        out.append(
            Instrument(
                venue=venue,
                venue_symbol=venue_symbol,
                base_symbol=base,
                instrument_class=instrument_class,
                quote_asset=quote or None,
            )
        )
    return tuple(out)


__all__ = ["BITGET_BASE_URL", "fetch_bitget_instruments"]
