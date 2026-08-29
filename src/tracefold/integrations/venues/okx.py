"""OKX public instrument catalogue: live USDT swaps and liquid-quote spot pairs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx

from tracefold.news.market_review.instruments import Instrument, classify, is_valid_symbol

from .errors import VenueExpectedError
from .http import get_json, price_client

OKX_BASE_URL: Final = "https://www.okx.com"
_SPOT_QUOTES: Final = frozenset({"USDT", "USDC"})


async def fetch_okx_instruments(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = OKX_BASE_URL,
) -> tuple[Instrument, ...]:
    out: list[Instrument] = []
    async with price_client(transport) as client:
        for inst_type, venue in (("SWAP", "okx.perp"), ("SPOT", "okx.spot")):
            payload = await get_json(
                client,
                f"{base_url.rstrip('/')}/api/v5/public/instruments",
                venue=venue,
                params={"instType": inst_type},
            )
            out.extend(_parse(payload, venue=venue))
    if not out:
        raise VenueExpectedError("venue_payload_empty", venue="okx")
    return tuple(out)


def _parse(payload: Any, *, venue: str) -> tuple[Instrument, ...]:
    if not isinstance(payload, Mapping) or str(payload.get("code") or "") != "0":
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    rows = payload.get("data")
    if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue=venue)
    out: list[Instrument] = []
    seen: set[str] = set()
    for entry in rows:
        if not isinstance(entry, Mapping) or str(entry.get("state") or "") != "live":
            continue
        venue_symbol = str(entry.get("instId") or "").strip().upper()
        if venue == "okx.perp":
            base = str(entry.get("ctValCcy") or "").strip().upper()
            quote = str(entry.get("settleCcy") or "").strip().upper()
            if not venue_symbol.endswith("-SWAP") or quote != "USDT":
                continue
        else:
            base = str(entry.get("baseCcy") or "").strip().upper()
            quote = str(entry.get("quoteCcy") or "").strip().upper()
            if quote not in _SPOT_QUOTES:
                continue
        if not venue_symbol or venue_symbol in seen or not is_valid_symbol(base):
            continue
        seen.add(venue_symbol)
        out.append(
            Instrument(
                venue=venue,
                venue_symbol=venue_symbol,
                base_symbol=base,
                instrument_class=_instrument_class(base, entry=entry, venue=venue),
                quote_asset=quote or None,
            )
        )
    return tuple(out)


def _instrument_class(base: str, *, entry: Mapping[str, Any], venue: str) -> str:
    """OKX category 3 is its TradFi family; retain the domain's commodity/index hints inside that family."""

    if venue == "okx.perp" and str(entry.get("instCategory") or "") == "3":
        hinted = classify(base, venue="okx.perp")
        return hinted if hinted != "unknown" else "equity"
    return classify(base, venue="binance.perp")


__all__ = ["OKX_BASE_URL", "fetch_okx_instruments"]
