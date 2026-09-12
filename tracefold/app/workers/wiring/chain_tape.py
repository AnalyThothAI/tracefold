"""Compose independently supervised wallet ingestion, net-buy detection and price tasks (#641).

Each task owns its adapters, one bounded `advance()` and one `aclose()`. PostgreSQL facts and durable
work markers connect the stages. Slow price calls cannot hold up ingestion, and a faulted
stage closes only its own clients. App owns polling, cancellation and joining all in-flight work.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Protocol

from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.runtime import CHAIN_TAPE, WALLET_NET_BUY, WALLET_PRICES, CapabilityStates
from tracefold.app.workers.wiring.database import WorkerChainTapeDatabase
from tracefold.integrations.dexscreener import DexScreenerClient
from tracefold.integrations.robinhood_chain import RobinhoodChainClient
from tracefold.integrations.robinhoodtrenches import RobinhoodTrenchesClient
from tracefold.news.bus import now_ms
from tracefold.news.chain_tape import ChainTapeLoop
from tracefold.news.chain_tape.detect import NetBuyDetector
from tracefold.news.chain_tape.loop import POLL_INTERVAL_SECONDS
from tracefold.news.chain_tape.prices import WalletPriceSampler
from tracefold.news.chain_tape.roster import RosterRules
from tracefold.news.chain_tape.rules import WalletRules
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry

CHAIN_TAPE_TASK_NAME = "news-chain-tape"
WALLET_NET_BUY_TASK_NAME = "news-wallet-net-buy"
WALLET_PRICES_TASK_NAME = "news-wallet-prices"


class WalletStage(Protocol):
    """The three wallet tasks expose the same bounded turn and resource lifetime to App."""

    async def advance(self) -> Any: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ChainTapeComposition:
    """Three stages with independent resources and one operator-configured polling cadence."""

    loop: ChainTapeLoop
    poll_seconds: float
    detector: NetBuyDetector
    prices: WalletPriceSampler


def _wire_chain_tape(
    *,
    settings: Settings,
    db: WorkerDatabase,
    capabilities: CapabilityStates,
    telemetry: TelemetryRegistry | None = None,
) -> ChainTapeComposition | None:
    """Build the tape loop, or record why there is none. Never raises for a configuration fact."""

    chain_tape = settings.news.chain_tape
    if not chain_tape.enabled:
        for capability in (CHAIN_TAPE, WALLET_NET_BUY, WALLET_PRICES):
            capabilities.disabled(capability, "news_chain_tape_disabled")
        return None
    tape_db = WorkerChainTapeDatabase(db)
    loop = ChainTapeLoop(
        db=tape_db,
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
    detector = NetBuyDetector(
        db=tape_db,
        rules=WalletRules(
            net_buy_fast_n=chain_tape.rules.net_buy_fast_n,
            net_buy_slow_n=chain_tape.rules.net_buy_slow_n,
            min_net_buy_usd=chain_tape.rules.min_net_buy_usd,
            trigger_max_age_s=chain_tape.rules.trigger_max_age_s,
        ),
        notifications_enabled=chain_tape.notifications_enabled,
        clock=now_ms,
    )
    prices = WalletPriceSampler(db=tape_db, prices=DexScreenerClient(), clock=now_ms)
    for capability in (CHAIN_TAPE, WALLET_NET_BUY, WALLET_PRICES):
        capabilities.running(capability)
    return ChainTapeComposition(
        loop=loop,
        detector=detector,
        prices=prices,
        poll_seconds=float(chain_tape.poll_interval_s),
    )


async def run_chain_tape(
    loop: WalletStage,
    *,
    stop_event: asyncio.Event,
    poll_seconds: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Poll one stage and join its in-flight turn on stop or cancellation before closing its clients.

    Expected provider/admission failures remain the stage's durable retry decision. Unexpected errors
    propagate to its own Workers capability. A cancelled turn has no in-memory handoff to lose: the
    next process reads the stage's PostgreSQL work markers again.
    """

    try:
        while not stop_event.is_set():
            await _advance_or_stop(loop, stop_event=stop_event)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(poll_seconds)))
    finally:
        await loop.aclose()


async def _advance_or_stop(loop: WalletStage, *, stop_event: asyncio.Event) -> None:
    turn = asyncio.create_task(loop.advance())
    stopping = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait({turn, stopping}, return_when=asyncio.FIRST_COMPLETED)
        # A completed failure remains a failure even when stop was signalled in the same event-loop turn.
        if turn in done:
            await turn
    finally:
        for task in (turn, stopping):
            if not task.done():
                task.cancel()
        await asyncio.gather(turn, stopping, return_exceptions=True)
    if not turn.cancelled():
        turn.result()


__all__ = [
    "CHAIN_TAPE_TASK_NAME",
    "WALLET_NET_BUY_TASK_NAME",
    "WALLET_PRICES_TASK_NAME",
    "ChainTapeComposition",
    "_wire_chain_tape",
    "run_chain_tape",
]
