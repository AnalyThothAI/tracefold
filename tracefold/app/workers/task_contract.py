"""Stable, ordered task declarations for the sole Workers process root."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from tracefold.app.workers.wiring.trading import (
    SIGNAL_LANE_TASK_NAME,
    run_signal_lane,
)
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.trading.signal_lane import SignalLane

WORKERS_PROBE_TASK_NAME = "workers-probe"
WORKERS_CONTROL_TASK_NAME = "workers-control"

# One task: its stable runtime name, and a callable that runs it until the stop event is set.
WorkerRunner = tuple[str, Callable[[asyncio.Event], Awaitable[None]]]


def worker_business_runners(
    *,
    news_pipeline: NewsPipeline | None,
    signal_lane: SignalLane | None,
    telemetry: Any | None = None,
) -> tuple[WorkerRunner, ...]:
    """Return the ordered task declarations consumed by the Workers root.

    The Signal lane's loop is declared here rather than inside `tracefold.trading`: the lane
    exposes one business action and App owns polling, the stop event and the process lifecycle.
    """

    runners: list[WorkerRunner] = []
    if news_pipeline is not None:
        runners.extend(news_pipeline.runners())
    if signal_lane is not None:
        lane = signal_lane
        runners.append(
            (
                SIGNAL_LANE_TASK_NAME,
                lambda stop: run_signal_lane(lane, stop_event=stop, telemetry=telemetry),
            )
        )
    return tuple(runners)
