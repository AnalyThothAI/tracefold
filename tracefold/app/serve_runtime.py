from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition
from typing import Any
from uuid import UUID, uuid4

from tracefold.app.http.exceptions import ApiUnavailable
from tracefold.app.serve_database import ServeDatabase, ServeDatabaseBusy, ServeRepositories
from tracefold.app.workers.runtime import workers_runtime_status
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.migrations import latest_migration_version
from tracefold.platform.runtime_identity import runtime_identity


class MeasuredOnce:
    """Single-flight measurement with bounded followers and shared failed rounds."""

    def __init__(
        self,
        *,
        ttl_s: float = 30,
        stale_s: float = 60,
        wait_s: float = 2,
        failure_s: float = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._stale_s = stale_s
        self._wait_s = wait_s
        self._failure_s = failure_s
        self._clock = clock
        self._condition = Condition()
        self._flight: _Measurement | None = None
        self._value: dict[str, Any] | None = None
        self._expires_at = 0.0
        self._measured_at = 0.0
        self._failed_until = 0.0

    def get(self, measure: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._condition:
            now = self._clock()
            if self._value is not None and now < self._expires_at:
                return self._value
            if self._flight is not None:
                if self._value is not None and now - self._measured_at <= self._stale_s:
                    return self._value
                flight = self._flight
                if (
                    not self._condition.wait_for(lambda: flight.done, timeout=self._wait_s)
                    or flight.value is None
                    or self._clock() - flight.started_at > self._stale_s
                ):
                    raise ApiUnavailable("service_busy")
                return flight.value
            if now < self._failed_until:
                raise ApiUnavailable("service_busy")
            flight = self._flight = _Measurement(started_at=now)
        try:
            value = measure()
            if self._clock() - now > self._stale_s:
                raise ApiUnavailable("service_busy")
        except BaseException as exc:
            with self._condition:
                flight.done = True
                self._failed_until = self._clock() + self._failure_s
                self._flight = None
                self._condition.notify_all()
            if isinstance(exc, Exception):
                raise ApiUnavailable("service_busy") from exc
            raise
        with self._condition:
            self._value = value
            self._measured_at = now
            self._expires_at = now + self._ttl_s
            flight.value = value
            flight.done = True
            self._flight = None
            self._condition.notify_all()
            return value


@dataclass(slots=True)
class _Measurement:
    started_at: float
    done: bool = False
    value: dict[str, Any] | None = None


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
    news_status: MeasuredOnce = field(default_factory=MeasuredOnce)

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
