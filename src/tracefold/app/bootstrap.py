from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from importlib.metadata import version
from threading import Thread
from typing import Any
from uuid import uuid4

from tracefold.app.database import WorkerDatabase
from tracefold.app.provider_types import WiredProviders
from tracefold.app.providers import wire_providers
from tracefold.app.runtime_claim_recovery import recover_old_runtime_claims
from tracefold.app.runtime_resources import ProviderGovernor, RuntimeResources
from tracefold.app.runtime_state import RuntimeSnapshot, capture_runtime_snapshot
from tracefold.app.worker_manifest import worker_names
from tracefold.app.worker_runtime_status import WorkerRuntimeStatusRepository
from tracefold.app.worker_runtime_supervisor import WorkerRuntimeSupervisor
from tracefold.app.workers import construct_workers
from tracefold.market import (
    CollectorService,
    EventMarketCaptureService,
    IngestService,
    TickLookup,
    require_event_anchor_active_window_ms,
)
from tracefold.platform.config.settings import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.postgres_client import postgres_health_check
from tracefold.platform.postgres.postgres_migrations import latest_migration_version


@dataclass(slots=True)
class WorkerRuntime:
    settings: Settings
    db: WorkerDatabase
    telemetry: TelemetryRegistry
    providers: WiredProviders
    collector: CollectorService
    supervisor: WorkerRuntimeSupervisor
    snapshot: RuntimeSnapshot
    ingest: _PooledIngestStore
    lock_conn: Any
    runtime_id: str
    resources: RuntimeResources
    provider_governor: ProviderGovernor
    role: str = "workers"

    def current_snapshot(self) -> RuntimeSnapshot:
        self.snapshot = capture_runtime_snapshot(self)
        return self.snapshot

    async def aclose(self) -> None:
        loop = asyncio.get_running_loop()
        shutdown_started = loop.time()
        errors = await self.supervisor.request_stop()
        self.resources.begin_shutdown()

        await self.resources.drain(
            ("realtime_db", "background_db", "cpu"),
            timeout_seconds=_remaining_shutdown_time(
                loop=loop,
                started_at=shutdown_started,
                deadline_seconds=5.0,
            ),
        )
        await self.resources.drain(
            ("provider_io", "model"),
            timeout_seconds=_remaining_shutdown_time(
                loop=loop,
                started_at=shutdown_started,
                deadline_seconds=25.0,
            ),
        )
        errors.extend(
            await self.supervisor.finish(
                timeout_seconds=_remaining_shutdown_time(
                    loop=loop,
                    started_at=shutdown_started,
                    deadline_seconds=30.0,
                ),
                publish_status=False,
            )
        )
        try:
            provider_errors = await asyncio.wait_for(
                _cleanup_runtime_providers(self),
                timeout=_remaining_shutdown_time(
                    loop=loop,
                    started_at=shutdown_started,
                    deadline_seconds=30.0,
                ),
            )
            errors.extend(provider_errors)
        except TimeoutError as exc:
            errors.append(exc)
        try:
            self.db.release_steady_runtime_lock(self.lock_conn)
        except Exception as exc:
            errors.append(exc)
        try:
            await self.db.aclose()
        except Exception as exc:
            errors.append(exc)
        try:
            self.resources.close()
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise ExceptionGroup("worker_runtime_cleanup_failed", errors)


def bootstrap_workers(settings: Settings) -> WorkerRuntime:
    telemetry = TelemetryRegistry()
    db: WorkerDatabase | None = None
    providers: WiredProviders | None = None
    lock_conn: Any | None = None
    resources: RuntimeResources | None = None
    try:
        db = WorkerDatabase.create(settings, telemetry=telemetry)
        with db.worker_pool.connection(timeout=0.250) as conn:
            startup_db = postgres_health_check(conn, expected_migration_version=latest_migration_version())
        if not startup_db.get("ok"):
            raise RuntimeError(f"postgres health check failed: {startup_db}")
        lock_conn = db.acquire_steady_runtime_lock()
        resources = RuntimeResources()
        providers = wire_providers(settings)
        runtime = _assemble_runtime(
            settings=settings,
            db=db,
            telemetry=telemetry,
            providers=providers,
            startup_db_status=startup_db,
            lock_conn=lock_conn,
            resources=resources,
        )
    except Exception as exc:
        for error in _cleanup_provider_roots_sync(providers):
            exc.add_note(f"provider cleanup failed: {type(error).__name__}: {error}")
        if db is not None:
            try:
                if lock_conn is not None:
                    db.release_steady_runtime_lock(lock_conn)
                _close_db_bundle_sync(db)
            except Exception as cleanup_exc:
                exc.add_note(f"db pool bundle cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}")
        if resources is not None:
            resources.close()
        raise
    return runtime


