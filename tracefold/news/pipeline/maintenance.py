"""Instrument snapshots, retention, and outbox maintenance loops."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, ClassVar, Literal, cast

from ..bus import (
    BrokerBackpressure,
    BrokerPublishFailure,
    BrokerUnavailable,
    DeferError,
    TransientError,
    new_trace_id,
    now_ms,
)
from ..models import OUTBOX_MAX_AGE_MS
from ..telemetry import (
    NewsDurableEventTelemetryPort,
    NewsExternalDataSource,
    NewsExternalDataTelemetryPort,
    NewsHandoffRepairOutcome,
    NewsHandoffStage,
    NewsOpenNewsIncidentCause,
    NewsWorkSemantics,
)
from .admission import publish_event
from .runtime import NewsDatabasePort, _sleep_or_stop
from .triage import publish_verdict

log = logging.getLogger("tracefold.news")

_OUTBOX_MIN_AGE_MS = 15_000
_JANITOR_PERIOD_SECONDS = 60.0
_DAY_MS = 24 * 3600_000
_RAW_RETENTION_BATCH_SIZE = 500
# One group is one row, and production holds hundreds, so a batch this size drains in one pass.
_MARKET_TRACK_PRUNE_BATCH = 500
_RAW_RETENTION_MAX_BATCHES = 4
_RAW_RETENTION_MAX_WALL_SECONDS = 3.0
_RAW_RETENTION_BATCH_TIMEOUT_SECONDS = 1.0
_INSTRUMENT_SNAPSHOT_PERIOD_SECONDS = 6 * 3600.0
# The bus bounds the snapshot's own AMQP and management work; this is only the loop's guard against an
# adapter that never returns, so it has to sit above every snapshot that *does* finish. Cutting it below
# the adapter's management read timeout would report a slow management API as a disconnected broker —
# the one distinction the snapshot exists to make. It stays under the AMQP heartbeat, so a genuinely
# hung connection still trips it and is reported, truthfully, as not connected.
_BROKER_SNAPSHOT_DEADLINE_SECONDS = 15.0
_INSTRUMENT_RETRY_SECONDS = 15 * 60.0
_OPENNEWS_INCIDENT_CAUSES: tuple[NewsOpenNewsIncidentCause, ...] = (
    "authentication",
    "broker_backpressure",
    "broker_unavailable",
    "idle_timeout",
    "network_connect",
    "planned_shutdown",
    "process_outage",
    "protocol_error",
    "provider_close",
    "triage_circuit_open",
    "unknown",
)


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
        telemetry: NewsDurableEventTelemetryPort | None = None,
    ) -> None:
        # Two ports, because the retention sweep is a measured heavy transaction and the outbox catch-up is
        # not. Which physical lane each one lands on is the composition root's answer, never the Janitor's.
        self.db = db
        self.cold_db = cold_db
        self.bus = bus
        self.telemetry = telemetry
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
            try:
                await self.repair_event_handoffs()
            except (BrokerBackpressure, BrokerUnavailable, TransientError, DeferError) as exc:
                self._record_handoff_repair("event", "transient")
                log.warning("news Event handoff repair deferred error=%s", type(exc).__name__)
            try:
                await self.repair_verdict_handoffs()
            except (BrokerBackpressure, BrokerUnavailable, TransientError, DeferError) as exc:
                self._record_handoff_repair("verdict", "transient")
                log.warning("news Verdict handoff repair deferred error=%s", type(exc).__name__)
        if self.telemetry is not None:
            try:
                await self._refresh_incident_telemetry(stamp)
            except (TransientError, DeferError) as exc:
                log.warning("news OpenNews incident telemetry deferred error=%s", type(exc).__name__)
        try:
            await self.cold_db.tx(
                "news_expire_bands",
                lambda repos: repos.news.expire_bands(now_ms=stamp, batch_size=_RAW_RETENTION_BATCH_SIZE),
                timeout_seconds=3.0,
            )
        except Exception as exc:
            log.warning("news band expiry failed code=band_expiry_failed:%s", type(exc).__name__)
        try:
            await self._purge_raw_retention(stamp)
        except Exception as exc:
            log.warning("news raw retention failed code=raw_retention_failed:%s", type(exc).__name__)
        try:
            retention = await self.cold_db.tx(
                "news_learning_retention",
                lambda repos: dict(repos.news.purge_learning_retention(batch_size=500)),
                timeout_seconds=10.0,
            )
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
            snapshot: dict[str, Any] = {
                "configured": True,
                "connected": False,
                "queues": {},
                "error_code": None,
                "last_publish_error_code": None,
                "last_publish_error_at_ms": None,
            }
            try:
                depths = await asyncio.wait_for(self.bus.broker_snapshot(), timeout=_BROKER_SNAPSHOT_DEADLINE_SECONDS)
                prefix = f"{self.bus.prefix}." if getattr(self.bus, "prefix", "") else ""
                snapshot.update(
                    connected=True,
                    queues={name.removeprefix(prefix): value for name, value in depths.items()},
                )
            except Exception as exc:
                snapshot["error_code"] = f"broker_snapshot_failed:{type(exc).__name__}"
            last_publish_failure = self.bus.last_publish_failure
            if isinstance(last_publish_failure, BrokerPublishFailure):
                snapshot["last_publish_error_code"] = last_publish_failure.error_code
                snapshot["last_publish_error_at_ms"] = last_publish_failure.at_ms
            broker_observed_at_ms = now_ms()
            with contextlib.suppress(TransientError, DeferError):

                def _snapshot(
                    repos: Any,
                    s: int = broker_observed_at_ms,
                    snap: dict[str, Any] = snapshot,
                ) -> None:
                    repos.news.update_broker_snapshot(snapshot=snap, now_ms=s)

                await self.db.tx("news_broker_snapshot", _snapshot, timeout_seconds=3.0)

    async def _purge_raw_retention(self, stamp: int) -> None:
        started = time.perf_counter()
        deleted_rows = 0
        batches = 0
        backlog_rows = 0
        backlog_capped = False
        oldest_observed_at_ms: int | None = None
        for _ in range(_RAW_RETENTION_MAX_BATCHES):
            remaining_seconds = _RAW_RETENTION_MAX_WALL_SECONDS - (time.perf_counter() - started)
            if remaining_seconds <= 0:
                break
            result = await self.cold_db.tx(
                "news_raw_retention",
                lambda repos: dict(
                    repos.news.purge_before(
                        cutoff_ms=stamp - self.retention_raw_ms,
                        judged_cutoff_ms=stamp - self.retention_judged_ms,
                        batch_size=_RAW_RETENTION_BATCH_SIZE,
                    )
                ),
                timeout_seconds=min(_RAW_RETENTION_BATCH_TIMEOUT_SECONDS, remaining_seconds),
            )
            batches += 1
            deleted_rows += int(result.get("deleted_items") or 0)
            backlog_rows = int(result.get("backlog_items") or 0)
            backlog_capped = bool(result.get("backlog_capped"))
            oldest = result.get("oldest_observed_at_ms")
            oldest_observed_at_ms = None if oldest is None else int(oldest)
            if backlog_rows == 0:
                break
        # Alerting state for a group whose every observation has left the retention window answers a
        # question about records nobody can read any more. One bounded batch per pass, beside the
        # purge that made them unreadable, so it can never become its own unbounded scan (#553 §3.4).
        with contextlib.suppress(TransientError, DeferError):
            await self.cold_db.tx(
                "news_market_track_retention",
                lambda repos: repos.news.market_prune_tracks(
                    cutoff_ms=stamp - self.retention_judged_ms, limit=_MARKET_TRACK_PRUNE_BATCH
                ),
            )
        wall_seconds = max(0.0, time.perf_counter() - started)
        oldest_age_seconds = (
            0.0 if oldest_observed_at_ms is None else max(0.0, (stamp - oldest_observed_at_ms) / 1000.0)
        )
        if self.telemetry is not None:
            self.telemetry.record_news_raw_retention(
                deleted_rows=deleted_rows,
                batches=batches,
                wall_seconds=wall_seconds,
                backlog_rows=backlog_rows,
                backlog_capped=backlog_capped,
                oldest_age_seconds=oldest_age_seconds,
            )
        if deleted_rows or backlog_rows:
            log.info(
                "news raw retention deleted=%d batches=%d backlog=%d capped=%s "
                "oldest_age_seconds=%.3f wall_seconds=%.3f",
                deleted_rows,
                batches,
                backlog_rows,
                backlog_capped,
                oldest_age_seconds,
                wall_seconds,
            )

    def _record_handoff_state(self, stage: NewsHandoffStage, state: dict[str, int | None], stamp: int) -> None:
        if self.telemetry is None:
            return
        oldest_at = state.get("oldest_pending_at_ms")
        self.telemetry.set_news_handoff_state(
            stage,
            pending=int(state.get("pending") or 0),
            oldest_age_seconds=max(0.0, (stamp - int(oldest_at)) / 1000.0) if oldest_at is not None else 0.0,
            expired=int(state.get("expired") or 0),
        )

    def _record_handoff_repair(self, stage: NewsHandoffStage, outcome: NewsHandoffRepairOutcome) -> None:
        if self.telemetry is not None:
            self.telemetry.record_news_handoff_repair(stage, outcome)

    async def _refresh_incident_telemetry(self, stamp: int) -> None:
        telemetry = self.telemetry
        if telemetry is None:
            return
        rows = await self.db.read(
            "news_opennews_incident_summary",
            lambda repos: repos.news.open_incident_summary(),
        )
        for cause in _OPENNEWS_INCIDENT_CAUSES:
            telemetry.set_news_opennews_incident(
                provider="opennews",
                cause=cause,
                count=0,
                oldest_age_seconds=0.0,
            )
        summaries: dict[NewsOpenNewsIncidentCause, tuple[int, int | None]] = {
            cause: (0, None) for cause in _OPENNEWS_INCIDENT_CAUSES
        }
        for row in rows or []:
            raw_cause = str(row.get("cause_class") or "unknown")
            cause = cast(
                NewsOpenNewsIncidentCause,
                raw_cause if raw_cause in _OPENNEWS_INCIDENT_CAUSES else "unknown",
            )
            count, oldest = summaries[cause]
            opened_at = row.get("oldest_opened_at_ms")
            opened = int(opened_at) if opened_at is not None else None
            summaries[cause] = (
                count + int(row.get("count") or 0),
                opened if oldest is None else (oldest if opened is None else min(oldest, opened)),
            )
        for cause in _OPENNEWS_INCIDENT_CAUSES:
            count, oldest = summaries[cause]
            if count <= 0:
                continue
            telemetry.set_news_opennews_incident(
                provider="opennews",
                cause=cause,
                count=count,
                oldest_age_seconds=max(0.0, (stamp - oldest) / 1000.0) if oldest is not None else 0.0,
            )

    async def repair_event_handoffs(self) -> int:
        """Repair confirmed Event-to-Triage handoffs inside the relevance window."""

        stamp = now_ms()
        floor_ms, ceiling_ms = stamp - _OUTBOX_MIN_AGE_MS, stamp - OUTBOX_MAX_AGE_MS

        def _scan(repos: Any) -> Any:
            return repos.news.event_handoff_scan(older_than_ms=floor_ms, newer_than_ms=ceiling_ms)

        rows, state = await self.db.read("news_event_handoff_scan", _scan)
        self._record_handoff_state("event", state, stamp)
        expired = int(state.get("expired") or 0)
        if expired:
            log.warning(
                "news Event handoff expired for %d row(s) older than %d min",
                expired,
                OUTBOX_MAX_AGE_MS // 60_000,
            )
        republished = 0
        for row in rows:
            outcome = await publish_event(
                self.bus,
                self.db,
                event_id=str(row["event_id"]),
                dedupe_family=str(row["dedupe_family"]),
                queue_priority=str(row["queue_priority"]),
                trace_id=str(row.get("trace_id") or new_trace_id()),
                occurred_at_ms=int(row["opened_at_ms"]),
            )
            self._record_handoff_repair("event", outcome)
            republished += 1
        return republished

    async def repair_verdict_handoffs(self) -> int:
        """Repair confirmed push-Verdict-to-Delivery handoffs inside the relevance window."""

        stamp = now_ms()
        floor_ms, ceiling_ms = stamp - _OUTBOX_MIN_AGE_MS, stamp - OUTBOX_MAX_AGE_MS

        def _scan(repos: Any) -> Any:
            return repos.news.verdict_handoff_scan(older_than_ms=floor_ms, newer_than_ms=ceiling_ms)

        rows, state = await self.db.read("news_verdict_handoff_scan", _scan)
        self._record_handoff_state("verdict", state, stamp)
        expired = int(state.get("expired") or 0)
        if expired:
            log.warning(
                "news Verdict handoff expired for %d row(s) older than %d min",
                expired,
                OUTBOX_MAX_AGE_MS // 60_000,
            )
        republished = 0
        for row in rows:
            outcome = await publish_verdict(
                self.bus,
                self.db,
                event_id=str(row["event_id"]),
                trace_id=str(row.get("trace_id") or new_trace_id()),
                amqp_priority=5 if str(row["queue_priority"]) == "high" else 0,
                policy_version=str(row["policy_version"]),
                occurred_at_ms=int(row["created_at_ms"]),
            )
            self._record_handoff_repair("verdict", outcome)
            republished += 1
        return republished
