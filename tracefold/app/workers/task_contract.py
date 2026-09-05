"""Stable, ordered task declarations for the sole Workers process root.

Each declaration names the capability it answers for and whether it is *foundational*.

A foundational task is part of the information entry itself. News reception, admission and the
retention that keeps them writable are not capabilities an operator can be asked to live without:
they are what every other capability reads. An unexpected program error there stays root fatal,
exactly like PostgreSQL, the schema and the singleton process ownership, so the container restart
that has always healed a receiver crash still happens rather than being replaced by a permanent
ingestion outage behind a green readiness (#553 PR-3).

Every other task is a capability task: an unexpected program error stops that task and marks its
capability `faulted`, and the healthy tasks beside it carry on. One optional task owns one capability
key, so a fault always names exactly what stopped.

A new optional loop joins by returning one more `WorkerTask` from `worker_business_tasks` with its
own capability name; nothing else has to change. #553 PR-2's market notification loop is exactly
that: one task, one capability, one `advance()`-shaped runner. It is declared here beside the Signal
lane rather than through `NewsPipeline.runners()` because App owns its polling for the same reason it
owns the lane's: the loop exposes one business action, `advance()`, and the tick, the stop event and
the process lifecycle are the root's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tracefold.app.workers.runtime import (
    CHAIN_TAPE,
    MARKET_NOTIFICATIONS,
    NEWS_DELIVERY,
    NEWS_EDITORIAL,
    NEWS_INGESTION,
    NEWS_INSTRUMENTS,
    NEWS_QUOTES,
    NEWS_REACTIONS,
    TRADING_SIGNAL_LANE,
)
from tracefold.app.workers.wiring.chain_tape import (
    CHAIN_TAPE_TASK_NAME,
    run_chain_tape,
)
from tracefold.app.workers.wiring.news import (
    MARKET_NOTIFICATIONS_TASK_NAME,
    run_market_notifications,
)
from tracefold.app.workers.wiring.trading import (
    SIGNAL_LANE_TASK_NAME,
    run_signal_lane,
)
from tracefold.news.chain_tape import ChainTapeLoop
from tracefold.news.market_notifications import MarketNotificationLoop
from tracefold.news.pipeline.root import NewsPipeline
from tracefold.trading.signal_lane import SignalLane

WORKERS_PROBE_TASK_NAME = "workers-probe"
WORKERS_CONTROL_TASK_NAME = "workers-control"

# News names its own loops; App decides which capability each one answers for and whether it is
# foundational, because the capability vocabulary is what `/api/status` publishes and News must not
# own a Trading-visible surface. An unknown task name is a composition bug and fails closed rather
# than reporting under a guessed name.
_NEWS_TASK_DECLARATIONS: dict[str, tuple[str, bool]] = {
    # The information entry. Reception, admission and the retention that keeps them writable stay
    # root fatal: they are the thing every other capability reads.
    "news-receiver": (NEWS_INGESTION, True),
    "news-recovery": (NEWS_INGESTION, True),
    "news-deduper": (NEWS_INGESTION, True),
    "news-janitor": (NEWS_INGESTION, True),
    # Optional capabilities, one task each, so a fault names exactly what stopped.
    "news-triage": (NEWS_EDITORIAL, False),
    "news-deliverer": (NEWS_DELIVERY, False),
    "news-instruments": (NEWS_INSTRUMENTS, False),
    "news-quotes": (NEWS_QUOTES, False),
    "news-reactions": (NEWS_REACTIONS, False),
}


@dataclass(frozen=True, slots=True)
class WorkerTask:
    """One business task: its runtime name, the capability it answers for, its runner, and its fate.

    `foundational` decides what an unexpected program error inside it means: a root fatal, or one
    faulted capability beside healthy ones.
    """

    name: str
    capability: str
    run: Callable[[asyncio.Event], Awaitable[None]]
    foundational: bool


def worker_business_tasks(
    *,
    news_pipeline: NewsPipeline | None,
    signal_lane: SignalLane | None,
    market_notifications: MarketNotificationLoop | None = None,
    chain_tape: ChainTapeLoop | None = None,
    telemetry: Any | None = None,
) -> tuple[WorkerTask, ...]:
    """Return the ordered task declarations consumed by the Workers root.

    The Signal lane's loop is declared here rather than inside `tracefold.trading`: the lane
    exposes one business action and App owns polling, the stop event and the process lifecycle.

    These declarations do not set capability states. Composition already did, and it knows more than
    a task list can: a Deliverer task runs whether or not a sender could be built, so "a task exists"
    is not "the capability works", and letting this loop declare `running` would quietly overwrite
    the `unavailable` that composition recorded.
    """

    tasks: list[WorkerTask] = []
    if news_pipeline is not None:
        for task_name, runner in news_pipeline.runners():
            capability, foundational = _NEWS_TASK_DECLARATIONS[task_name]
            tasks.append(
                WorkerTask(
                    name=task_name,
                    capability=capability,
                    run=runner,
                    foundational=foundational,
                )
            )
    if market_notifications is not None:
        market = market_notifications
        tasks.append(
            WorkerTask(
                name=MARKET_NOTIFICATIONS_TASK_NAME,
                capability=MARKET_NOTIFICATIONS,
                run=lambda stop: run_market_notifications(market, stop_event=stop),
                foundational=False,
            )
        )
    if chain_tape is not None:
        tape = chain_tape
        tasks.append(
            WorkerTask(
                name=CHAIN_TAPE_TASK_NAME,
                capability=CHAIN_TAPE,
                run=lambda stop: run_chain_tape(tape, stop_event=stop),
                foundational=False,
            )
        )
    if signal_lane is not None:
        lane = signal_lane
        tasks.append(
            WorkerTask(
                name=SIGNAL_LANE_TASK_NAME,
                capability=TRADING_SIGNAL_LANE,
                run=lambda stop: run_signal_lane(lane, stop_event=stop, telemetry=telemetry),
                foundational=False,
            )
        )
    return tuple(tasks)


__all__ = [
    "WORKERS_CONTROL_TASK_NAME",
    "WORKERS_PROBE_TASK_NAME",
    "WorkerTask",
    "worker_business_tasks",
]
