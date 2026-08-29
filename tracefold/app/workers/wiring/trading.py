"""Compose the one capital lane, and own its poll loop.

The lane exposes exactly one business action, `advance()`. Polling, the stop event and the process
lifecycle are App's (#331), which is why the loop lives here rather than inside the business package:
a bounded context that owns its own scheduler is a service, and this system has one worker process.

A disabled Decision Plane constructs no lane, adapter, or execution client. The credential-free public
catalog remains a Workers responsibility because `trading.enabled` controls only Decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence
from typing import Any, Literal

from loguru import logger

from tracefold.app.learning_runtime import active_arm_manifest
from tracefold.app.trading_config import capital_lane_config
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.app.workers.wiring.news_to_trading import news_oi_sources
from tracefold.integrations.trading_catalog import (
    VenueExpectedError,
    fetch_binance_usdm_catalog,
    fetch_hyperliquid_perp_catalog,
)
from tracefold.integrations.venues import fetch_binance_candles, fetch_hyperliquid_candles
from tracefold.news.learning.contracts import epoch_id_for_bundle
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading import InstrumentRef, VenueBinding
from tracefold.trading.capital_lane import CapitalLane
from tracefold.trading.catalog import VenueCatalog
from tracefold.trading.contracts import Bar as TradingBar

from .execution_capabilities import ExecutionCapabilityCompileError, ExecutionCapabilityCompiler

CAPITAL_LANE_TASK_NAME = "trading-capital-lane"
VENUE_CATALOG_TASK_NAME = "trading-venue-catalog"
# The lane moves at the speed of a five-minute OI frame; two seconds is what makes a fresh frame reach
# a Case well inside its own trigger budget without the scan becoming a busy loop.
CAPITAL_LANE_POLL_SECONDS = 2.0
VENUE_CATALOG_PERIOD_SECONDS = 6 * 3_600.0
VENUE_CATALOG_RETRY_SECONDS = 15 * 60.0


def _wire_venue_catalog(*, db: WorkerDatabase, telemetry: TelemetryRegistry | None = None) -> VenueCatalog:
    return VenueCatalog(
        db=WorkerTradingDatabase(db),
        clock=lambda: int(time.time() * 1_000),
        stale_after_ms=int(VENUE_CATALOG_PERIOD_SECONDS * 1_000),
        telemetry=telemetry,
    )


def _wire_execution_capability_compiler(*, db: WorkerDatabase) -> ExecutionCapabilityCompiler:
    return ExecutionCapabilityCompiler(WorkerTradingDatabase(db))


def _wire_capital_lane(
    *,
    settings: Settings,
    db: WorkerDatabase,
    telemetry: TelemetryRegistry | None = None,
) -> CapitalLane | None:
    """#104/#331. Disabled by default; a disabled Trading context constructs nothing.

    The lane shares Event Reaction's one-slot heavy admission rather than the four News lane slots, for
    the same reason #88 gave: a trading backlog must not compete with the Deduper, Triage and the
    Deliverer for the lane they were budgeted.
    """

    if not settings.trading.enabled:
        return None
    return CapitalLane(
        db=WorkerTradingDatabase(db),
        config=capital_lane_config(settings),
        bars=_source_native_bars,
        oi_projection=news_oi_sources,
        # The one place that may tell Trading which News generation is running (#314). Trading
        # holds no News literal and reads no News table; this seam derives the label from the same
        # stable arm the News workers appoint, so the two cannot drift.
        news_generation=epoch_id_for_bundle(active_arm_manifest(settings).bundle_sha),
        telemetry=telemetry,
    )


async def _source_native_bars(instrument: InstrumentRef, start_ms: int, end_ms: int) -> Sequence[TradingBar]:
    """Fetch only the exact venue frozen by the source-native Case; never reroute or fall back."""

    if instrument.binding == "BINANCE_USDM":
        candles = await fetch_binance_candles(
            instrument.provider_symbol,
            venue="binance.perp",
            start_ms=start_ms,
            end_ms=end_ms,
        )
    elif instrument.binding == "HYPERLIQUID_PERP":
        candles = await fetch_hyperliquid_candles(
            instrument.provider_symbol,
            venue="hl.perp",
            start_ms=start_ms,
            end_ms=end_ms,
        )
    else:  # pragma: no cover - InstrumentRef carries the closed union
        raise RuntimeError("trading_source_binding_unresolved")
    return tuple(TradingBar(open_at_ms=c.open_at_ms, close_at_ms=c.close_at_ms, close=c.close) for c in candles)


async def run_venue_catalog(
    catalog: VenueCatalog,
    *,
    capability_compiler: ExecutionCapabilityCompiler | None = None,
    stop_event: asyncio.Event,
    period_seconds: float = VENUE_CATALOG_PERIOD_SECONDS,
) -> None:
    """Refresh both public bindings independently; provider errors retain each binding's last-good."""

    fetchers: tuple[tuple[VenueBinding, Any], ...] = (
        ("BINANCE_USDM", fetch_binance_usdm_catalog),
        ("HYPERLIQUID_PERP", fetch_hyperliquid_perp_catalog),
    )
    while not stop_event.is_set():
        started = time.perf_counter()
        complete = True
        source_count = 0
        target_count = 0
        try:
            for binding, fetch in fetchers:
                source_count += 1
                source: Literal["binance", "hyperliquid"] = "binance" if binding == "BINANCE_USDM" else "hyperliquid"
                try:
                    instruments = await catalog.observe_provider(source=source, call=fetch())
                except VenueExpectedError as exc:
                    complete = False
                    await catalog.unavailable(binding=binding, reason=exc.code)
                else:
                    snapshot = await catalog.publish(binding=binding, instruments=instruments)
                    if capability_compiler is not None:
                        try:
                            await capability_compiler.compile(snapshot)
                        except ExecutionCapabilityCompileError as exc:
                            complete = False
                            logger.warning(
                                "Execution capability compile failed for binding={}: {}",
                                binding,
                                exc,
                            )
                    target_count += len(instruments)
        except BaseException:
            catalog.record_turn(
                "error",
                time.perf_counter() - started,
                source_count=source_count,
                target_count=target_count,
            )
            raise
        catalog.record_turn(
            "success" if complete else "partial",
            time.perf_counter() - started,
            source_count=source_count,
            target_count=target_count,
        )
        wait = period_seconds if complete else min(period_seconds, VENUE_CATALOG_RETRY_SECONDS)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(wait)))


async def run_capital_lane(
    lane: CapitalLane,
    *,
    stop_event: asyncio.Event,
    telemetry: Any | None = None,
    poll_seconds: float = CAPITAL_LANE_POLL_SECONDS,
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
            logger.exception("capital lane turn failed")
            if telemetry is not None:
                telemetry.record_external_data_turn(
                    "trading_capital_lane",
                    outcome,
                    time.perf_counter() - started,
                )
            raise
        if telemetry is not None:
            telemetry.record_external_data_turn(
                "trading_capital_lane",
                "success",
                time.perf_counter() - started,
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(poll_seconds)))


__all__ = [
    "CAPITAL_LANE_POLL_SECONDS",
    "CAPITAL_LANE_TASK_NAME",
    "VENUE_CATALOG_TASK_NAME",
    "run_capital_lane",
    "run_venue_catalog",
]
