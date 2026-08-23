"""Stable, ordered task declarations for the sole Workers process root."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from tracefold.news.pipeline.root import NewsPipeline
from tracefold.trading.pipeline.root import TradingPipeline

WORKERS_PROBE_TASK_NAME = "workers-probe"
WORKERS_CONTROL_TASK_NAME = "workers-control"

WorkerRunner = tuple[str, Callable[[asyncio.Event], Any]]


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


def worker_task_names(
    *,
    news_pipeline: NewsPipeline | None,
    trading_pipeline: TradingPipeline | None,
) -> tuple[str, ...]:
    """Stable runtime task names, including the two Workers-root tasks."""

    business = worker_business_runners(
        news_pipeline=news_pipeline,
        trading_pipeline=trading_pipeline,
    )
    return (WORKERS_PROBE_TASK_NAME, *(name for name, _runner in business), WORKERS_CONTROL_TASK_NAME)
