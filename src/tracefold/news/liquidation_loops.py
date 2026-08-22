"""Bounded liquidation-level shadow polling, isolated from News delivery and decision paths (#144)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from typing import Any, cast

from .bus import DeferError, TransientError
from .cold_db import ColdDatabase
from .liquidation import (
    LIQUIDATION_MODEL_VERSION,
    LIQUIDATION_PERIOD_SECONDS,
    LIQUIDATION_PROVIDER,
    LIQUIDATION_RANGE,
    LIQUIDATION_REFRESH_SECONDS,
    LIQUIDATION_TARGET_MAX_PER_TURN,
    LIQUIDATION_TARGETS,
    LIQUIDATION_TURN_DEADLINE_SECONDS,
    LiquidationSnapshotProvider,
    LiquidationTarget,
    ProviderLiquidationSnapshot,
    unavailable_snapshot,
)

_COLD_READ_TIMEOUT_SECONDS = 10.0
_COLD_WRITE_TIMEOUT_SECONDS = 10.0


class LiquidationSnapshotLoop:
    """One exact pair per minute; every pair is refreshed roughly once per four-minute cycle."""

    def __init__(
        self,
        *,
        db: Any,
        provider: LiquidationSnapshotProvider,
        targets: Sequence[LiquidationTarget] = LIQUIDATION_TARGETS,
        period_seconds: float = LIQUIDATION_PERIOD_SECONDS,
        refresh_seconds: float = LIQUIDATION_REFRESH_SECONDS,
        turn_deadline_seconds: float = LIQUIDATION_TURN_DEADLINE_SECONDS,
        clock_ms: Callable[[], int] | None = None,
        enabled: bool = True,
    ) -> None:
        self.db = ColdDatabase(db)
        self.provider = provider
        self.targets = tuple(targets)
        self.period = max(60.0, float(period_seconds))
        self.refresh_ms = max(60.0, float(refresh_seconds)) * 1000.0
        self.turn_deadline = min(60.0, max(1.0, float(turn_deadline_seconds)))
        self.clock_ms = clock_ms or _clock_ms
        self.enabled = bool(enabled)
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._turn_active = False

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if not self.enabled:
            await stop_event.wait()
            return
        while not stop_event.is_set():
            await self.turn()
            await _sleep_or_stop(stop_event, self.period)

    async def turn(self) -> dict[str, Any]:
        # The Workers runner is sequential, and this guard also makes direct operational/test calls skip instead
        # of queueing a second provider request behind an in-flight turn.
        if self._turn_active:
            return {"due": 0, "attempted": 0, "written": 0, "fresh": 0, "error": "turn_in_progress"}
        self._turn_active = True
        try:
            return await self._turn_once()
        finally:
            self._turn_active = False

    async def _turn_once(self) -> dict[str, Any]:
        stamp = int(self.clock_ms())

        def _due(repos: Any) -> list[LiquidationTarget]:
            return cast(
                list[LiquidationTarget],
                repos.liquidation.due_targets(
                    self.targets,
                    provider=LIQUIDATION_PROVIDER,
                    model_version=LIQUIDATION_MODEL_VERSION,
                    range_key=LIQUIDATION_RANGE,
                    due_before_ms=stamp - int(self.refresh_ms),
                    limit=LIQUIDATION_TARGET_MAX_PER_TURN,
                ),
            )

        try:
            due = await self.db.read("news_liquidation_due", _due, timeout_seconds=_COLD_READ_TIMEOUT_SECONDS)
        except (TransientError, DeferError) as exc:
            self.last_error = f"db:{type(exc).__name__}"
            return {"due": 0, "attempted": 0, "written": 0, "fresh": 0, "error": self.last_error}
        if not due:
            self.last_error = None
            self.last_result = {"due": 0, "attempted": 0, "written": 0, "fresh": 0, "error": None}
            return self.last_result
        target = due[0]
        try:
            snapshot = await asyncio.wait_for(
                self.provider.fetch(
                    target,
                    model_version=LIQUIDATION_MODEL_VERSION,
                    range_key=LIQUIDATION_RANGE,
                ),
                timeout=self.turn_deadline,
            )
        except TimeoutError:
            snapshot = unavailable_snapshot(
                target,
                received_at_ms=int(self.clock_ms()),
                error_class="turn_deadline",
            )
        except Exception as exc:
            snapshot = unavailable_snapshot(
                target,
                received_at_ms=int(self.clock_ms()),
                error_class=f"provider_{type(exc).__name__}",
            )

        def _store(repos: Any, value: ProviderLiquidationSnapshot = snapshot) -> None:
            repos.liquidation.store_snapshot(value)

        try:
            await self.db.tx("news_liquidation_store", _store, timeout_seconds=_COLD_WRITE_TIMEOUT_SECONDS)
            written = 1
        except (TransientError, DeferError) as exc:
            written = 0
            self.last_error = f"db:{type(exc).__name__}"
        else:
            self.last_error = snapshot.error_class
        self.last_result = {
            "due": len(due),
            "attempted": 1,
            "written": written,
            "fresh": int(snapshot.freshness == "fresh" and written == 1),
            "error": self.last_error,
        }
        return self.last_result


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.001, float(seconds)))


def _clock_ms() -> int:
    import time

    return int(time.time() * 1000)


__all__ = ["LiquidationSnapshotLoop"]
