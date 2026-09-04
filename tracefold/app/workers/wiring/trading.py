"""Compose the one engine-neutral Signal lane.

The lane exposes exactly one business action, `advance()`. Polling, the stop event and the process
lifecycle are App's (#331), which is why the loop lives here rather than inside the business package:
a bounded context that owns its own scheduler is a service, and this system has one worker process.

A disabled Decision Plane constructs no lane, adapter, or execution client.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence
from typing import Any

from loguru import logger

from tracefold.app.trading_config import signal_lane_config
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.wiring.database import WorkerNewsColdDatabase, WorkerTradingDatabase
from tracefold.app.workers.wiring.news_to_trading import news_oi_sources
from tracefold.integrations.venues import fetch_binance_candles, fetch_hyperliquid_candles
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading import OiTradeCandidate
from tracefold.trading.contracts import Bar as TradingBar
from tracefold.trading.contracts import OiCandidateRow
from tracefold.trading.signal_lane import SignalLane
from tracefold.trading.sources import source_venue

SIGNAL_LANE_TASK_NAME = "trading-signal-lane"
# The lane moves at the speed of a five-minute OI frame; two seconds is what makes a fresh frame reach
# a Case well inside its own trigger budget without the scan becoming a busy loop.
SIGNAL_LANE_POLL_SECONDS = 2.0
_SIGNAL_PROJECTION_TIMEOUT_SECONDS = 10.0


def _wire_signal_lane(
    *,
    settings: Settings,
    db: WorkerDatabase,
    telemetry: TelemetryRegistry | None = None,
) -> SignalLane | None:
    """#104/#331. Disabled by default; a disabled Trading context constructs nothing.

    The lane shares Event Reaction's one-slot heavy admission rather than the four News lane slots, for
    the same reason #88 gave: a trading backlog must not compete with the Deduper, Triage and the
    Deliverer for the lane they were budgeted.
    """

    if not settings.trading.enabled:
        return None

    news_db = WorkerNewsColdDatabase(db)

    async def read_news_oi_projection(
        metric_version: str,
        after_created_at_ms: int,
        until_created_at_ms: int,
    ) -> Sequence[OiCandidateRow]:
        return await news_db.read(
            "trading_oi_projection",
            lambda repos: news_oi_sources(
                repos,
                metric_version,
                after_created_at_ms,
                until_created_at_ms,
            ),
            timeout_seconds=_SIGNAL_PROJECTION_TIMEOUT_SECONDS,
        )

    return SignalLane(
        db=WorkerTradingDatabase(db),
        config=signal_lane_config(settings),
        bars=_source_native_bars,
        oi_projection=read_news_oi_projection,
        telemetry=telemetry,
    )


async def _source_native_candles(
    source_venue_key: str, base_symbol: str, start_ms: int, end_ms: int
) -> Sequence[TradingBar]:
    """Public bars from a Source's own venue, in that venue's own spelling of the market.

    Two vocabularies meet here and only here. A Source carries the provider's venue key
    (`binance.usdm`, `hyperliquid.perp`, `hyperliquid.xyz`) and `integrations.venues` answers to the
    price-plane key (`binance.perp`, `hl.perp`, `hl.xyz`), and the symbol is spelled differently on
    each side too — `SOLUSDT` against `SOL`, `xyz:AAPL` against a bare ticker on the builder DEX. Both
    translations are fields on `trading.sources.SourceVenue`, so this function chooses a client and
    nothing else; the ladder of `if`s that used to spell them here was one of four copies of the same
    table (#537 PR-3).

    This is evidence, never an execution route: nothing here chooses where an order would go. The
    return type is Trading's own `Bar` rather than the venue package's `Candle`, because a `Candle` is
    a News type and this seam belongs to neither capability's tables.
    """

    venue = source_venue(source_venue_key)
    if venue is None:
        raise RuntimeError("trading_source_venue_unresolved")
    symbol = venue.price_symbol(base_symbol)
    if venue.telemetry_source == "binance":
        candles = await fetch_binance_candles(symbol, venue=venue.price_venue, start_ms=start_ms, end_ms=end_ms)
    else:
        candles = await fetch_hyperliquid_candles(symbol, venue=venue.price_venue, start_ms=start_ms, end_ms=end_ms)
    return tuple(TradingBar(open_at_ms=c.open_at_ms, close_at_ms=c.close_at_ms, close=c.close) for c in candles)


async def _source_native_bars(candidate: OiTradeCandidate, start_ms: int, end_ms: int) -> Sequence[TradingBar]:
    """The pre-move read: the Case's own bars, on the venue its Source was observed on."""

    return await _source_native_candles(candidate.venue, candidate.base_symbol, start_ms, end_ms)


async def run_signal_lane(
    lane: SignalLane,
    *,
    stop_event: asyncio.Event,
    telemetry: Any | None = None,
    poll_seconds: float = SIGNAL_LANE_POLL_SECONDS,
) -> None:
    """Poll `advance()` until the process stops. The lane owns no clock of its own.

    An exception out of `advance()` is an infrastructure fault by construction — every business
    refusal is a durable row — so it terminates this business task and makes the Workers root fail
    closed. Retrying here would leave process readiness green beside a FAULTED Decision Plane.
    """

    while not stop_event.is_set():
        started = time.perf_counter()
        outcome = "error"
        try:
            await lane.advance()
        except Exception:
            logger.exception("signal lane turn failed")
            if telemetry is not None:
                telemetry.record_external_data_turn(
                    "trading_signal_lane",
                    outcome,
                    time.perf_counter() - started,
                )
            raise
        if telemetry is not None:
            telemetry.record_external_data_turn(
                "trading_signal_lane",
                "success",
                time.perf_counter() - started,
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(poll_seconds)))


__all__ = [
    "SIGNAL_LANE_POLL_SECONDS",
    "SIGNAL_LANE_TASK_NAME",
    "run_signal_lane",
]
