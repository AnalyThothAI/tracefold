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
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.app.workers.wiring.news_to_trading import news_oi_sources
from tracefold.integrations.venues import fetch_binance_candles, fetch_hyperliquid_candles
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.resource import ResourceAdmissionTimeout
from tracefold.trading import OiTradeCandidate
from tracefold.trading.contracts import Bar as TradingBar
from tracefold.trading.contracts import OiCandidateRow
from tracefold.trading.signal_lane import SignalLane
from tracefold.trading.sources import source_venue

SIGNAL_LANE_TASK_NAME = "trading-signal-lane"
# The lane moves at the speed of a five-minute OI frame; two seconds is what makes a fresh frame reach
# a Case well inside its own trigger budget without the scan becoming a busy loop.
SIGNAL_LANE_POLL_SECONDS = 2.0
# The longest a refused turn waits before the next one. Doubling from the poll interval reaches it in
# four refusals; a database that is merely busy is retried within seconds, and one that is down is not
# asked every two seconds.
SIGNAL_LANE_BACKOFF_MAX_SECONDS = 30.0
_SIGNAL_PROJECTION_TIMEOUT_SECONDS = 10.0


def _wire_signal_lane(
    *,
    settings: Settings,
    db: WorkerDatabase,
    telemetry: TelemetryRegistry | None = None,
) -> SignalLane | None:
    """#104/#331. Disabled by default; a disabled Trading context constructs nothing.

    Both of the lane's database paths -- its own Trading statements and its one News read -- take an
    ordinary business permit, never one of the four News lane slots, for the same reason #88 gave: a
    trading backlog must not compete with the Deduper, Triage and the Deliverer for the lane they were
    budgeted. They used to share the one-slot heavy admission with the Janitor and Event Reaction,
    which is what turned every long retention sweep into a refused lane turn (#680 RC7).

    The News read goes straight to the business lane in the platform error vocabulary, where it used
    to borrow the Janitor's adapter and come back as a News `DeferError` the lane had no word for.
    """

    if not settings.trading.enabled:
        return None

    async def read_news_oi_projection(
        metric_version: str,
        after_created_at_ms: int,
        until_created_at_ms: int,
    ) -> Sequence[OiCandidateRow]:
        return await db.run_business(
            "trading_oi_projection",
            _read_news_oi_projection,
            db,
            metric_version,
            after_created_at_ms,
            until_created_at_ms,
            operation_timeout_seconds=_SIGNAL_PROJECTION_TIMEOUT_SECONDS,
        )

    return SignalLane(
        db=WorkerTradingDatabase(db),
        config=signal_lane_config(settings),
        bars=_source_native_bars,
        oi_projection=read_news_oi_projection,
        telemetry=telemetry,
    )


def _read_news_oi_projection(
    db: WorkerDatabase,
    metric_version: str,
    after_created_at_ms: int,
    until_created_at_ms: int,
) -> Sequence[OiCandidateRow]:
    with db.worker_session("trading_oi_projection", _SIGNAL_PROJECTION_TIMEOUT_SECONDS) as repos:
        return news_oi_sources(repos, metric_version, after_created_at_ms, until_created_at_ms)


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


def refusal_backoff_seconds(refusals: int, *, poll_seconds: float = SIGNAL_LANE_POLL_SECONDS) -> float:
    """The wait after the `refusals`-th refused turn in a row: doubling from the poll, capped."""

    doubled = max(0.05, float(poll_seconds)) * float(2 ** max(1, int(refusals)))
    return min(SIGNAL_LANE_BACKOFF_MAX_SECONDS, doubled)


async def run_signal_lane(
    lane: SignalLane,
    *,
    stop_event: asyncio.Event,
    telemetry: Any | None = None,
    poll_seconds: float = SIGNAL_LANE_POLL_SECONDS,
) -> None:
    """Poll `advance()` until the process stops. The lane owns no clock of its own.

    A turn the database refused -- an admission, lock, statement, transaction or connection timeout,
    which `WorkerDatabase` names `ResourceAdmissionTimeout` -- ends that turn and nothing else. It is
    logged, counted as an errored turn, and the next one runs after a doubling backoff capped at
    `SIGNAL_LANE_BACKOFF_MAX_SECONDS`. Every business refusal is a durable row and every turn re-reads
    the ledger it writes, so a skipped turn loses no work. Until #680 this raised, the Workers root
    marked the capability `faulted`, and nothing restarted it: 33 of 35 production faults were one
    such timeout, and the longest outage lasted 31 hours.

    Anything else out of `advance()` is a program error, and it is raised, never swallowed: the
    Workers root records `trading_signal_lane` as `faulted` with this failure and stops the task, and
    News reception, fact writes and reads carry on beside it (#553 PR-3). `ResourceOperationOverrun`
    is not a refused turn -- a native operation outlived its envelope and still holds its thread -- so
    it still reaches the root, which treats it as the foundation failure it is.
    """

    refusals = 0
    while not stop_event.is_set():
        started = time.perf_counter()
        try:
            turn = await lane.advance()
        except ResourceAdmissionTimeout as exc:
            refusals += 1
            if telemetry is not None:
                telemetry.record_external_data_turn(
                    "trading_signal_lane",
                    "error",
                    time.perf_counter() - started,
                )
            delay = refusal_backoff_seconds(refusals, poll_seconds=poll_seconds)
            logger.warning(
                "signal lane turn refused by the database; retrying error={} refusals={} retry_in_seconds={}",
                exc,
                refusals,
                delay,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            continue
        except Exception:
            logger.exception("signal lane turn failed")
            if telemetry is not None:
                telemetry.record_external_data_turn(
                    "trading_signal_lane",
                    "error",
                    time.perf_counter() - started,
                )
            raise
        refusals = 0
        if telemetry is not None:
            # What the turn read and what it made of it. The port and both gauges have carried these
            # two counts since #331; this loop passed neither, so the lane's source and target volume
            # was the one external-data runner an operator could not see (#604 T2).
            telemetry.record_external_data_turn(
                "trading_signal_lane",
                "success",
                time.perf_counter() - started,
                source_count=turn.sources,
                target_count=turn.cases_created,
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(poll_seconds)))


__all__ = [
    "SIGNAL_LANE_BACKOFF_MAX_SECONDS",
    "SIGNAL_LANE_POLL_SECONDS",
    "SIGNAL_LANE_TASK_NAME",
    "refusal_backoff_seconds",
    "run_signal_lane",
]
