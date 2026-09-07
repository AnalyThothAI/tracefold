"""Compose independently supervised wallet ingestion, research and digest tasks (#614).

Each task owns its adapters, one bounded `advance()` and one `aclose()`. PostgreSQL facts and durable
work markers connect the stages. Slow context or model calls cannot hold up ingestion, and a faulted
stage closes only its own clients. App owns polling, cancellation and joining all in-flight work.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from loguru import logger

from tracefold.app.learning_runtime import compose_news_program_runtime
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.runtime import CHAIN_TAPE, WALLET_DIGEST, WALLET_RESEARCH, CapabilityStates
from tracefold.app.workers.wiring.database import WorkerChainTapeDatabase
from tracefold.integrations.dexscreener import DexScreenerClient
from tracefold.integrations.robinhood_chain import RobinhoodChainClient
from tracefold.integrations.robinhoodtrenches import RobinhoodTrenchesClient
from tracefold.news.bus import now_ms
from tracefold.news.chain_tape import ChainTapeLoop
from tracefold.news.chain_tape.derive import WalletCardDeriver
from tracefold.news.chain_tape.digest_writer import WalletDigestWriter
from tracefold.news.chain_tape.loop import POLL_INTERVAL_SECONDS
from tracefold.news.chain_tape.roster import RosterRules
from tracefold.news.chain_tape.rules import WalletRules
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry

CHAIN_TAPE_TASK_NAME = "news-chain-tape"
WALLET_RESEARCH_TASK_NAME = "news-wallet-research"
WALLET_DIGEST_TASK_NAME = "news-wallet-digest"


class WalletStage(Protocol):
    """The three wallet tasks expose the same bounded turn and resource lifetime to App."""

    async def advance(self) -> Any: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ChainTapeComposition:
    """Three stages with independent resources and one operator-configured polling cadence."""

    loop: ChainTapeLoop
    poll_seconds: float
    research: WalletCardDeriver
    digest: WalletDigestWriter | None


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
        for capability in (CHAIN_TAPE, WALLET_RESEARCH, WALLET_DIGEST):
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
    research = WalletCardDeriver(
        db=tape_db,
        chain=RobinhoodChainClient(rpc_url=chain_tape.rpc_url),
        site=RobinhoodTrenchesClient(base_url=chain_tape.roster_provider_url),
        prices=DexScreenerClient(),
        rules=_wallet_rules(chain_tape.rules),
        telemetry=telemetry,
        clock=now_ms,
    )
    digest = _wire_digest(settings, db=tape_db, telemetry=telemetry)
    capabilities.running(CHAIN_TAPE)
    capabilities.running(WALLET_RESEARCH)
    if digest is None:
        capabilities.disabled(WALLET_DIGEST, "news_wallet_digest_disabled")
    else:
        capabilities.running(WALLET_DIGEST)
    return ChainTapeComposition(
        loop=loop, research=research, digest=digest, poll_seconds=float(chain_tape.poll_interval_s)
    )


def _wire_digest(
    settings: Settings,
    *,
    db: WorkerChainTapeDatabase,
    telemetry: TelemetryRegistry | None,
) -> WalletDigestWriter | None:
    """The four-hourly summary, with a model behind it when one is configured (#572 §5.4).

    Two independent switches, and neither of them is a fault. `digest.enabled` off means no summary at
    all; a Program that resolves to `None` -- no model endpoint configured on this host -- means the
    summary is written from its own fact pack. The card rules are unaffected by either, which is the
    whole point of keeping the model off the card path.

    The digest owns its site session, so cancelling it cannot close an ingestion or research request.
    """

    configured = settings.news.chain_tape.digest
    if not configured.enabled:
        return None
    program = compose_news_program_runtime(settings).chain_tape_digest()
    if program is None:
        logger.info("chain tape digest has no configured model; summaries render from the fact pack")
    return WalletDigestWriter(
        db=db,
        program=program,
        bags=RobinhoodTrenchesClient(base_url=settings.news.chain_tape.roster_provider_url),
        interval_s=int(configured.interval_s),
        max_calls_per_day=int(configured.max_calls_per_day),
        telemetry=telemetry,
        clock=now_ms,
    )


def _wallet_rules(configured: Any) -> WalletRules:
    """The operator's thresholds as the rules module's own value object.

    The dollar figures cross as `Decimal` rather than as the floats YAML produced: they are compared
    against stored `numeric` position values, and a float comparison against a `Decimal` is the one
    place a threshold could quietly mean something other than what was configured.
    """

    return WalletRules(
        exit_notifications_enabled=bool(configured.exit_notifications_enabled),
        buy_min_usd=Decimal(str(configured.buy_min_usd)),
        buy_window_s=int(configured.buy_window_s),
        exit_ratio_bps=int(configured.exit_ratio_bps),
        exit_min_position_usd=Decimal(str(configured.exit_min_position_usd)),
        exit_cascade_window_s=int(configured.exit_cascade_window_s),
        exit_cascade_min_usd=Decimal(str(configured.exit_cascade_min_usd)),
        crowding_n=int(configured.crowding_n),
        crowding_window_s=int(configured.crowding_window_s),
        crowding_min_usd=Decimal(str(configured.crowding_min_usd)),
        crowding_premium_late_bps=int(configured.crowding_premium_late_bps),
        trigger_max_age_s=int(configured.trigger_max_age_s),
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
    "WALLET_DIGEST_TASK_NAME",
    "WALLET_RESEARCH_TASK_NAME",
    "ChainTapeComposition",
    "_wire_chain_tape",
    "run_chain_tape",
]
