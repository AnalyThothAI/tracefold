"""Publish complete source membership; PostgreSQL owns the retry due time."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any, ClassVar, Final, Protocol

from ..bus import DeferError, TransientError, now_ms
from ..telemetry import NewsExternalDataSource, NewsExternalDataTelemetryPort, NewsWorkSemantics
from .contracts import CHAIN_TAPE_NAME, ChainTapeDatabasePort, RosterMember, RosterSnapshot, retry_delay_ms

ROSTER_SOURCE: Final[NewsExternalDataSource] = "robinhoodtrenches"
ROSTER_REFRESH_PERIOD_MS: Final = 3_600_000
POLL_INTERVAL_SECONDS: Final = 2.0


class RosterProviderPort(Protocol):
    @property
    def last_response_bytes(self) -> int: ...

    async def traders(self, *, window: str = "30d") -> Sequence[RosterMember]: ...


class RosterRefreshLoop:
    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("latest_state",)

    def __init__(
        self,
        *,
        db: ChainTapeDatabasePort,
        provider: RosterProviderPort,
        window: str = "30d",
        refresh_period_ms: int = ROSTER_REFRESH_PERIOD_MS,
        telemetry: NewsExternalDataTelemetryPort | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.db = db
        self.provider = provider
        self.window = window
        self.refresh_period_ms = max(0, int(refresh_period_ms))
        self.telemetry = telemetry
        self._clock = clock
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None

    async def aclose(self) -> None:
        close = getattr(self.provider, "aclose", None)
        if close is not None:
            await close()

    async def advance(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "due": False,
            "candidates": 0,
            "published": False,
            "roster_version": 0,
            "window": self.window,
        }
        self.last_result = result
        self.last_error = None
        try:
            state, current = await self.db.read("news_chain_tape_roster_state", _read_roster, timeout_seconds=5.0)
            stamp = self._clock()
            if not self._due(state, current):
                return result
            result["due"] = True
            # Persist the attempt before I/O. A restart after a killed request retains a
            # bounded retry, not an immediately due hot loop or a fabricated success.
            await self.db.tx(
                "news_chain_tape_roster_attempt",
                lambda repos: repos.news.chain_tape_begin_roster_refresh(
                    now_ms=stamp, next_attempt_at_ms=stamp + 30_000
                ),
                timeout_seconds=10.0,
            )
        except (TransientError, DeferError) as exc:
            self.last_error = f"db:{type(exc).__name__}"
            return result
        started = time.perf_counter()
        try:
            members = tuple(await self.provider.traders(window=self.window))
            if not members:
                raise ValueError("roster_payload_empty")
        except Exception as exc:
            self.last_error = f"{ROSTER_SOURCE}:{getattr(exc, 'code', None) or type(exc).__name__}"
            failures = int((state or {}).get("roster_consecutive_failures") or 0) + 1
            await self._failed(stamp, failures, int(getattr(exc, "retry_after_ms", 0) or 0))
            self._measure(started, "error", result)
            return result
        result["candidates"] = len(members)
        completed = self._clock()

        def publish(repos: Any) -> RosterSnapshot:
            snapshot: RosterSnapshot = repos.news.chain_tape_store_roster(members, now_ms=completed)
            repos.news.chain_tape_save_roster_refresh(
                now_ms=stamp,
                succeeded=True,
                error=None,
                completed_at_ms=completed,
                next_attempt_at_ms=completed + self.refresh_period_ms,
                consecutive_failures=0,
            )
            return snapshot

        try:
            snapshot = await self.db.tx("news_chain_tape_roster", publish, timeout_seconds=10.0)
        except (TransientError, DeferError) as exc:
            self.last_error = f"db:{type(exc).__name__}"
            await self._failed(stamp, int((state or {}).get("roster_consecutive_failures") or 0) + 1, 0)
            self._measure(started, "error", result)
            return result
        result.update(published=True, roster_version=snapshot.roster_version)
        self._measure(started, "success", result)
        return result

    def _due(self, state: Any, current: RosterSnapshot | None) -> bool:
        due = int((state or {}).get("roster_next_attempt_at_ms") or 0)
        if due:
            return self._clock() >= due
        if current is None:
            return True
        last = int((state or {}).get("roster_last_success_at_ms") or current.taken_at_ms)
        return self._clock() >= last + self.refresh_period_ms

    async def _failed(self, stamp: int, failures: int, advised_delay: int) -> None:
        try:
            await self.db.tx(
                "news_chain_tape_roster_refresh",
                lambda repos: repos.news.chain_tape_save_roster_refresh(
                    now_ms=stamp,
                    succeeded=False,
                    error=self.last_error,
                    next_attempt_at_ms=self._clock() + retry_delay_ms(failures, advised_delay),
                    consecutive_failures=failures,
                ),
                timeout_seconds=10.0,
            )
        except (TransientError, DeferError):
            # The previously committed attempt still carries its retry due time.
            return

    def _measure(self, started: float, outcome: Any, result: dict[str, Any]) -> None:
        if self.telemetry is not None:
            elapsed = time.perf_counter() - started
            self.telemetry.record_external_data_provider_call(
                CHAIN_TAPE_NAME,
                ROSTER_SOURCE,
                outcome,
                elapsed,
                byte_count=getattr(self.provider, "last_response_bytes", None),
            )
            self.telemetry.record_external_data_turn(
                CHAIN_TAPE_NAME, outcome, elapsed, target_count=result["candidates"], source_count=1
            )


def _read_roster(repos: Any) -> tuple[Any, RosterSnapshot | None]:
    # This task is the sole roster writer. Collection may update its own state,
    # but cannot change either membership or refresh timestamps during this read.
    return repos.news.chain_tape_state(), repos.news.chain_tape_current_roster()