def _assemble_runtime(
    *,
    settings: Settings,
    db: WorkerDatabase,
    telemetry: TelemetryRegistry,
    providers: WiredProviders,
    startup_db_status: dict[str, object],
    lock_conn: Any,
    resources: RuntimeResources,
) -> WorkerRuntime:
    workers = settings.workers
    worker_collector_enabled = bool(
        workers.collector.enabled and providers.ingestion.upstream_client_factory is not None
    )
    ingest = _PooledIngestStore(
        db,
        providers=providers.asset_market,
        event_anchor_active_window_ms=workers.event_anchor_capture.active_window_ms,
    )
    collector = CollectorService(
        name="collector",
        settings=workers.collector,
        db=db,
        telemetry=telemetry,
        store=ingest,
        upstream_client=None,
    )
    provider_governor = ProviderGovernor()
    runtime_id = str(uuid4())
    recovered_claims = recover_old_runtime_claims(
        db,
        runtime_id=runtime_id,
        now_ms=_now_ms(),
    )
    runtime_workers = construct_workers(
        settings=settings,
        db=db,
        telemetry=telemetry,
        providers=providers,
        collector=collector,
        collector_enabled=worker_collector_enabled,
        resources=resources,
        provider_governor=provider_governor,
        runtime_id=runtime_id,
    )
    runtime_version = version("tracefold")

    async def publish_status(statuses: dict[str, dict[str, Any]]) -> None:
        await resources.run_background_db(
            _publish_runtime_status,
            db,
            runtime_id,
            runtime_version,
            statuses,
            _now_ms(),
        )

    supervisor = WorkerRuntimeSupervisor(
        workers=runtime_workers,
        status_sink=publish_status,
    )
    snapshot = RuntimeSnapshot.startup(
        startup_db_status=startup_db_status,
        composition={
            "ok": True,
            "runtime_role": "workers",
            "recovered_claims": recovered_claims,
        },
    )
    runtime = WorkerRuntime(
        settings=settings,
        db=db,
        telemetry=telemetry,
        providers=providers,
        collector=collector,
        supervisor=supervisor,
        snapshot=snapshot,
        ingest=ingest,
        lock_conn=lock_conn,
        runtime_id=runtime_id,
        resources=resources,
        provider_governor=provider_governor,
    )
    if worker_collector_enabled:
        factory = providers.ingestion.upstream_client_factory
        collector.upstream_client = factory(collector.handle_frame) if factory is not None else None
    runtime.current_snapshot()
    return runtime


