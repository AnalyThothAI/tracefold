"""Composition for the `news-chain-tape` task (#572 PR-1).

One optional capability, one task, one `advance()`-shaped loop -- the same shape #553 PR-2 introduced
for the market notification loop, and for the same reason: the loop exposes one business action, while
the tick, the stop event and the process lifecycle belong to the Workers root.

The two provider adapters are constructed here because `tracefold.news` may not name `httpx`. The loop
sees two protocols and never learns which endpoint answered.
"""

from __future__ import annotations

import asyncio
import contextlib

from loguru import logger

from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.runtime import CHAIN_TAPE, CapabilityStates
from tracefold.app.workers.wiring.database import WorkerChainTapeDatabase
from tracefold.integrations.robinhood_chain import RobinhoodChainClient
from tracefold.integrations.robinhoodtrenches import RobinhoodTrenchesClient
from tracefold.news.chain_tape import ChainTapeLoop
from tracefold.news.chain_tape.loop import POLL_INTERVAL_SECONDS
from tracefold.news.chain_tape.roster import RosterRules
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry

CHAIN_TAPE_TASK_NAME = "news-chain-tape"


def _wire_chain_tape(
    *,
    settings: Settings,
    db: WorkerDatabase,
    capabilities: CapabilityStates,
    telemetry: TelemetryRegistry | None = None,
) -> ChainTapeLoop | None:
    """Build the tape loop, or record why there is none. Never raises for a configuration fact."""

    chain_tape = settings.news.chain_tape
    if not chain_tape.enabled:
        capabilities.disabled(CHAIN_TAPE, "news_chain_tape_disabled")
        return None
    loop = ChainTapeLoop(
        db=WorkerChainTapeDatabase(db),
        chain=RobinhoodChainClient(rpc_url=chain_tape.rpc_url),
        roster_provider=RobinhoodTrenchesClient(base_url=chain_tape.roster_provider_url),
        rules=RosterRules(
            min_closed_trades=chain_tape.roster.min_closed_trades,
            min_profit_factor=chain_tape.roster.min_profit_factor,
            top_quality=chain_tape.roster.top_quality,
            top_whale_by_open_cost=chain_tape.roster.top_whale_by_open_cost,
        ),
        telemetry=telemetry,
    )
    capabilities.running(CHAIN_TAPE)
    return loop


async def run_chain_tape(
    loop: ChainTapeLoop,
    *,
    stop_event: asyncio.Event,
    poll_seconds: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Poll `advance()` until the process stops. The loop owns no clock and no timer of its own.

    An exception out of `advance()` is an infrastructure fault by construction: every provider failure is
    already an outcome recorded on the tape's own state row, so what is left is the database port and a
    program error. It ends this run of the loop and is raised rather than swallowed. The Workers root
    records `chain_tape` as `faulted`, the task stops, and News reception, market facts and every read
    carry on beside it. Nothing restarts it: the position is in PostgreSQL, so an operator restart after
    the fix resumes from exactly where this process stopped.
    """

    try:
        while not stop_event.is_set():
            try:
                await loop.advance()
            except Exception:
                logger.exception("chain tape turn failed")
                raise
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(poll_seconds)))
    finally:
        await loop.aclose()


__all__ = ["CHAIN_TAPE_TASK_NAME", "_wire_chain_tape", "run_chain_tape"]
