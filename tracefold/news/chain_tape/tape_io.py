"""Bounded database passes for independently supervised wallet detection and price stages.

Translated admission and transient database refusals keep their durable work pending for the next
turn. Unexpected failures escape to that stage's capability supervisor. Retry diagnostics contain
operation and exception type only; a running capability means the loop is alive, not that its backlog
has drained.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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


__all__ = ["FAILED", "TapePasses"]
