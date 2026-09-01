from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import BoundedSemaphore
from typing import Any
from uuid import UUID, uuid4

from tracefold.app.http.exceptions import ApiUnavailable
from tracefold.app.operator_control import OperatorIntentReceipt, persist_operator_intent
from tracefold.app.repository_session import repositories as open_repositories
from tracefold.app.serve_database import ServeDatabase, ServeDatabaseBusy, ServeRepositories
from tracefold.app.workers.runtime import workers_runtime_status
from tracefold.platform.config.models import Settings
from tracefold.platform.config.secret_file import SecretFileError, read_secure_distinct_token
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.migrations import latest_migration_version
from tracefold.platform.runtime_identity import runtime_identity
from tracefold.trading import PreparedOperatorIntent


@dataclass(slots=True)
class ServeRuntime:
    """PostgreSQL-only HTTP composition."""

    settings: Settings
    db: ServeDatabase
    telemetry: TelemetryRegistry
    runtime_id: UUID
    runtime_revision: str
    image_digest: str
    started_at_ms: int
    operator_command_gate: BoundedSemaphore = field(default_factory=lambda: BoundedSemaphore(1), repr=False)

    @contextmanager
    def repositories(self, *, lane: str = "ordinary") -> Iterator[ServeRepositories]:
        try:
            with self.db.api_session(lane) as repos:
                yield repos
        except ServeDatabaseBusy as exc:
            raise ApiUnavailable("service_busy") from exc

    def status_payload(self, *, now_ms: int | None = None) -> dict[str, Any]:
        measured_at_ms = int(time.time() * 1_000) if now_ms is None else int(now_ms)
        runtime = self._runtime_status_payload(now_ms=measured_at_ms)
        return {"measured_at_ms": measured_at_ms, "runtime": runtime}

    def persist_operator_intent(self, prepared: PreparedOperatorIntent) -> OperatorIntentReceipt:
        """Use one bounded short write transaction outside the read-only serving pool."""

        if not self.operator_command_gate.acquire(timeout=0.050):
            raise ApiUnavailable("operator_command_busy")
        try:
            with (
                open_repositories(
                    self.settings,
                    application_name="tracefold_serve_operator_control",
                ) as repos,
                repos.transaction(),
            ):
                return persist_operator_intent(repos.trading, prepared)
        finally:
            self.operator_command_gate.release()

    def readiness_payload(self, *, now_ms: int | None = None) -> dict[str, Any]:
        measured_at_ms = int(time.time() * 1_000) if now_ms is None else int(now_ms)
        runtime = self._runtime_status_payload(now_ms=measured_at_ms)
        db_status = runtime["db"]
        reasons = [
            reason for reason in runtime["reasons"] if reason in {"database_unavailable", "database_schema_mismatch"}
        ]
        return {
            "ok": bool(db_status["ok"]),
            "reasons": reasons,
            "store": "postgresql",
            "db": db_status,
            "composition": {
                "workers_runtime": runtime["workers_runtime"],
            },
        }

    def _runtime_status_payload(self, *, now_ms: int) -> dict[str, Any]:
        expected_revision = latest_migration_version()
        runtime_query_failed = False
        runtime_row: dict[str, Any] | None = None
        try:
            with self.repositories(lane="control") as repos:
                raw_db = repos.database_health(expected_migration_version=expected_revision)
                if bool(raw_db.get("ok")) or raw_db.get("migration_version") is not None:
                    try:
                        runtime_row = repos.workers_runtime_row()
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
            now_ms=now_ms,
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
            "db": db_status,
            "serve_runtime": {
                "runtime_id": str(self.runtime_id),
                "runtime_revision": self.runtime_revision,
                "image_digest": self.image_digest,
                "started_at_ms": self.started_at_ms,
            },
            "workers_runtime": runtime_status,
        }

    async def aclose(self) -> None:
        await self.db.aclose()


def bootstrap_serve(settings: Settings) -> ServeRuntime:
    if not settings.ws_token:
        raise ValueError("ws_token is required in config.yaml")
    token_path = settings.trading_console_write_token_file()
    if token_path is not None:
        try:
            read_secure_distinct_token(token_path, forbidden_value=settings.ws_token)
        except SecretFileError as exc:
            if exc.code == "conflict":
                raise ValueError("trading_console_write_token_conflicts_with_ws_token") from None
    telemetry = TelemetryRegistry()
    db = ServeDatabase.create(settings, telemetry=telemetry)
    try:
        identity = runtime_identity()
        runtime = ServeRuntime(
            settings=settings,
            db=db,
            telemetry=telemetry,
            runtime_id=uuid4(),
            runtime_revision=identity.runtime_revision,
            image_digest=identity.image_digest,
            started_at_ms=int(time.time() * 1_000),
        )
        readiness = runtime.readiness_payload()
        if readiness["db"]["error_code"] == "database_unavailable":
            raise RuntimeError("postgres health check failed")
        return runtime
    except Exception:
        db.api_pool.close()
        raise


__all__ = ["ServeRuntime", "bootstrap_serve"]
