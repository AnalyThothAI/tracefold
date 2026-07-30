from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from tracefold.app.database import ServeDatabase, ServeDatabaseBusy
from tracefold.app.http.exceptions import ApiUnavailable
from tracefold.app.http.ws import PersistedLiveBroadcaster
from tracefold.app.repositories import RepositorySession
from tracefold.app.runtime_state import RuntimeSnapshot
from tracefold.app.worker_runtime_status import (
    WorkerRuntimeStatusRepository,
    unavailable_worker_statuses,
)
from tracefold.platform.config.settings import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.postgres_client import postgres_health_check
from tracefold.platform.postgres.postgres_migrations import latest_migration_version


@dataclass(slots=True)
class ServeRuntime:
    """Read-side composition. It cannot construct providers or workers."""

    settings: Settings
    db: ServeDatabase
    telemetry: TelemetryRegistry
    hub: PersistedLiveBroadcaster
    snapshot: RuntimeSnapshot
    role: str = "serve"

    @contextmanager
    def repositories(self, *, lane: str = "ordinary") -> Iterator[RepositorySession]:
        try:
            with self.db.api_session(lane) as repos:
                yield repos
        except ServeDatabaseBusy as exc:
            raise ApiUnavailable("service_busy") from exc

    def current_snapshot(self) -> RuntimeSnapshot:
        try:
            with self.repositories(lane="control") as repos:
                workers = WorkerRuntimeStatusRepository(repos.conn).read_current(now_ms=_now_ms())
        except Exception:
            workers = unavailable_worker_statuses("worker_status_query_failed")
        reasons = tuple(
            f"worker:{name}:{status['effective_status']}"
            for name, status in workers.items()
            if status["effective_status"] in {"unavailable", "degraded", "failed"}
        )
        self.snapshot = RuntimeSnapshot(
            workers=workers,
            collector={},
            provider_states={},
            startup_db_status=dict(self.snapshot.startup_db_status),
            composition=dict(self.snapshot.composition),
            degradation_reasons=reasons,
        )
        return self.snapshot

    async def aclose(self) -> None:
        await self.hub.aclose()
        await self.db.aclose()


def bootstrap_serve(settings: Settings) -> ServeRuntime:
    if not settings.ws_token:
        raise ValueError("ws_token is required in config.yaml")
    telemetry = TelemetryRegistry()
    db = ServeDatabase.create(settings, telemetry=telemetry)
    try:
        with db.api_session("control") as repos:
            startup_db = postgres_health_check(
                repos.conn,
                expected_migration_version=latest_migration_version(),
            )
        if not startup_db.get("ok"):
            raise RuntimeError(f"postgres health check failed: {startup_db}")
        snapshot = RuntimeSnapshot.startup(
            startup_db_status=startup_db,
            composition={"ok": True, "runtime_role": "serve"},
        )
        return ServeRuntime(
            settings=settings,
            db=db,
            telemetry=telemetry,
            hub=PersistedLiveBroadcaster(
                token=settings.ws_token,
                repository_session=lambda: db.api_session("control"),
                default_replay_limit=settings.api.replay_limit,
            ),
            snapshot=snapshot,
        )
    except Exception:
        db.api_pool.close()
        raise


def _now_ms() -> int:
    return int(time.time() * 1000)
