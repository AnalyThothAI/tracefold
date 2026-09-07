"""Bounded database passes for independently supervised wallet research and digest stages.

Translated admission and transient database refusals keep their durable work pending for the next
turn. Unexpected failures escape to that stage's capability supervisor. Retry diagnostics contain
operation and exception type only; a running capability means the loop is alive, not that its backlog
has drained.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Final

from ..bus import DeferError, TransientError

log = logging.getLogger("tracefold.news.chain_tape")

# "This pass did not answer." Distinct from a pass that answered `None`, which is a fact about the
# window rather than a failure.
FAILED: Final = object()


class TapePasses:
    """Retry expected database refusals; let each stage own the fate of an unexpected failure."""

    db: Any
    _read_timeout_seconds: ClassVar[float]
    _write_timeout_seconds: ClassVar[float]
    _failure_stage: ClassVar[str]

    async def _read(self, name: str, fn: Callable[[Any], Any], errors: list[str]) -> Any:
        try:
            return await self.db.read(name, fn, timeout_seconds=self._read_timeout_seconds)
        except (TransientError, DeferError) as exc:
            errors.append(f"db:{type(exc).__name__}")
            log.warning("wallet %s deferred operation=%s error=%s", self._failure_stage, name, type(exc).__name__)
            return FAILED

    async def _write(self, name: str, fn: Callable[[Any], Any], errors: list[str]) -> Any:
        try:
            return await self.db.tx(name, fn, timeout_seconds=self._write_timeout_seconds)
        except (TransientError, DeferError) as exc:
            errors.append(f"db:{type(exc).__name__}")
            log.warning("wallet %s deferred operation=%s error=%s", self._failure_stage, name, type(exc).__name__)
            return FAILED


def tape_decimal(value: Any, *, allow_zero: bool = True) -> Decimal | None:
    """One stored or provider-supplied number as an exact `Decimal`, or `None` when it is not one.

    `allow_zero` is the only thing the two callers disagree on, and it is a fact about what they read: a
    balance, an amount or a mark may legitimately be zero, and a moving-average cost a digest line is
    built from may not -- a zero there is a provider that has no cost for this position, not a position
    that cost nothing.
    """

    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed if (parsed >= 0 if allow_zero else parsed > 0) else None


def bag_for(bags: Sequence[Any], *, token: str, symbol: str | None) -> Any | None:
    """The provider's bag for this token: by address where it publishes one, by symbol where it does not."""

    for bag in bags:
        if str(getattr(bag, "token", "") or "").lower() == token:
            return bag
    if symbol:
        for bag in bags:
            if str(getattr(bag, "symbol", "") or "").upper() == symbol.upper():
                return bag
    return None


__all__ = ["FAILED", "TapePasses", "bag_for", "tape_decimal"]