class _PooledIngestStore:
    def __init__(
        self,
        db: WorkerDatabase,
        *,
        providers: Any,
        event_anchor_active_window_ms: int,
        now_ms: Any = None,
    ):
        self.db = db
        self.event_anchor_active_window_ms = require_event_anchor_active_window_ms(event_anchor_active_window_ms)
        self._capture_service = EventMarketCaptureService(
            providers=providers,
            now_ms=now_ms or _now_ms,
        )

    def insert_raw_frame(self, **kwargs) -> bool:
        with self.db.worker_session("collector") as repos, repos.transaction():
            return repos.evidence.insert_raw_frame(**kwargs)

    def ingest_event(self, event: Any):
        prepared = IngestService.prepare_event(event)
        market_resolutions: list[dict[str, Any]] = []
        prefetched_ticks: dict[tuple[str, str], Any] = {}
        with self.db.worker_session("collector") as repos, repos.transaction():
            ingest = _ingest_service_for_repos(
                repos,
                event_anchor_active_window_ms=self.event_anchor_active_window_ms,
            )
            if ingest.event_already_exists(prepared):
                return ingest.duplicate_result(prepared)
            ingest.prepare_registry_for_resolution(prepared)
            resolutions = ingest.resolve_prepared(prepared, persist=False)
            for decision in resolutions:
                market_resolution = ingest.market_resolution_for_decision(decision)
                if market_resolution is None:
                    continue
                market_resolutions.append(market_resolution)
                prefetched_ticks[(market_resolution["target_type"], market_resolution["target_id"])] = (
                    repos.market_ticks.latest_at_or_before(
                        target_type=market_resolution["target_type"],
                        target_id=market_resolution["target_id"],
                        at_ms=prepared.event_ms,
                        max_lag_ms=60_000,
                    )
                )
            tick_lookup = TickLookup(
                latest_at_or_before=lambda target_type, target_id, _at_ms, _max_lag_ms: prefetched_ticks.get(
                    (target_type, target_id)
                )
            )
            captures = [
                self._capture_service.capture_for_event(
                    event_id=market_resolution["event_id"],
                    intent_id=market_resolution["intent_id"],
                    resolution_id=market_resolution["resolution_id"],
                    resolution=market_resolution,
                    event_ms=prepared.event_ms,
                    tick_lookup=tick_lookup,
                )
                for market_resolution in market_resolutions
            ]
            result = ingest.commit_prepared_event(prepared, resolutions=resolutions, captures=captures)
            return result


