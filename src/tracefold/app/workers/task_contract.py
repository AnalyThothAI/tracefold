"""Stable, ordered task declarations for the sole Workers process root."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from tracefold.news.pipeline.root import NewsPipeline
from tracefold.trading.pipeline.root import TradingPipeline

if TYPE_CHECKING:
    from tracefold.app.workers.wiring.manual_trading import ManualTradingRunner

WORKERS_PROBE_TASK_NAME = "workers-probe"
WORKERS_CONTROL_TASK_NAME = "workers-control"

# One task: its stable runtime name, and a callable that runs it until the stop event is set.
WorkerRunner = tuple[str, Callable[[asyncio.Event], Awaitable[None]]]


def worker_business_runners(
    *,
    news_pipeline: NewsPipeline | None,
    trading_pipeline: TradingPipeline | None,
    manual_trading_runner: ManualTradingRunner | None = None,
) -> tuple[WorkerRunner, ...]:
    """Return the ordered task declarations consumed by the Workers root."""

    runners: list[WorkerRunner] = []
    if news_pipeline is not None:
        runners.extend(news_pipeline.runners())
    if trading_pipeline is not None:
        runners.extend(trading_pipeline.runners())
    if manual_trading_runner is not None:
        runners.append(("trading-telegram", manual_trading_runner.run))
    return tuple(runners)
