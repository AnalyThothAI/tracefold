"""Stable, ordered task declarations for the sole Workers process root.

Every declaration here is a *capability* task: it belongs to one named business capability, and an
unexpected program error inside it stops that task and marks that capability `faulted` instead of
killing the process (#553 PR-3). The shared foundation -- PostgreSQL, the schema, the singleton
process ownership, the probe and the control heartbeat -- is not declared here and keeps its root
fatal, because a process that has lost those cannot honestly serve anything.

A new optional loop joins by returning one more `WorkerTask` from `worker_business_tasks` with its
own capability name; nothing else has to change, and it inherits the confinement above. #553 PR-2's
market notification loop is exactly that: one task, one capability, one `advance()`-shaped runner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tracefold.app.workers.runtime import (
    NEWS_DELIVERY,
    NEWS_EDITORIAL,
    NEWS_INGESTION,
    NEWS_MARKET_REVIEW,
    TRADING_SIGNAL_LANE,
)
from tracefold.app.workers.wiring.trading import (
    SIGNAL_LANE_TASK_NAME,
    run_signal_lane,
)
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.trading.signal_lane import SignalLane

WORKERS_PROBE_TASK_NAME = "workers-probe"
WORKERS_CONTROL_TASK_NAME = "workers-control"

# News names its own loops; App decides which capability each one answers for, because the capability
# vocabulary is what `/api/status` publishes and News must not own a Trading-visible surface. An
# unknown task name is a composition bug and fails closed rather than reporting under a guessed name.
_NEWS_TASK_CAPABILITIES = {
    "news-receiver": NEWS_INGESTION,
    "news-recovery": NEWS_INGESTION,
    "news-deduper": NEWS_INGESTION,
    "news-janitor": NEWS_INGESTION,
    "news-triage": NEWS_EDITORIAL,
    "news-deliverer": NEWS_DELIVERY,
    "news-instruments": NEWS_MARKET_REVIEW,
    "news-quotes": NEWS_MARKET_REVIEW,
    "news-reactions": NEWS_MARKET_REVIEW,
}


@dataclass(frozen=True, slots=True)
class WorkerTask:
    """One business task: its stable runtime name, the capability it answers for, and its runner."""

    name: str
    capability: str
    run: Callable[[asyncio.Event], Awaitable[None]]


def worker_business_tasks(
    *,
    news_pipeline: NewsPipeline | None,
    signal_lane: SignalLane | None,
    telemetry: Any | None = None,
) -> tuple[WorkerTask, ...]:
    """Return the ordered task declarations consumed by the Workers root.

    The Signal lane's loop is declared here rather than inside `tracefold.trading`: the lane
    exposes one business action and App owns polling, the stop event and the process lifecycle.
    """

    tasks: list[WorkerTask] = []
    if news_pipeline is not None:
        for task_name, runner in news_pipeline.runners():
            tasks.append(
                WorkerTask(
                    name=task_name,
                    capability=_NEWS_TASK_CAPABILITIES[task_name],
                    run=runner,
                )
            )
    if signal_lane is not None:
        lane = signal_lane
        tasks.append(
            WorkerTask(
                name=SIGNAL_LANE_TASK_NAME,
                capability=TRADING_SIGNAL_LANE,
                run=lambda stop: run_signal_lane(lane, stop_event=stop, telemetry=telemetry),
            )
        )
    return tuple(tasks)


__all__ = [
    "WORKERS_CONTROL_TASK_NAME",
    "WORKERS_PROBE_TASK_NAME",
    "WorkerTask",
    "worker_business_tasks",
]
