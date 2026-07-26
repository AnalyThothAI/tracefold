from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Thread
from typing import Any

from tracefold.app.database import DBPoolBundle
from tracefold.app.provider_types import WiredProviders
from tracefold.app.providers import wire_providers
from tracefold.app.runtime_state import RuntimeSnapshot, capture_runtime_snapshot
from tracefold.app.worker_scheduler import WorkerScheduler
from tracefold.app.workers import construct_workers
from tracefold.market import (
    CollectorService,
    EventMarketCaptureService,
    EventPublisherProtocol,
    IngestService,
    TickLookup,
    require_event_anchor_active_window_ms,
)
from tracefold.platform.config.settings import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.postgres_client import postgres_health_check
from tracefold.platform.postgres.postgres_migrations import latest_migration_version


@dataclass(slots=True)
class Runtime:
    settings: Settings
    db: DBPoolBundle
    telemetry: TelemetryRegistry
    providers: WiredProviders
    hub: EventPublisherProtocol
    collector: CollectorService
    scheduler: WorkerScheduler
    snapshot: RuntimeSnapshot
    ingest: _PooledIngestStore

    def repositories(self):
        return self.db.api_session()

    def current_snapshot(self) -> RuntimeSnapshot:
        self.snapshot = capture_runtime_snapshot(self)
        return self.snapshot

    async def aclose(self) -> None:
        scheduler_error: Exception | None = None
        try:
            await self.scheduler.stop()
        except Exception as exc:
            scheduler_error = exc
        provider_errors = await _cleanup_runtime_providers(self)
        if scheduler_error is not None:
            if provider_errors:
                raise ExceptionGroup("runtime_close_failed", [scheduler_error, *provider_errors])
            raise scheduler_error
        if provider_errors:
            raise ExceptionGroup("provider_cleanup_failed", provider_errors)


PublisherFactory = Callable[[DBPoolBundle], EventPublisherProtocol]


def bootstrap(
    settings: Settings,
    *,
    start_collector: bool = True,
    publisher_factory: PublisherFactory | None = None,
) -> Runtime:
    if not settings.ws_token:
        raise ValueError("ws_token is required in config.yaml")
    telemetry = TelemetryRegistry()
    db: DBPoolBundle | None = None
    providers: WiredProviders | None = None
    try:
        db = DBPoolBundle.create(settings, telemetry=telemetry)
        with db.api_pool.connection() as conn:
            startup_db = postgres_health_check(conn, expected_migration_version=latest_migration_version())
        if not startup_db.get("ok"):
            raise RuntimeError(f"postgres health check failed: {startup_db}")
        providers = wire_providers(
            settings,
            start_collector=start_collector,
        )
        runtime = _assemble_runtime(
            settings=settings,
            db=db,
            telemetry=telemetry,
            providers=providers,
            start_collector=start_collector,
            startup_db_status=startup_db,
            publisher_factory=publisher_factory,
        )
    except Exception as exc:
        for error in _cleanup_provider_roots_sync(providers):
            exc.add_note(f"provider cleanup failed: {type(error).__name__}: {error}")
        if db is not None:
            try:
                _close_db_bundle_sync(db)
            except Exception as cleanup_exc:
                exc.add_note(f"db pool bundle cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}")
        raise
    return runtime


def _assemble_runtime(
    *,
    settings: Settings,
    db: DBPoolBundle,
    telemetry: TelemetryRegistry,
    providers: WiredProviders,
    start_collector: bool,
    startup_db_status: dict[str, object],
    publisher_factory: PublisherFactory | None = None,
) -> Runtime:
    workers = settings.workers
    worker_collector_enabled = bool(
        start_collector and workers.collector.enabled and providers.ingestion.upstream_client_factory is not None
    )
    ingest = _PooledIngestStore(
        db,
        providers=providers.asset_market,
        event_anchor_active_window_ms=workers.event_anchor_backfill.active_window_ms,
    )
    hub = publisher_factory(db) if publisher_factory is not None else _NullEventPublisher()
    collector = CollectorService(
        name="collector",
        settings=workers.collector,
        db=db,
        telemetry=telemetry,
        handles=settings.handles,
        store=ingest,
        publisher=hub,
        upstream_client=None,
    )
    runtime_workers = construct_workers(
        settings=settings,
        db=db,
        telemetry=telemetry,
        providers=providers,
        hub=hub,
        collector=collector,
        collector_enabled=worker_collector_enabled,
        collector_start_requested=start_collector,
    )
    scheduler = WorkerScheduler(workers=runtime_workers, db=db)
    snapshot = RuntimeSnapshot.startup(
        startup_db_status=startup_db_status,
        composition={"ok": True},
    )
    runtime = Runtime(
        settings=settings,
        db=db,
        telemetry=telemetry,
        providers=providers,
        hub=hub,
        collector=collector,
        scheduler=scheduler,
        snapshot=snapshot,
        ingest=ingest,
    )
    if worker_collector_enabled:
        factory = providers.ingestion.upstream_client_factory
        collector.upstream_client = factory(collector.handle_frame) if factory is not None else None
    runtime.current_snapshot()
    return runtime


class _NullEventPublisher:
    async def publish(self, _payload: dict[str, Any]) -> None:
        return None


class _PooledIngestStore:
    def __init__(
        self,
        db: DBPoolBundle,
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

    def ingest_event(self, event: Any, *, is_watched: bool):
        prepared = IngestService.prepare_event(event, is_watched=is_watched)
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
            return ingest.commit_prepared_event(prepared, resolutions=resolutions, captures=captures)


def _ingest_service_for_repos(
    repos: Any,
    *,
    event_anchor_active_window_ms: int,
) -> IngestService:
    return IngestService(
        evidence=repos.evidence,
        entities=repos.entities,
        signals=repos.signals,
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
        token_radar_dirty_targets=repos.token_radar_dirty_targets,
        transaction=repos.transaction,
        event_anchor_active_window_ms=event_anchor_active_window_ms,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _cleanup_runtime_providers(runtime: Runtime) -> list[Exception]:
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


def _close_db_bundle_sync(db: DBPoolBundle) -> None:
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
