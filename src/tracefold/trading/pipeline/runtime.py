"""Shared runtime contract for the two Trading runners."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from ..candidate.eligibility import EligibilityPolicy
from ..contracts import (
    TRADING_COLD_WRITE_TIMEOUT_SECONDS,
    Bar,
    InstrumentCandidateRow,
    LiquidationCandidateRow,
    LiveExchangeId,
    NewsCandidateRow,
    OiCandidateRow,
)
from ..decision.policy import DEFAULT_TRADE_POLICY, TradePolicy
from ..decision.regime import DEFAULT_REGIME_POLICY, RegimePolicy

COLD_READ_TIMEOUT_SECONDS = 10.0
COLD_WRITE_TIMEOUT_SECONDS = TRADING_COLD_WRITE_TIMEOUT_SECONDS
BAR_INTERVAL_MS = 300_000


class TradingDatabasePort(Protocol):
    """The two runners' whole view of the process database: one bounded read, one bounded transaction.

    Capital safety is why this is a port and not a shared client. A runner may not open a session, pick a
    pool, choose a lane, or hold a connection across a provider call; it names an operation and a deadline
    and gets a repository session back. The composition root satisfies it on the one-slot heavy admission
    shared with Event Reaction and Janitor (#88, #104), so a trading backlog can never compete for the four
    News lane slots or ordinary Quote admission.
    """

    async def tx[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T: ...

    async def read[T](self, name: str, fn: Callable[[Any], T], *, timeout_seconds: float) -> T: ...


BarFetcher = Callable[[str, int, int], Awaitable[Sequence[Bar]]]
BarFetcherFactory = Callable[[str], BarFetcher | None]
# `(repos, metric_version, after_ms, until_ms) -> three trigger lanes`. The repository session stays
# opaque: this context never learns which repositories it carries. No Trading threshold is passed (#264);
# the projection answers "which facts exist", the Candidate Gate answers "which of them may trigger".
CandidateProjectionReader = Callable[
    [Any, str, int, int],
    tuple[Sequence[OiCandidateRow], Sequence[NewsCandidateRow], Sequence[LiquidationCandidateRow]],
]
InstrumentProjectionReader = Callable[[Any, str, Sequence[str]], Sequence[InstrumentCandidateRow]]


def now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def cutoff_history_start_ms(*, anchor_at_ms: int, lookback_ms: int) -> int:
    """Open time of the candle that closed immediately before the lookback target."""

    target = int(anchor_at_ms) - int(lookback_ms)
    return (target // BAR_INTERVAL_MS - 1) * BAR_INTERVAL_MS


async def sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(seconds)))


@dataclass(frozen=True, slots=True)
class TradingConfig:
    """Everything a runner needs that is not a collaborator. One object so a turn is reproducible."""

    poll_seconds: float = 2.0
    oi_metric_version: str = "oi_signal_v1"
    venue_priority: tuple[LiveExchangeId, ...] = ("binance", "hyperliquid")
    eligibility: EligibilityPolicy = field(default_factory=EligibilityPolicy)
    regime: RegimePolicy = field(default_factory=lambda: DEFAULT_REGIME_POLICY)
    trade: TradePolicy = field(default_factory=lambda: DEFAULT_TRADE_POLICY)
    fixed_notional_usd: Decimal = Decimal("10")
    max_dspy_cases_per_day: int = 12


__all__ = [
    "BAR_INTERVAL_MS",
    "COLD_READ_TIMEOUT_SECONDS",
    "COLD_WRITE_TIMEOUT_SECONDS",
    "BarFetcher",
    "BarFetcherFactory",
    "CandidateProjectionReader",
    "InstrumentProjectionReader",
    "TradingConfig",
    "TradingDatabasePort",
    "cutoff_history_start_ms",
    "now_ms",
    "sleep_or_stop",
]
