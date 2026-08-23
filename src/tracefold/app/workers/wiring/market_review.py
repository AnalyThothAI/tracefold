from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from typing import Any

from tracefold.integrations.venues import (
    fetch_binance_candles,
    fetch_binance_futures_day_quotes,
    fetch_binance_futures_quotes,
    fetch_binance_instruments,
    fetch_binance_spot_day_quotes,
    fetch_binance_spot_quotes,
    fetch_hyperliquid_candles,
    fetch_hyperliquid_instruments,
    fetch_hyperliquid_quotes,
    fetch_us_reference_instruments,
)
from tracefold.news.market_review.loops import EventReactionLoop, QuoteSnapshotLoop
from tracefold.news.pipeline.maintenance import InstrumentSnapshotLoop


def _price_venue_enabled(settings: Any, source_key: str) -> bool:
    """#88 reuses the existing venue switches; the price plane never gets an operator knob of its own."""

    venues = settings.news.venues
    if not venues.enabled:
        return False
    if source_key.startswith("binance."):
        return bool(venues.binance)
    if source_key.startswith("hl."):
        return bool(venues.hyperliquid)
    return False


def _quote_snapshot_loop(settings: Any, *, db: Any, watchlist: Sequence[str]) -> QuoteSnapshotLoop | None:
    """One batch quote adapter per source group, resolved by source key so a new HIP-3 dex needs no wiring."""

    def fetcher_for(source_key: str) -> Any | None:
        if not _price_venue_enabled(settings, source_key):
            return None
        if source_key == "binance.spot":
            return fetch_binance_spot_quotes
        if source_key == "binance.perp":
            return fetch_binance_futures_quotes
        if source_key.startswith("hl."):
            return functools.partial(fetch_hyperliquid_quotes, venue=source_key)
        return None

    def day_fetcher_for(source_key: str) -> Any | None:
        """The wider endpoint one turn in fifteen, for the day-change reference (#109).

        Only Binance has one: a Hyperliquid request already carries `prevDayPx` beside the mid, so its
        ordinary fetcher is its day fetcher and there is nothing to alternate.
        """

        if not _price_venue_enabled(settings, source_key):
            return None
        if source_key == "binance.spot":
            return fetch_binance_spot_day_quotes
        if source_key == "binance.perp":
            return fetch_binance_futures_day_quotes
        return None

    venues = settings.news.venues
    if not venues.enabled or not (venues.binance or venues.hyperliquid):
        return None
    return QuoteSnapshotLoop(db=db, fetcher_for=fetcher_for, day_fetcher_for=day_fetcher_for, watchlist=watchlist)


def _event_reaction_loop(settings: Any, *, db: Any) -> EventReactionLoop | None:
    def fetcher_for(venue: str) -> Any | None:
        if not _price_venue_enabled(settings, venue):
            return None
        if venue.startswith("binance."):

            async def binance(venue_symbol: str, start_ms: int, end_ms: int) -> Any:
                return await fetch_binance_candles(venue_symbol, venue=venue, start_ms=start_ms, end_ms=end_ms)

            return binance
        if venue.startswith("hl."):

            async def hyperliquid(venue_symbol: str, start_ms: int, end_ms: int) -> Any:
                return await fetch_hyperliquid_candles(venue_symbol, venue=venue, start_ms=start_ms, end_ms=end_ms)

            return hyperliquid
        return None

    venues = settings.news.venues
    if not venues.enabled or not (venues.binance or venues.hyperliquid):
        return None
    return EventReactionLoop(db=db, fetcher_for=fetcher_for)


def _instrument_snapshot_loop(settings: Any, *, db: Any) -> InstrumentSnapshotLoop | None:
    """#75: one fetcher per venue family, each independently skippable. No credentials are involved."""

    venues = settings.news.venues
    if not venues.enabled:
        return None
    fetchers: list[tuple[str, Callable[[], Any]]] = []
    if venues.binance:
        fetchers.append(("binance", fetch_binance_instruments))
    if venues.hyperliquid:
        fetchers.append(("hyperliquid", fetch_hyperliquid_instruments))
    if venues.us_reference:
        fetchers.append(("us_reference", fetch_us_reference_instruments))
    if not fetchers:
        return None
    return InstrumentSnapshotLoop(
        db=db,
        fetchers=fetchers,
        period_seconds=float(venues.snapshot_period_hours) * 3600.0,
    )
