"""Instrument snapshots, retention, and outbox maintenance loops."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, ClassVar, Literal

from ..bus import DeferError, TransientError, new_trace_id, now_ms
from ..models import OUTBOX_MAX_AGE_MS
from ..telemetry import NewsExternalDataSource, NewsExternalDataTelemetryPort, NewsWorkSemantics
from .admission import publish_event
from .runtime import NewsDatabasePort, _sleep_or_stop

log = logging.getLogger("tracefold.news")

_OUTBOX_MIN_AGE_MS = 15_000
_JANITOR_PERIOD_SECONDS = 60.0
_DAY_MS = 24 * 3600_000
_INSTRUMENT_SNAPSHOT_PERIOD_SECONDS = 6 * 3600.0
_INSTRUMENT_RETRY_SECONDS = 15 * 60.0


class InstrumentSnapshotLoop:
    """Venue listing catalogues -> `news_market_instruments`, one bounded snapshot per period (#75).

    The universe is a provider fact, so the snapshot is idempotent and rebuildable: re-running it on an unchanged
    catalogue only moves `last_seen_ms`. It feeds symbol normalization and the Gate's asset class — not listing
    cards, which arrive as provider frames (#89).

    A venue that fails is skipped, never fatal: `apply_snapshot` only reconciles venues that actually answered, so
    an unreachable Binance cannot read as a mass delisting.
    """

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("latest_state",)

    def __init__(
        self,
        *,
        db: NewsDatabasePort,
        fetchers: Sequence[tuple[str, Callable[[], Any]]],
        period_seconds: float = _INSTRUMENT_SNAPSHOT_PERIOD_SECONDS,
        enabled: bool = True,
        telemetry: NewsExternalDataTelemetryPort | None = None,
    ) -> None:
        self.db = db
        self.fetchers = tuple(fetchers)
        self.period = float(period_seconds)
        self.enabled = bool(enabled)
        self.telemetry = telemetry
        self.last_result: Any | None = None
        self.last_error: str | None = None

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if not self.enabled or not self.fetchers:
            if self.telemetry is not None:
                self.telemetry.record_external_data_skipped("instrument_snapshot", "disabled")
            await stop_event.wait()
            return
        while not stop_event.is_set():
            started = time.perf_counter()
            try:
                ok = await self.turn()
            except Exception:
                if self.telemetry is not None:
                    self.telemetry.record_external_data_turn(
                        "instrument_snapshot",
                        "error",
                        time.perf_counter() - started,
                        source_count=len(self.fetchers),
                    )
                raise
            if self.telemetry is not None:
                result = self.last_result if ok else None
                self.telemetry.record_external_data_turn(
                    "instrument_snapshot",
                    "partial" if ok and self.last_error else ("success" if ok else "error"),
                    time.perf_counter() - started,
                    target_count=int(getattr(result, "total", 0) or 0),
                    source_count=len(self.fetchers),
                )
            await _sleep_or_stop(stop_event, self.period if ok else _INSTRUMENT_RETRY_SECONDS)

    async def turn(self) -> bool:
        """One snapshot. Returns False when no venue answered, so the caller retries sooner."""

        instruments: list[Any] = []
        errors: list[str] = []
        for venue, fetch in self.fetchers:
            started = time.perf_counter()
            try:
                fetched = await fetch()
            except Exception as exc:  # adapters raise VenueExpectedError; anything else is equally non-fatal here
                if self.telemetry is not None:
                    self.telemetry.record_external_data_provider_call(
                        "instrument_snapshot",
                        _instrument_source(venue),
                        "error",
                        time.perf_counter() - started,
                    )
                code = getattr(exc, "code", None) or type(exc).__name__
                errors.append(f"{venue}:{code}")
                log.warning("news instrument snapshot venue failed venue=%s code=%s", venue, code)
            else:
                instruments.extend(fetched)
                if self.telemetry is not None:
                    self.telemetry.record_external_data_provider_call(
                        "instrument_snapshot",
                        _instrument_source(venue),
                        "success",
                        time.perf_counter() - started,
                    )
        self.last_error = ",".join(errors) or None
        if not instruments:
            return False
        stamp = now_ms()

        def _apply(repos: Any, items: list[Any] = instruments, s: int = stamp) -> Any:
            repos.instruments.reconcile_seed_aliases(now_ms=s)
            result = repos.instruments.apply_snapshot(items, now_ms=s)
            repos.instruments.learn_aliases_from_universe(now_ms=s)
            return result, repos.instruments.dangling_seed_aliases()

        try:
            result, dangling = await self.db.tx("news_instrument_snapshot", _apply, timeout_seconds=30.0)
        except (TransientError, DeferError) as exc:
            self.last_error = f"db:{type(exc).__name__}"
            return False
        self.last_result = result
        log.info(
            "news instrument snapshot venues=%s total=%d delisted=%d",
            ",".join(result.venues),
            result.total,
            result.delisted,
        )
        # A seed alias pointing at a symbol no venue lists resolves to nothing, silently, forever (#89).
        for row in dangling:
            log.warning(
                "news instrument seed alias resolves to nothing alias=%s base=%s", row["alias"], row["base_symbol"]
            )
        return True


def _instrument_source(venue: str) -> NewsExternalDataSource:
    if venue == "binance":
        return "binance"
    if venue == "hyperliquid":
        return "hyperliquid"
    if venue == "okx":
        return "okx"
    if venue == "us_reference":
        return "us_reference"
    return "other"


class JanitorLoop:
    """Outbox catch-up, band expiry, retention, broker snapshot — one bounded turn per period."""

    external_data_exempt_reason: ClassVar[Literal["internal_maintenance"]] = "internal_maintenance"

    def __init__(
        self,
        *,
        db: NewsDatabasePort,
        cold_db: NewsDatabasePort,
        bus: Any | None = None,
        period_seconds: float = _JANITOR_PERIOD_SECONDS,
        retention_raw_days: int = 30,
        retention_judged_days: int = 365,
    ) -> None:
        # Two ports, because the retention sweep is a measured heavy transaction and the outbox catch-up is
        # not. Which physical lane each one lands on is the composition root's answer, never the Janitor's.
        self.db = db
        self.cold_db = cold_db
        self.bus = bus
        self.period = float(period_seconds)
        # Two tiers (#81): a raw Item nobody judged is storage, an Item behind a judged or labelled Event is the
        # corpus every later comparison replays against.
        self.retention_raw_ms = int(retention_raw_days) * _DAY_MS
        self.retention_judged_ms = int(retention_judged_days) * _DAY_MS

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.turn()
            await _sleep_or_stop(stop_event, self.period)

    async def turn(self) -> None:
        stamp = now_ms()
        if self.bus is not None:
            with contextlib.suppress(TransientError, DeferError, Exception):
                await self.republish_unpublished()
        try:

            def _janitor(repos: Any, s: int = stamp) -> dict[str, Any]:
                repos.news.expire_bands(now_ms=s)
                repos.news.purge_before(
                    cutoff_ms=s - self.retention_raw_ms, judged_cutoff_ms=s - self.retention_judged_ms
                )
                return dict(repos.news.purge_learning_retention(batch_size=500))

            retention = await self.cold_db.tx("news_janitor", _janitor, timeout_seconds=10.0)
            deleted = sum(
                int(retention.get(field) or 0) for field in ("deleted_recordings", "deleted_cases", "deleted_artifacts")
            )
            if deleted:
                log.info("news learning retention deleted=%d detail=%s", deleted, retention)
        except Exception as exc:
            error_code = f"learning_retention_failed:{type(exc).__name__}"
            log.warning("news learning retention failed code=%s", error_code)
            with contextlib.suppress(Exception):

                def _retention_error(repos: Any, s: int = stamp, code: str = error_code) -> None:
                    repos.news.record_learning_retention_error(error_code=code, now_ms=s)

                await self.cold_db.tx("news_learning_retention_error", _retention_error, timeout_seconds=2.0)
        if self.bus is not None:
            snapshot: dict[str, Any] = {"configured": True, "connected": False, "queues": {}, "error_code": None}
            try:
                depths = await asyncio.wait_for(self.bus.queue_depths(), timeout=5.0)
                prefix = f"{self.bus.prefix}." if getattr(self.bus, "prefix", "") else ""
                snapshot.update(
                    connected=True,
                    queues={name.removeprefix(prefix): value for name, value in depths.items()},
                )
            except Exception as exc:
                snapshot["error_code"] = f"broker_snapshot_failed:{type(exc).__name__}"
            with contextlib.suppress(TransientError, DeferError):

                def _snapshot(repos: Any, s: int = stamp, snap: dict[str, Any] = snapshot) -> None:
                    repos.news.update_broker_snapshot(snapshot=snap, now_ms=s)

                await self.db.tx("news_broker_snapshot", _snapshot, timeout_seconds=3.0)

    async def republish_unpublished(self) -> int:
        """Commit-then-crash (or publish failure) before publish: re-publish candidate Events that never left."""

        stamp = now_ms()
        floor_ms, ceiling_ms = stamp - _OUTBOX_MIN_AGE_MS, stamp - OUTBOX_MAX_AGE_MS

        def _scan(repos: Any) -> Any:
            return repos.news.outbox_scan(older_than_ms=floor_ms, newer_than_ms=ceiling_ms)

        rows, expired = await self.db.read("news_outbox_unpublished", _scan)
        if expired:
            # Never silent: the ceiling gave up on these, and that is a fact an operator should see.
            log.warning(
                "news outbox gave up on %d stranded event(s) older than %d min (#76)",
                expired,
                OUTBOX_MAX_AGE_MS // 60_000,
            )
        republished = 0
        for row in rows:
            event_id = str(row["event_id"])

            def _card(repos: Any, e: str = event_id) -> Any:
                return repos.news.event_card(e)

            card = await self.db.read("news_outbox_card", _card)
            if card is None:
                continue
            await publish_event(
                self.bus,
                self.db,
                event_id=str(card["event_id"]),
                dedupe_family=str(card["dedupe_family"]),
                queue_priority=str(card["queue_priority"]),
                trace_id=str(card.get("trace_id") or new_trace_id()),
            )
            republished += 1
        return republished
