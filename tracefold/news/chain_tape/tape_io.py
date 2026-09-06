"""The database passes both halves of one chain-tape turn take, and the failure rule they share.

`derive` and `digest_writer` run inside the same `ChainTapeLoop.advance()` turn and sit under the same
contract: the rules half cannot fault the ingestion half. Each held its own byte-identical copy of the
two bounded passes and the absorb rule below, which is two places for one promise to be edited. The
copies differed in three values -- two timeouts and the word an error is recorded under -- so those are
what a subclass names.
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
    """One bounded read, one bounded write, and what happens to a failure the port did not translate."""

    db: Any
    _read_timeout_seconds: ClassVar[float]
    _write_timeout_seconds: ClassVar[float]
    # What this half calls itself in an error line and in its own log record.
    _failure_stage: ClassVar[str]
    _failure_label: ClassVar[str]

    async def _read(self, name: str, fn: Callable[[Any], Any], errors: list[str]) -> Any:
        try:
            return await self.db.read(name, fn, timeout_seconds=self._read_timeout_seconds)
        except (TransientError, DeferError) as exc:
            errors.append(f"db:{type(exc).__name__}")
            return FAILED
        except Exception as exc:  # deliberately everything; see `_absorb`
            return self._absorb(name, exc, errors)

    async def _write(self, name: str, fn: Callable[[Any], Any], errors: list[str]) -> Any:
        try:
            return await self.db.tx(name, fn, timeout_seconds=self._write_timeout_seconds)
        except (TransientError, DeferError) as exc:
            errors.append(f"db:{type(exc).__name__}")
            return FAILED
        except Exception as exc:  # deliberately everything; see `_absorb`
            return self._absorb(name, exc, errors)

    def _absorb(self, name: str, exc: BaseException, errors: list[str]) -> Any:
        """Record a failure the port did not translate, and end this pass rather than the tape.

        The port turns an admission refusal and an overrun into the two News errors above; anything else
        -- a constraint the driver refused, a shape a row could not take -- arrives here raw. The loop's
        own contract is that the rules half cannot fault the ingestion half, and it has to hold for the
        cases nobody enumerated as much as for the ones that were: a single derived row that PostgreSQL
        will not accept would otherwise stop the chain tape, on every restart, for ever, because the row
        or the window that produced it is still there to be produced again.

        It is recorded rather than swallowed. The reason reaches the tape's own state row through
        `errors`, the turn is `partial`, and the traceback is logged.
        """

        log.exception("chain tape %s failed: %s", self._failure_label, name)
        errors.append(f"{self._failure_stage}:{name}:{type(exc).__name__}")
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
