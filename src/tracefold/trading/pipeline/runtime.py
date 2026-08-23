"""Shared runtime contract for the two Trading runners."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..candidate.eligibility import EligibilityPolicy
from ..contracts import (
    TRADING_COLD_WRITE_TIMEOUT_SECONDS,
    TRADING_RECONCILE_BACKOFF_MS,
    Bar,
    TradingMode,
)
from ..decision.policy import DEFAULT_TRADE_POLICY, TradePolicy
from ..decision.regime import DEFAULT_REGIME_POLICY, RegimePolicy
from ..execution.order import DEFAULT_ORDER_POLICY, OrderPolicy

COLD_READ_TIMEOUT_SECONDS = 10.0
COLD_WRITE_TIMEOUT_SECONDS = TRADING_COLD_WRITE_TIMEOUT_SECONDS
BAR_INTERVAL_MS = 300_000
RECONCILE_BACKOFF_MS = TRADING_RECONCILE_BACKOFF_MS

BarFetcher = Callable[[str, int, int], Awaitable[Sequence[Bar]]]
BarFetcherFactory = Callable[[str], BarFetcher | None]
CandidateProjectionReader = Callable[
    [Any, str, int, int, int, int],
    tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
]
InstrumentProjectionReader = Callable[[Any, str, Sequence[str]], Sequence[Mapping[str, Any]]]


def now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


async def sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.05, float(seconds)))


@dataclass(frozen=True, slots=True)
class TradingConfig:
    """Everything a runner needs that is not a collaborator. One object so a turn is reproducible."""

    mode: TradingMode = "paper"
    account_ref: str = "default"
    poll_seconds: float = 2.0
    oi_metric_version: str = "oi_signal_v1"
    venue_priority: tuple[str, ...] = ("binance", "hyperliquid")
    eligibility: EligibilityPolicy = field(default_factory=EligibilityPolicy)
    regime: RegimePolicy = field(default_factory=lambda: DEFAULT_REGIME_POLICY)
    trade: TradePolicy = field(default_factory=lambda: DEFAULT_TRADE_POLICY)
    order: OrderPolicy = field(default_factory=lambda: DEFAULT_ORDER_POLICY)
    max_dspy_cases_per_day: int = 12


__all__ = [
    "BAR_INTERVAL_MS",
    "COLD_READ_TIMEOUT_SECONDS",
    "COLD_WRITE_TIMEOUT_SECONDS",
    "RECONCILE_BACKOFF_MS",
    "BarFetcher",
    "BarFetcherFactory",
    "CandidateProjectionReader",
    "InstrumentProjectionReader",
    "TradingConfig",
    "now_ms",
    "sleep_or_stop",
]