def _ingest_service_for_repos(
    repos: Any,
    *,
    event_anchor_active_window_ms: int,
) -> IngestService:
    return IngestService(
        evidence=repos.evidence,
        entities=repos.entities,
        registry=repos.registry,
        identity_evidence=repos.identity_evidence,
        token_intent_lookup=repos.token_intent_lookup,
        token_evidence=repos.token_evidence,
        token_intents=repos.token_intents,
        intent_resolutions=repos.intent_resolutions,
        discovery=repos.discovery,
        market_ticks=repos.market_ticks,
        market_tick_current=repos.market_tick_current,
        enriched_events=repos.enriched_events,
        event_anchor_jobs=repos.event_anchor_jobs,
        radar_source_edges=repos.radar_source_edges,
        persisted_live=repos.persisted_live,
        transaction=repos.transaction,
        event_anchor_active_window_ms=event_anchor_active_window_ms,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _remaining_shutdown_time(
    *,
    loop: asyncio.AbstractEventLoop,
    started_at: float,
    deadline_seconds: float,
) -> float:
    return max(0.001, float(deadline_seconds) - (loop.time() - started_at))


def _publish_runtime_status(
    db: WorkerDatabase,
    runtime_id: str,
    runtime_version: str,
    statuses: dict[str, dict[str, Any]],
    now_ms: int,
) -> None:
    with db.worker_session("worker_runtime_status", statement_timeout_seconds=3) as repos, repos.transaction():
        queue_summaries = _runtime_queue_summaries(repos.conn)
        enriched_statuses = {}
        for unit_name, status in statuses.items():
            queue_summary = queue_summaries[unit_name]
            quarantine_count = int(queue_summary.get("quarantine_count") or 0)
            enriched_statuses[unit_name] = {
                **status,
                **queue_summary,
                "effective_status": (
                    "degraded"
                    if quarantine_count and str(status["effective_status"]) not in {"disabled", "unavailable", "failed"}
                    else status["effective_status"]
                ),
                "last_error": (
                    "unresolved_projection_quarantine"
                    if quarantine_count and not status.get("last_error")
                    else status.get("last_error")
                ),
            }
        WorkerRuntimeStatusRepository(repos.conn).publish(
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            statuses=enriched_statuses,
            now_ms=now_ms,
        )


def _runtime_queue_summaries(conn: Any) -> dict[str, dict[str, int | None]]:
    rows = conn.execute(
        """
        SELECT unit_name, queue_depth, oldest_due_at_ms, quarantine_count
        FROM (
          SELECT
            'event_anchor_capture'::text AS unit_name,
            count(*)::bigint AS queue_depth,
            min(next_run_at_ms)::bigint AS oldest_due_at_ms,
            0::bigint AS quarantine_count
          FROM event_anchor_backfill_jobs
          WHERE status IN ('pending', 'running')
          UNION ALL
          SELECT
            'resolution_refresh',
            count(*)::bigint,
            min(due_at_ms)::bigint,
            0::bigint
          FROM token_discovery_dirty_lookup_keys
          UNION ALL
          SELECT
            CASE clock_kind
              WHEN 'intraday_market' THEN 'macro_intraday_market'
              WHEN 'daily_settlement' THEN 'macro_settlements'
              WHEN 'scheduled_release' THEN 'macro_economic_releases'
              WHEN 'official_state' THEN 'macro_official_state'
              WHEN 'official_document' THEN 'macro_official_documents'
            END,
            count(*)::bigint,
            min(next_due_at_ms)::bigint,
            0::bigint
          FROM macro_acquisition_targets
          WHERE clock_kind <> 'backfill'
            AND status NOT IN ('invalid', 'unavailable')
          GROUP BY clock_kind
          UNION ALL
          SELECT
            'news_ingest',
            count(*)::bigint,
            min(next_fetch_at_ms)::bigint,
            0::bigint
          FROM news_sources
          WHERE enabled
          UNION ALL
          SELECT
            'asset_profile_refresh',
            count(*)::bigint,
            min(due_at_ms)::bigint,
            0::bigint
          FROM asset_profile_refresh_targets
          WHERE terminal_reason IS NULL
          UNION ALL
          SELECT
            'token_image_mirror',
            count(*)::bigint,
            min(due_at_ms)::bigint,
            0::bigint
          FROM token_image_source_dirty_targets
          UNION ALL
          SELECT
            'steady_projection_coordinator',
            count(*) FILTER (
              WHERE status IN ('dirty', 'retry_wait', 'running')
            )::bigint,
            min(deadline_at_ms) FILTER (
              WHERE status IN ('dirty', 'retry_wait', 'running')
            )::bigint,
            count(*) FILTER (WHERE status = 'quarantined')::bigint
          FROM (
            SELECT status, deadline_at_ms FROM radar_projection_frontiers
            UNION ALL
            SELECT status, deadline_at_ms FROM token_profile_projection_frontiers
            UNION ALL
            SELECT status, deadline_at_ms FROM macro_module_frontiers
            UNION ALL
            SELECT status, deadline_at_ms FROM news_projection_frontiers
          ) AS projection_frontiers
          UNION ALL
          SELECT
            'model_generation_coordinator',
            count(*) FILTER (
              WHERE status IN ('dirty', 'retry_wait', 'running')
            )::bigint,
            min(deadline_at_ms) FILTER (
              WHERE status IN ('dirty', 'retry_wait', 'running')
            )::bigint,
            count(*) FILTER (WHERE status = 'quarantined')::bigint
          FROM model_generation_frontiers
        ) AS summaries
        WHERE unit_name IS NOT NULL
        """
    ).fetchall()
    summaries: dict[str, dict[str, int | None]] = {
        name: {
            "deadline_at_ms": None,
            "queue_depth": 0,
            "oldest_due_at_ms": None,
        }
        for name in worker_names()
    }
    for row in rows:
        unit_name = str(row["unit_name"])
        if unit_name not in summaries:
            continue
        quarantine_count = int(row["quarantine_count"] or 0)
        summaries[unit_name] = {
            "deadline_at_ms": (int(row["oldest_due_at_ms"]) if row["oldest_due_at_ms"] is not None else None),
            "queue_depth": int(row["queue_depth"] or 0),
            "oldest_due_at_ms": (int(row["oldest_due_at_ms"]) if row["oldest_due_at_ms"] is not None else None),
            "quarantine_count": quarantine_count,
        }
    return summaries


async def _cleanup_runtime_providers(runtime: WorkerRuntime) -> list[Exception]:
    errors: list[Exception] = []
    try:
        await runtime.providers.aclose()
    except Exception as exc:
        errors.append(exc)
    return errors


def _cleanup_provider_roots_sync(
    providers: WiredProviders | None,
) -> list[Exception]:
    errors: list[Exception] = []
    if providers is not None:
        try:
            _await_sync(providers.aclose())
        except Exception as exc:
            errors.append(exc)
    return errors


def _close_db_bundle_sync(db: WorkerDatabase) -> None:
    _await_sync(db.aclose())


def _await_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(awaitable)
        except Exception as exc:
            result["error"] = exc

    thread = Thread(target=runner)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
