"""Compose the one capital lane, and own its poll loop.

The lane exposes exactly one business action, `advance()`. Polling, the stop event and the process
lifecycle are App's (#331), which is why the loop lives here rather than inside the business package:
a bounded context that owns its own scheduler is a service, and this system has one worker process.

A disabled Trading context constructs nothing at all — no lane, no adapter, no client.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence
from typing import Any

from loguru import logger

from tracefold.app.learning_runtime import active_arm_manifest
from tracefold.app.trading_config import capital_lane_config
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.app.workers.wiring.news_to_trading import news_oi_sources
from tracefold.integrations.venues import fetch_binance_candles
from tracefold.news.learning.contracts import epoch_id_for_bundle
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.trading.capital_lane import CapitalLane
from tracefold.trading.contracts import LIVE_VENUE
from tracefold.trading.contracts import Bar as TradingBar

CAPITAL_LANE_TASK_NAME = "trading-capital-lane"
# The lane moves at the speed of a five-minute OI frame; two seconds is what makes a fresh frame reach
# a Case well inside its own trigger budget without the scan becoming a busy loop.
CAPITAL_LANE_POLL_SECONDS = 2.0


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
    try:
        return CapitalLane(
            db=WorkerTradingDatabase(db),
            config=capital_lane_config(settings),
            bars=_binance_bars,
            oi_projection=news_oi_sources,
            # The one place that may tell Trading which News generation is running (#314). Trading
            # holds no News literal and reads no News table; this seam derives the label from the same
            # stable arm the News workers appoint, so the two cannot drift.
            news_generation=epoch_id_for_bundle(active_arm_manifest(settings).bundle_sha),
            telemetry=telemetry,
        )
    except Exception:
        logger.exception("capital lane wiring failed; Trading stays disabled for this process")
        return None


async def _binance_bars(provider_symbol: str, start_ms: int, end_ms: int) -> Sequence[TradingBar]:
    """Closed Binance USD-M perp candles. One venue, because one venue carries live capital.

    There is deliberately no `exchange_id` parameter and no factory. A live Hyperliquid bar fetch was
    reachable from the old wiring purely because the same factory served both venues, and a research
    venue that can be priced by the live lane is one refactor away from being traded by it.
    """

    candles = await fetch_binance_candles(provider_symbol, venue=LIVE_VENUE, start_ms=start_ms, end_ms=end_ms)
    return tuple(TradingBar(open_at_ms=c.open_at_ms, close_at_ms=c.close_at_ms, close=c.close) for c in candles)


async def run_capital_lane(
    lane: CapitalLane,
    *,
    stop_event: asyncio.Event,
    telemetry: Any | None = None,
    poll_seconds: float = CAPITAL_LANE_POLL_SECONDS,
) -> None:
    """Poll `advance()` until the process stops. The lane owns no clock of its own.

    An exception out of `advance()` is an infrastructure fault by construction — every business refusal
    is a durable row — so it is logged and retried on the next tick rather than crashing the worker.
    """

    while not stop_event.is_set():
        started = time.perf_counter()
        outcome = "error"
        try:
            turn = await lane.advance()
        except Exception:
            logger.exception("capital lane turn failed")
        else:
            outcome = "error" if turn.outcome == "HALTED" and turn.reason == "runtime_state_missing" else "success"
        if telemetry is not None:
            telemetry.record_external_data_turn(
                "trading_capital_lane",
                outcome,
                time.perf_counter() - started,
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(poll_seconds)))


__all__ = [
    "CAPITAL_LANE_POLL_SECONDS",
    "CAPITAL_LANE_TASK_NAME",
    "run_capital_lane",
]
