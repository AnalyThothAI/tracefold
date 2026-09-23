"""The venue-truth invariant's input: what the venue said about the account's positions, and when (#680 PR-3).

Nautilus keeps the Cache converged with the venue, but on 1.231.0 its reconciliation can also repair
a disagreement by inventing fills (the 2026-09-23 APT close), and every steady-state check the Runtime
had read only the Cache. So the Runtime reads the venue itself, every `VENUE_READ_INTERVAL_SECONDS`,
and the Strategy compares what it read with the Cache. This module is the read loop and the value it
produces; the comparison belongs to the Strategy, because only it may read the Cache.

The rules the value carries:

* a read that failed, timed out or could not be parsed is `unknown` (`positions is None`), never flat,
  and never clears a disagreement an earlier read found;
* a read is evidence only about the instant it started: a position that changed after that instant is
  judged by the next read, not this one;
* nothing here submits, cancels or changes anything. It is detect-only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal

from loguru import logger

# How often the venue is read, and how long one read may take. positionRisk weighs 5 of the
# account's 2,400 per minute; two a minute is a rounding error, and the Strategy never needs to know
# sooner than Nautilus' own 5 s checks would have told it.
VENUE_READ_INTERVAL_SECONDS = 30.0
VENUE_READ_TIMEOUT_SECONDS = 25.0
# Entries need a successful read younger than this: four intervals, so a single slow or failed read
# never blocks them and a venue this Runtime cannot see for two minutes always does.
VENUE_STALE_AFTER_NS = 120 * 1_000_000_000
# A read that started less than this after local activity on an instrument (a fill, a position event)
# does not judge that instrument: the venue and the Cache may be describing two different moments.
VENUE_SETTLE_NS = 5 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class VenueReading:
    """One read of the account's positions: Binance symbol -> signed quantity, or `None` if unknown."""

    started_at_ns: int
    completed_at_ns: int
    positions: Mapping[str, Decimal] | None
    failure: str | None = None

    def __post_init__(self) -> None:
        if self.started_at_ns <= 0 or self.completed_at_ns < self.started_at_ns:
            raise ValueError("oi_runtime_venue_reading_clock_invalid")
        if (self.positions is None) == (self.failure is None):
            raise ValueError("oi_runtime_venue_reading_invalid")

    @property
    def known(self) -> bool:
        return self.positions is not None

    def quantity(self, symbol: str) -> Decimal:
        return Decimal(0) if self.positions is None else self.positions.get(symbol, Decimal(0))


def read_failure(exc: BaseException) -> str:
    """A short, secret-free name for why a read failed: the Binance code when there is one."""

    if isinstance(exc, TimeoutError):
        return "timeout"
    message = getattr(exc, "message", None)
    code = message.get("code") if isinstance(message, Mapping) else None
    return f"{type(exc).__name__}:{code}" if code is not None else type(exc).__name__


async def watch_venue(
    read: Callable[[], Awaitable[Mapping[str, Decimal]]],
    observe: Callable[[VenueReading], None],
    stop: asyncio.Event,
    *,
    interval_seconds: float = VENUE_READ_INTERVAL_SECONDS,
    timeout_seconds: float = VENUE_READ_TIMEOUT_SECONDS,
    clock_ns: Callable[[], int] = time.time_ns,
) -> None:
    """Read the venue until `stop`, handing every outcome -- success or failure -- to `observe`.

    It runs on the event loop that runs every Nautilus callback, so `observe` is called on the thread
    that owns the Cache. It holds no database session and no transaction across the HTTP call.
    """

    while not stop.is_set():
        started_at_ns = clock_ns()
        try:
            positions = await asyncio.wait_for(read(), timeout=timeout_seconds)
            reading = VenueReading(started_at_ns, max(clock_ns(), started_at_ns), dict(positions))
        except Exception as exc:
            reading = VenueReading(started_at_ns, max(clock_ns(), started_at_ns), None, failure=read_failure(exc))
        try:
            observe(reading)
        except Exception:
            logger.exception("Execution runtime venue reading could not be observed")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)


__all__ = [
    "VENUE_READ_INTERVAL_SECONDS",
    "VENUE_READ_TIMEOUT_SECONDS",
    "VENUE_SETTLE_NS",
    "VENUE_STALE_AFTER_NS",
    "VenueReading",
    "read_failure",
    "watch_venue",
]
