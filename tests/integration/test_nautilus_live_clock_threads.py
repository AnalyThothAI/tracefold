"""Which OS thread each pinned-Nautilus callback runs on (#510 F).

`OiNautilusStrategy` keeps one unlocked mutable aggregate, `RuntimeExecutionState`: plain dicts, sets
and dataclass attributes touched by the entry, protection, exit and recovery coordinators. Every
order and position callback reaches it through the message bus, and the 100 ms `OI-PUMP` timer
reaches it through `on_timer`. Whether that is one thread or two is not a style question — it decides
whether the Runtime needs `call_soon_threadsafe` or a lock, and neither the Nautilus docs nor the
Python type stubs state it.

So measure it, against a real `nautilus_trader.live.node.TradingNode` on the pinned version, with no
exchange attached: no data client, no execution client, no credentials, no network. The node still
builds the same kernel, the same `LiveClock` and the same event loop the production Runtime uses, and
that is the whole of what this question is about.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import timedelta
from typing import Any

import pytest
from nautilus_trader.config import LoggingConfig, StrategyConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.trading.strategy import Strategy

pytestmark = pytest.mark.integration

_TIMER_INTERVAL = timedelta(milliseconds=20)
_TIMER_NAME = "OI-THREAD-PROBE"


class _ThreadProbeStrategy(Strategy):
    """The `OiNautilusStrategy` timer shape and nothing else: set one timer, record who calls it."""

    def __init__(self) -> None:
        super().__init__(StrategyConfig(strategy_id="THREAD-PROBE"))
        self.start_thread: int | None = None
        self.timer_threads: list[int] = []

    def on_start(self) -> None:
        self.start_thread = threading.get_ident()
        self.clock.set_timer(
            name=_TIMER_NAME,
            interval=_TIMER_INTERVAL,
            callback=self.on_timer,
            fire_immediately=True,
        )

    def on_timer(self, _event: Any) -> None:
        self.timer_threads.append(threading.get_ident())

    def on_stop(self) -> None:
        if _TIMER_NAME in self.clock.timer_names:
            self.clock.cancel_timer(_TIMER_NAME)


def _node_config() -> TradingNodeConfig:
    return TradingNodeConfig(
        trader_id=TraderId("PROBE-000"),
        logging=LoggingConfig(log_level="ERROR", log_colors=False, use_pyo3=True),
        data_clients={},
        exec_clients={},
        timeout_connection=10.0,
        timeout_reconciliation=10.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
        timeout_post_stop=10.0,
    )


async def _measure() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    node = TradingNode(config=_node_config(), loop=loop)
    strategy = _ThreadProbeStrategy()
    node.trader.add_strategy(strategy)
    node.build()
    task = asyncio.create_task(node.run_async(), name="probe-node")
    try:
        deadline = loop.time() + 30.0
        while len(strategy.timer_threads) < 3 and loop.time() < deadline:
            if task.done():
                await task
                raise RuntimeError("probe_node_returned")
            await asyncio.sleep(0.02)
        measured = {
            "event_loop_thread": threading.get_ident(),
            "on_start_thread": strategy.start_thread,
            "on_timer_threads": frozenset(strategy.timer_threads),
            "timer_calls": len(strategy.timer_threads),
            "python_managed_threads": frozenset(item.ident for item in threading.enumerate()),
        }
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(node.stop_async(), timeout=20.0)
        task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=20.0)
    return measured


def test_live_clock_timers_run_off_the_event_loop_thread_that_owns_every_other_callback() -> None:
    """The measurement, and the reason `on_timer` may not touch Runtime state directly.

    Result on `nautilus-trader` 1.231.0: `on_start` runs on the asyncio event-loop thread, and the
    `LiveClock` timer callback runs on exactly one other thread which `threading.enumerate()` does not
    list at all — it is owned by the Rust core, not by Python. `OiNautilusStrategy.on_timer` therefore
    does nothing but `call_soon_threadsafe(self._pump)`, so the coordinators keep mutating
    `RuntimeExecutionState` from the single thread the order and position callbacks already arrive on.

    If a future pin makes the timer run on the loop thread this test fails, and the marshalling can be
    deleted with evidence rather than by assumption.
    """

    measured = asyncio.run(_measure())

    assert measured["timer_calls"] >= 3
    assert measured["on_start_thread"] == measured["event_loop_thread"]
    assert len(measured["on_timer_threads"]) == 1
    assert measured["event_loop_thread"] not in measured["on_timer_threads"]
    assert measured["on_timer_threads"].isdisjoint(measured["python_managed_threads"])
