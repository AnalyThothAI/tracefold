"""The `news-wallet-roster` turn: rebuild the followed list, or publish nothing at all (#649 §5.1).

This used to be the first thing `ChainTapeLoop.advance()` did, and it was wrong in two separate ways
that production made visible on 2026-09-15 (#649 §2.1):

* **It was inside the collection turn.** Forty-five per-trader documents, awaited one at a time
  against a 15 s read timeout, ran *before* the first chain call. A slow provider was a stopped
  collector.
* **A rate-limited refresh still published.** A per-trader request that answered 429 was written down
  as "profit factor unknown", an unknown factor cannot pass the quality rule, and the resulting list
  was stored as a new version anyway. Production published one version an hour for two days, and the
  count of addresses with a known profit factor swung 13 → 26 → 19 → 24 between neighbouring
  versions, entirely from throttling. The eligibility list -- the thing that decides whose buys can
  raise an alert -- was being rewritten hourly by network noise.

So this task publishes on one condition: **every candidate lookup answered.** The three outcomes are
kept apart, because they mean different things and only one of them is a reason to withhold a list:

* a document that answered, with or without a profit factor -- an ordinary statistic;
* a handle the site does not have (404) -- explicitly unknown, cannot pass the quality rule, and not
  a failure of this refresh;
* a call that did not answer at all (timeout, 429, unparseable body) -- this refresh **failed**, the
  previous version stays exactly as it is with its own `taken_at_ms`, and the attempt's time and
  reason are recorded so the page can say so.

Pacing is the client's (`RobinhoodTrenchesClient.pace_seconds`), measured rather than guessed: the
per-trader endpoint answers in about 1.2 s and the eighteenth call inside 25 s answered 429, so the
floor is two seconds and ninety-two candidates take about three minutes of a one-hour period.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Sequence
from typing import Any, ClassVar, Final, Protocol

from ..bus import DeferError, TransientError, now_ms
from ..telemetry import (
    NewsExternalDataSource,
    NewsExternalDataTelemetryPort,
    NewsWorkSemantics,
)
from .contracts import CHAIN_TAPE_NAME, RosterSnapshot
from .loop import ChainTapeDatabasePort
from .roster import RosterRules, quality_candidates, select_roster

ROSTER_SOURCE: Final[NewsExternalDataSource] = "robinhoodtrenches"
ROSTER_REFRESH_PERIOD_MS: Final = 3_600_000
# The task's own tick. The refresh itself is due at most once an hour; this is only how often the
# task asks whether it is due, and it is the same cadence the other three wallet stages poll at.
POLL_INTERVAL_SECONDS: Final = 2.0
_DB_READ_TIMEOUT_SECONDS: Final = 5.0
_DB_WRITE_TIMEOUT_SECONDS: Final = 10.0

# The site did not answer. Distinct from a site that answered "no such handle", which is a fact about
# the roster rather than a failure of the refresh.
_FAILED: Final = object()


class RosterProviderPort(Protocol):
    """The roster's authority: the tracked list, and one document per handle for its profit factor.

    Both calls take the statistics window, because the list and the factor have to be computed over
    the same one. They were not: the list was requested at `window=7d` and the per-trader document
    was requested with no window at all, so the deployed rule compared a seven-day factor against a
    seven-day closed-trade count only by the provider's default (#649 §2.1).
    """

    @property
    def last_response_bytes(self) -> int: ...

    async def traders(self, *, window: str = "7d") -> Sequence[Any]: ...

    async def trader(self, handle: str, *, window: str = "7d") -> Any | None: ...


class RosterRefreshLoop:
    """One bounded refresh turn. Owns no clock, no timer and no task of its own."""

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("latest_state",)

    def __init__(
        self,
        *,
        db: ChainTapeDatabasePort,
        provider: RosterProviderPort,
        rules: RosterRules | None = None,
        window: str = "30d",
        refresh_period_ms: int = ROSTER_REFRESH_PERIOD_MS,
        telemetry: NewsExternalDataTelemetryPort | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.db = db
        self.provider = provider
        self.rules = rules or RosterRules()
        self.window = str(window)
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
        """Rebuild the list when it is due, and publish it only if every lookup answered."""

        result: dict[str, Any] = {
            "due": False,
            "candidates": 0,
            "looked_up": 0,
            "profit_factor_known": 0,
            "profit_factor_unknown": 0,
            "published": False,
            "roster_version": 0,
            "window": self.window,
        }
        self.last_error = None
        try:
            state, current = await self.db.read(
                "news_chain_tape_roster_state",
                lambda repos: (repos.news.chain_tape_state(), repos.news.chain_tape_current_roster()),
                timeout_seconds=_DB_READ_TIMEOUT_SECONDS,
            )
        except (TransientError, DeferError) as exc:
            # No attempt was made and there is nothing to record it against; the next turn re-reads.
            self.last_error = f"db:{type(exc).__name__}"
            self.last_result = dict(result)
            return result
        if not self._due(state, current):
            self.last_result = dict(result)
            return result
        result["due"] = True
        started = time.perf_counter()
        stamp = self._clock()
        errors: list[str] = []
        outcome = await self._rebuild(result, errors)
        if outcome is None:
            await self._record(stamp, succeeded=False, error=errors[0] if errors else "roster_refresh_failed")
            self.last_error = errors[0] if errors else "roster_refresh_failed"
            self._measure(started, "error", result)
            self.last_result = dict(result)
            return result
        published = await self._publish(outcome, stamp=stamp, result=result, errors=errors)
        if not published:
            self.last_error = errors[0] if errors else "roster_publish_failed"
            self._measure(started, "error", result)
            self.last_result = dict(result)
            return result
        self._measure(started, "success", result)
        self.last_result = dict(result)
        return result

    def _due(self, state: Any, current: RosterSnapshot | None) -> bool:
        """Is a rebuild owed? An hour since the last *successful* refresh, or no list at all.

        The clock that decides this is the last refresh that published, not the last attempt: a
        refresh that failed must come back, and a `taken_at_ms` re-stamped by an unchanged list is
        the same fetch time the success wrote.
        """

        if current is None:
            return True
        if not self.refresh_period_ms:
            return True
        last = (state or {}).get("roster_last_success_at_ms") or int(current.taken_at_ms)
        return self._clock() - int(last) >= self.refresh_period_ms

    async def _rebuild(
        self, result: dict[str, Any], errors: list[str]
    ) -> tuple[Sequence[Any], dict[str, float | None]] | None:
        """The whole provider conversation. `None` means "this refresh failed", never "empty list"."""

        candidates = await self._call(functools.partial(self.provider.traders, window=self.window), errors)
        if candidates is _FAILED:
            return None
        result["candidates"] = len(candidates)
        factors: dict[str, float | None] = {}
        for row in quality_candidates(candidates, rules=self.rules):
            handle = str(getattr(row, "handle", "") or "")
            if not handle or handle in factors:
                continue
            stats = await self._call(functools.partial(self.provider.trader, handle, window=self.window), errors)
            if stats is _FAILED:
                # A call that did not answer ends the refresh. This is the whole point: an unknown
                # factor cannot pass the quality rule, so publishing here would quietly demote every
                # address the site simply declined to talk about this hour.
                return None
            result["looked_up"] += 1
            factor = None if stats is None else getattr(stats, "profit_factor", None)
            factors[handle] = factor
            result["profit_factor_known" if factor is not None else "profit_factor_unknown"] += 1
        return candidates, factors

    async def _publish(
        self,
        rebuilt: tuple[Sequence[Any], dict[str, float | None]],
        *,
        stamp: int,
        result: dict[str, Any],
        errors: list[str],
    ) -> bool:
        candidates, factors = rebuilt
        members = select_roster(candidates, profit_factors=factors, rules=self.rules)
        if not members:
            # The site answered and nobody qualified on either list. That is a real answer about the
            # provider, not a complete roster, and replacing a working list with an empty one would
            # stop collection outright.
            errors.append("roster_selected_nobody")
            await self._record(stamp, succeeded=False, error="roster_selected_nobody")
            return False
        try:
            snapshot = await self.db.tx(
                "news_chain_tape_roster",
                lambda repos: _store(repos, members, stamp=stamp),
                timeout_seconds=_DB_WRITE_TIMEOUT_SECONDS,
            )
        except (TransientError, DeferError) as exc:
            errors.append(f"db:{type(exc).__name__}")
            self.last_error = errors[-1]
            return False
        result["published"] = True
        result["roster_version"] = snapshot.roster_version
        return True

    async def _record(self, stamp: int, *, succeeded: bool, error: str | None) -> None:
        try:
            await self.db.tx(
                "news_chain_tape_roster_refresh",
                functools.partial(_record_refresh, now_ms=stamp, succeeded=succeeded, error=error),
                timeout_seconds=_DB_WRITE_TIMEOUT_SECONDS,
            )
        except (TransientError, DeferError):
            # The refresh already failed; failing to write down that it failed does not make it worse
            # and must not fault the capability. The next turn attempts again.
            return

    async def _call(self, call: Callable[[], Any], errors: list[str]) -> Any:
        started = time.perf_counter()
        try:
            answer = await call()
        except Exception as exc:  # provider failures are expected and are this refresh's answer
            code = getattr(exc, "code", None) or type(exc).__name__
            errors.append(f"{ROSTER_SOURCE}:{code}")
            if self.telemetry is not None:
                self.telemetry.record_external_data_provider_call(
                    CHAIN_TAPE_NAME, ROSTER_SOURCE, "error", time.perf_counter() - started
                )
            return _FAILED
        if self.telemetry is not None:
            self.telemetry.record_external_data_provider_call(
                CHAIN_TAPE_NAME,
                ROSTER_SOURCE,
                "success",
                time.perf_counter() - started,
                byte_count=getattr(self.provider, "last_response_bytes", None),
            )
        return answer

    def _measure(self, started: float, outcome: Any, result: dict[str, Any]) -> None:
        if self.telemetry is None:
            return
        self.telemetry.record_external_data_turn(
            CHAIN_TAPE_NAME,
            outcome,
            time.perf_counter() - started,
            target_count=int(result.get("candidates") or 0),
            source_count=1,
        )


def _store(repos: Any, members: Sequence[Any], *, stamp: int) -> RosterSnapshot:
    """Publish and record the success in one transaction: a published list has a fresh success time."""

    snapshot: RosterSnapshot = repos.news.chain_tape_store_roster(members, now_ms=stamp)
    repos.news.chain_tape_save_roster_refresh(now_ms=stamp, succeeded=True, error=None)
    return snapshot


def _record_refresh(repos: Any, *, now_ms: int, succeeded: bool, error: str | None) -> None:
    repos.news.chain_tape_save_roster_refresh(now_ms=now_ms, succeeded=succeeded, error=error)


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "ROSTER_REFRESH_PERIOD_MS",
    "ROSTER_SOURCE",
    "RosterProviderPort",
    "RosterRefreshLoop",
]
