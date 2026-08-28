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
    fetch_delivery_price_points,
    fetch_hyperliquid_candles,
    fetch_hyperliquid_instruments,
    fetch_hyperliquid_quotes,
    fetch_okx_candles,
    fetch_okx_instruments,
    fetch_okx_quotes,
    fetch_us_reference_instruments,
)
from tracefold.news.market_review.loops import EventReactionLoop, MarketReviewDatabasePort, QuoteSnapshotLoop
from tracefold.news.pipeline.maintenance import InstrumentSnapshotLoop
from tracefold.news.pipeline.runtime import NewsDatabasePort
from tracefold.platform.observability import TelemetryRegistry


def _price_venue_enabled(settings: Any, source_key: str) -> bool:
    """#88 reuses the existing venue switches; the price plane never gets an operator knob of its own."""

    venues = settings.news.venues
    if not venues.enabled:
        return False
    if source_key.startswith("binance."):
        return bool(venues.binance)
    if source_key.startswith("hl."):
        return bool(venues.hyperliquid)
    if source_key.startswith("okx."):
        return bool(venues.okx)
    return False


def _quote_snapshot_loop(
    settings: Any,
    *,
    db: MarketReviewDatabasePort,
    watchlist: Sequence[str],
    telemetry: TelemetryRegistry | None = None,
) -> QuoteSnapshotLoop | None:
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
        if source_key.startswith("okx."):
            return functools.partial(fetch_okx_quotes, venue=source_key)
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
    if not venues.enabled or not (venues.binance or venues.hyperliquid or venues.okx):
        return None
    return QuoteSnapshotLoop(
        db=db,
        fetcher_for=fetcher_for,
        day_fetcher_for=day_fetcher_for,
        watchlist=watchlist,
        telemetry=telemetry,
    )


def _event_reaction_loop(
    settings: Any,
    *,
    db: MarketReviewDatabasePort,
    telemetry: TelemetryRegistry | None = None,
) -> EventReactionLoop | None:
    venues = settings.news.venues
    if not venues.enabled or not (venues.binance or venues.hyperliquid or venues.okx):
        return None
    return EventReactionLoop(
        db=db,
        fetcher_for=functools.partial(_candle_fetcher_for, settings, interval="5m"),
        telemetry=telemetry,
    )


def _candle_fetcher_for(settings: Any, venue: str, *, interval: str) -> Any | None:
    if not _price_venue_enabled(settings, venue):
        return None
    if venue.startswith("binance."):

        async def binance(venue_symbol: str, start_ms: int, end_ms: int) -> Any:
            return await fetch_binance_candles(
                venue_symbol,
                venue=venue,
                start_ms=start_ms,
                end_ms=end_ms,
                interval=interval,
            )

        return binance
    if venue.startswith("hl."):

        async def hyperliquid(venue_symbol: str, start_ms: int, end_ms: int) -> Any:
            return await fetch_hyperliquid_candles(
                venue_symbol,
                venue=venue,
                start_ms=start_ms,
                end_ms=end_ms,
                interval=interval,
            )

        return hyperliquid
    if venue.startswith("okx."):

        async def okx(venue_symbol: str, start_ms: int, end_ms: int) -> Any:
            return await fetch_okx_candles(
                venue_symbol,
                venue=venue,
                start_ms=start_ms,
                end_ms=end_ms,
                interval=interval,
            )

        return okx
    return None


def _delivery_price_fetcher_for(settings: Any, venue: str) -> Any | None:
    if not _price_venue_enabled(settings, venue):
        return None
    if not venue.startswith(("binance.", "hl.", "okx.")):
        return None

    async def fetch(venue_symbol: str, targets_ms: Sequence[int]) -> Any:
        return await fetch_delivery_price_points(venue_symbol, venue=venue, targets_ms=targets_ms)

    return fetch


def _instrument_snapshot_loop(
    settings: Any,
    *,
    db: NewsDatabasePort,
    telemetry: TelemetryRegistry | None = None,
) -> InstrumentSnapshotLoop | None:
    """#75: one fetcher per venue family, each independently skippable. No credentials are involved."""

    venues = settings.news.venues
    if not venues.enabled:
        return None
    fetchers: list[tuple[str, Callable[[], Any]]] = []
    if venues.binance:
        fetchers.append(("binance", fetch_binance_instruments))
    if venues.hyperliquid:
        fetchers.append(("hyperliquid", fetch_hyperliquid_instruments))
    if venues.okx:
        fetchers.append(("okx", fetch_okx_instruments))
    if venues.us_reference:
        fetchers.append(("us_reference", fetch_us_reference_instruments))
    if not fetchers:
        return None
    return InstrumentSnapshotLoop(
        db=db,
        fetchers=fetchers,
        period_seconds=float(venues.snapshot_period_hours) * 3600.0,
        telemetry=telemetry,
    )
