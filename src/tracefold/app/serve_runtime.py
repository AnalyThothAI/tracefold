from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from tracefold.app.database import ServeDatabase, ServeDatabaseBusy
from tracefold.app.http.exceptions import ApiUnavailable
from tracefold.app.http.ws import PersistedLiveBroadcaster
from tracefold.app.repositories import RepositorySession
from tracefold.app.workers_runtime import WorkersRuntimeRepository, workers_runtime_status
from tracefold.platform.config.settings import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.postgres_client import postgres_health_check
from tracefold.platform.postgres.postgres_migrations import latest_migration_version


@dataclass(slots=True)
class ServeRuntime:
    """PostgreSQL-only read-side composition."""

    settings: Settings
    db: ServeDatabase
    telemetry: TelemetryRegistry
    hub: PersistedLiveBroadcaster

    @contextmanager
    def repositories(self, *, lane: str = "ordinary") -> Iterator[RepositorySession]:
        try:
            with self.db.api_session(lane) as repos:
                yield repos
        except ServeDatabaseBusy as exc:
            raise ApiUnavailable("service_busy") from exc

    def status_payload(self, *, now_ms: int | None = None) -> dict[str, Any]:
        measured_at_ms = int(time.time() * 1_000) if now_ms is None else int(now_ms)
        expected_revision = latest_migration_version()
        runtime_query_failed = False
        runtime_row: dict[str, Any] | None = None
        try:
            with self.repositories(lane="control") as repos:
                raw_db = postgres_health_check(
                    repos.conn,
                    expected_migration_version=expected_revision,
                )
                if bool(raw_db.get("ok")) or raw_db.get("migration_version") is not None:
                    try:
                        runtime_row = WorkersRuntimeRepository(repos.conn).read()
                    except Exception:
                        runtime_query_failed = True
        except Exception:
            raw_db = {"ok": False}
            runtime_query_failed = True

        current_revision = raw_db.get("migration_version")
        connected = current_revision is not None or bool(raw_db.get("ok"))
        schema_ok = connected and current_revision == expected_revision
        db_error = None
        if not connected:
            db_error = "database_unavailable"
        elif not schema_ok:
            db_error = "schema_mismatch"
        db_status = {
            "ok": connected and schema_ok,
            "schema_ok": schema_ok,
            "current_revision": str(current_revision) if current_revision is not None else None,
            "expected_revision": expected_revision,
            "error_code": db_error,
        }
        runtime_status = workers_runtime_status(
            runtime_row,
            now_ms=measured_at_ms,
            query_failed=runtime_query_failed,
        )
        reasons: list[str] = []
        if not connected:
            reasons.append("database_unavailable")
        elif not schema_ok:
            reasons.append("database_schema_mismatch")
        runtime_reason = runtime_status["unavailable_reason"]
        if runtime_reason is not None:
            reasons.append(str(runtime_reason))
        return {
            "ok": bool(db_status["ok"]) and runtime_status["state"] == "running",
            "reasons": reasons,
            "measured_at_ms": measured_at_ms,
            "db": db_status,
            "workers_runtime": runtime_status,
        }

    async def aclose(self) -> None:
        await self.hub.aclose()
        await self.db.aclose()


def bootstrap_serve(settings: Settings) -> ServeRuntime:
    if not settings.ws_token:
        raise ValueError("ws_token is required in config.yaml")
    telemetry = TelemetryRegistry()
    db = ServeDatabase.create(settings, telemetry=telemetry)
    try:
        runtime = ServeRuntime(
            settings=settings,
            db=db,
            telemetry=telemetry,
            hub=PersistedLiveBroadcaster(
                token=settings.ws_token,
                repository_session=lambda: db.api_session("control"),
                default_replay_limit=settings.api.replay_limit,
            ),
        )
        status = runtime.status_payload()
        if status["db"]["error_code"] == "database_unavailable":
            raise RuntimeError("postgres health check failed")
        return runtime
    except Exception:
        db.api_pool.close()
        raise


__all__ = ["ServeRuntime", "bootstrap_serve"]
