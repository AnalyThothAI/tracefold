"""Stable, ordered task declarations for the sole Workers process root."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from tracefold.news.pipeline.root import NewsPipeline
from tracefold.trading.pipeline.root import TradingPipeline

WORKERS_PROBE_TASK_NAME = "workers-probe"
WORKERS_CONTROL_TASK_NAME = "workers-control"

# One task: its stable runtime name, and a callable that runs it until the stop event is set.
WorkerRunner = tuple[str, Callable[[asyncio.Event], Awaitable[None]]]


def worker_business_runners(
    *,
    news_pipeline: NewsPipeline | None,
    trading_pipeline: TradingPipeline | None,
) -> tuple[WorkerRunner, ...]:
    """Return the ordered task declarations consumed by the Workers root."""

    runners: list[WorkerRunner] = []
    if news_pipeline is not None:
        runners.extend(news_pipeline.runners())
    if trading_pipeline is not None:
        runners.extend(trading_pipeline.runners())
    return tuple(runners)
