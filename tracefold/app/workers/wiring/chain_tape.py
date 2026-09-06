"""Composition for the `news-chain-tape` task (#572 PR-1).

One optional capability, one task, one `advance()`-shaped loop -- the same shape #553 PR-2 introduced
for the market notification loop, and for the same reason: the loop exposes one business action, while
the tick, the stop event and the process lifecycle belong to the Workers root.

The provider adapters are constructed here because `tracefold.news` may not name `httpx`. The loop sees
protocols and never learns which endpoint answered.

#572 PR-2 adds the rules half. The same site client answers the roster *and* the card context (a
wallet's bags, a token's mark), because it is one site and one courtesy pacing budget; DexScreener is a
third adapter and is used for one thing only -- the +1h/+4h price receipt after a card has already been
sent.

#572 PR-3 adds the four-hourly digest, and it is the one part of this flow a model touches. The
Program is resolved from the same operator settings the editorial Program is -- the reader-card slot,
because a digest is reader-facing Chinese copy -- and it is optional twice over: an unconfigured
endpoint and a disabled `digest` block both leave the tape writing the deterministic summary it
computed before any call was considered.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from loguru import logger

from tracefold.app.learning_runtime import compose_news_program_runtime
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.runtime import CHAIN_TAPE, CapabilityStates
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


@dataclass(frozen=True, slots=True)
class ChainTapeComposition:
    """The composed tape and the tick the operator configured for it.

    The cadence rides here rather than on the loop because the loop owns no clock: App polls it, exactly
    as it polls the market notification loop and the Signal lane, and `news.chain_tape.poll_interval_s`
    is what App polls it with.
    """

    loop: ChainTapeLoop
    poll_seconds: float


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
        capabilities.disabled(CHAIN_TAPE, "news_chain_tape_disabled")
        return None
    tape_db = WorkerChainTapeDatabase(db)
    chain = RobinhoodChainClient(rpc_url=chain_tape.rpc_url)
    # One session against the site, shared by the roster refresh and the card context, so the two share
    # the pacing floor the adapter applies to somebody else's small public server.
    site = RobinhoodTrenchesClient(base_url=chain_tape.roster_provider_url)
    loop = ChainTapeLoop(
        db=tape_db,
        chain=chain,
        roster_provider=site,
        rules=RosterRules(
            min_closed_trades=chain_tape.roster.min_closed_trades,
            min_profit_factor=chain_tape.roster.min_profit_factor,
            top_quality=chain_tape.roster.top_quality,
            top_whale_by_open_cost=chain_tape.roster.top_whale_by_open_cost,
        ),
        telemetry=telemetry,
        deriver=WalletCardDeriver(
            db=tape_db,
            chain=chain,
            site=site,
            prices=DexScreenerClient(),
            rules=_wallet_rules(chain_tape.rules),
            telemetry=telemetry,
            clock=now_ms,
        ),
        digest=_wire_digest(settings, db=tape_db, site=site, telemetry=telemetry),
    )
    capabilities.running(CHAIN_TAPE)
    return ChainTapeComposition(loop=loop, poll_seconds=float(chain_tape.poll_interval_s))


def _wire_digest(
    settings: Settings,
    *,
    db: WorkerChainTapeDatabase,
    site: RobinhoodTrenchesClient,
    telemetry: TelemetryRegistry | None,
) -> WalletDigestWriter | None:
    """The four-hourly summary, with a model behind it when one is configured (#572 §5.4).

    Two independent switches, and neither of them is a fault. `digest.enabled` off means no summary at
    all; a Program that resolves to `None` -- no model endpoint configured on this host -- means the
    summary is written from its own fact pack. The card rules are unaffected by either, which is the
    whole point of keeping the model off the card path.

    The same site session as the roster and the card context: the digest asks it for one thing, the
    moving-average cost of a bounded number of positions, and it shares that client's pacing floor.
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
        bags=site,
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


__all__ = ["CHAIN_TAPE_TASK_NAME", "ChainTapeComposition", "_wire_chain_tape", "run_chain_tape"]
