"""Lighter public market catalogue for post-delivery tradeability checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

import httpx

from tracefold.news.market_review.instruments import Instrument, classify, is_valid_symbol

from .errors import VenueExpectedError
from .http import get_json, price_client

LIGHTER_BASE_URL: Final = "https://mainnet.zklighter.elliot.ai"


async def fetch_lighter_instruments(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = LIGHTER_BASE_URL,
) -> tuple[Instrument, ...]:
    async with price_client(transport) as client:
        payload = await get_json(client, f"{base_url.rstrip('/')}/api/v1/orderBooks", venue="lighter")
    if not isinstance(payload, Mapping) or int(payload.get("code") or 0) != 200:
        raise VenueExpectedError("venue_payload_invalid", venue="lighter")
    rows = payload.get("order_books")
    if isinstance(rows, str | bytes) or not isinstance(rows, Sequence):
        raise VenueExpectedError("venue_payload_invalid", venue="lighter")
    out: list[Instrument] = []
    seen: set[int] = set()
    for entry in rows:
        if not isinstance(entry, Mapping) or str(entry.get("status") or "") != "active":
            continue
        market_id = entry.get("market_id")
        if isinstance(market_id, bool) or not isinstance(market_id, int) or market_id < 0 or market_id in seen:
            continue
        symbol = str(entry.get("symbol") or "").strip().upper()
        market_type = str(entry.get("market_type") or "").strip().lower()
        base, _, quote = symbol.partition("/")
        if market_type not in {"perp", "spot"} or not is_valid_symbol(base):
            continue
        seen.add(market_id)
        # Lighter price endpoints are keyed by the numeric market id, not the display ticker.
        venue = f"lighter.{market_type}"
        hinted = classify(base, venue=venue)
        out.append(
            Instrument(
                venue=venue,
                venue_symbol=str(market_id),
                base_symbol=base,
                instrument_class=hinted,
                quote_asset=quote or "USDC",
            )
        )
    if not out:
        raise VenueExpectedError("venue_payload_empty", venue="lighter")
    return tuple(out)


__all__ = ["LIGHTER_BASE_URL", "fetch_lighter_instruments"]
