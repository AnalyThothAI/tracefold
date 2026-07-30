from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import BoundedSemaphore
from typing import Any, Protocol, cast

from psycopg_pool import PoolClosed, PoolTimeout

from tracefold.app.repositories import RepositorySession, repositories_for_connection
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.postgres_client import create_pool, with_password_from_file
from tracefold.platform.validation import require_nonnegative_float

_WORKER_STATEMENT_TIMEOUT_SECONDS = 30.0
_WORKER_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS = 5.0
_BOUNDED_QUERY_CONFIG = {
    "max_parallel_workers_per_gather": "0",
    "jit": "off",
    "work_mem": "16MB",
}
_SERVE_POOL_SIZE = 8
_SERVE_CHECKOUT_TIMEOUT_SECONDS = 0.250
_SERVE_STATEMENT_TIMEOUT_SECONDS = 1.0
_SERVE_SESSION_CONFIG = {
    "jit": "off",
    "max_parallel_workers_per_gather": "0",
    "work_mem": "8MB",
}
_SERVE_LANE_CAPACITIES = {
    "ordinary": 6,
    "search": 1,
    "control": 1,
}
_SERVE_PERMIT_TIMEOUT_SECONDS = 0.050
_WORKER_CHECKOUT_TIMEOUT_SECONDS = 0.250
_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS = (0x54524644, 0)
_STEADY_WORKERS_SINGLETON_LOCK_KEYS = (0x54524644, 1)


class _SyncClosePool(Protocol):
    def close(self) -> None: ...


class ServeDatabaseBusy(RuntimeError):
    pass


@dataclass(slots=True)
class ServeDatabase:
    """The read-only database boundary owned by the public serving runtime."""

    api_pool: Any
    telemetry: TelemetryRegistry | None = field(default_factory=TelemetryRegistry)
    admission: dict[str, BoundedSemaphore] = field(
        default_factory=lambda: {lane: BoundedSemaphore(capacity) for lane, capacity in _SERVE_LANE_CAPACITIES.items()}
    )

    @classmethod
    def create(cls, settings: Any, *, telemetry: TelemetryRegistry | None = None) -> ServeDatabase:
        postgres = settings.storage.postgres
        dsn = with_password_from_file(
            settings.postgres_dsn("serve"),
            settings.postgres_password_file("serve"),
        )
        pool = create_pool(
            dsn,
            min_size=1,
            max_size=_SERVE_POOL_SIZE,
            connect_timeout_seconds=postgres.connect_timeout_seconds,
            application_name="tracefold_serve",
            statement_timeout_seconds=_SERVE_STATEMENT_TIMEOUT_SECONDS,
            lock_timeout_seconds=0.250,
            read_only=True,
            idle_in_transaction_session_timeout_seconds=5.0,
        )
        pool.wait(timeout=float(postgres.connect_timeout_seconds))
        return cls(
            api_pool=pool,
            telemetry=telemetry if telemetry is not None else TelemetryRegistry(),
        )

    @contextmanager
    def api_session(self, lane: str = "ordinary") -> Iterator[RepositorySession]:
        try:
            gate = self.admission[lane]
        except KeyError as exc:
            raise ValueError(f"serve_database_lane_invalid:{lane}") from exc
        started = time.perf_counter()
        if not gate.acquire(timeout=_SERVE_PERMIT_TIMEOUT_SECONDS):
            if self.telemetry is not None:
                self.telemetry.record_pool_wait(
                    f"serve_{lane}_permit",
                    (time.perf_counter() - started) * 1000,
                )
            raise ServeDatabaseBusy(f"serve_database_busy:{lane}")
        try:
            permit_acquired_at = time.perf_counter()
            if self.telemetry is not None:
                self.telemetry.record_pool_wait(
                    f"serve_{lane}_permit",
                    (permit_acquired_at - started) * 1000,
                )
            try:
                with self.api_pool.connection(timeout=_SERVE_CHECKOUT_TIMEOUT_SECONDS) as conn:
                    if self.telemetry is not None:
                        self.telemetry.record_pool_wait(
                            "serve",
                            (time.perf_counter() - permit_acquired_at) * 1000,
                        )
                    for name, value in _SERVE_SESSION_CONFIG.items():
                        _set_config(conn, name, value)
                    yield repositories_for_connection(conn)
            except (PoolClosed, PoolTimeout) as exc:
                raise ServeDatabaseBusy(f"serve_database_pool_busy:{lane}") from exc
        finally:
            gate.release()

    async def aclose(self) -> None:
        await _close_pool(self.api_pool)


@dataclass(slots=True)
class WorkerDatabase:
    """The write-side database boundary owned by the steady worker runtime."""

    worker_pool: Any
    telemetry: TelemetryRegistry | None = field(default_factory=TelemetryRegistry)

    @classmethod
    def create(cls, settings: Any, *, telemetry: TelemetryRegistry | None = None) -> WorkerDatabase:
        postgres = settings.storage.postgres
        dsn = with_password_from_file(
            settings.postgres_dsn("workers"),
            settings.postgres_password_file("workers"),
        )
        worker_pool_max = 12
        try:
            worker_pool = create_pool(
                dsn,
                min_size=1,
                max_size=worker_pool_max,
                connect_timeout_seconds=postgres.connect_timeout_seconds,
                application_name="tracefold_workers",
                statement_timeout_seconds=_WORKER_STATEMENT_TIMEOUT_SECONDS,
                lock_timeout_seconds=0.250,
                idle_in_transaction_session_timeout_seconds=_WORKER_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS,
            )
            worker_pool.wait(timeout=float(postgres.connect_timeout_seconds))
        except Exception as exc:
            _close_partial_pools(
                exc,
                locals().get("worker_pool"),
            )
            raise
        return cls(
            worker_pool=worker_pool,
            telemetry=telemetry if telemetry is not None else TelemetryRegistry(),
        )

    @contextmanager
    def worker_session(
        self,
        name: str,
        statement_timeout_seconds: float | None = None,
    ) -> Iterator[RepositorySession]:
        started = time.perf_counter()
        conn = self.worker_pool.getconn(timeout=_WORKER_CHECKOUT_TIMEOUT_SECONDS)
        self._record_pool_wait("worker", (time.perf_counter() - started) * 1000)
        returned = False
        bounded_query_policy = name == "steady_projection_coordinator"
        try:
            _set_config(conn, "application_name", f"worker:{_normalize_worker_name(name)}")
            if bounded_query_policy:
                _set_worker_query_policy(conn)
            if statement_timeout_seconds is not None:
                _set_config(conn, "statement_timeout", _statement_timeout_value(statement_timeout_seconds))
            try:
                yield repositories_for_connection(
                    conn,
                    transaction_observer=(
                        None
                        if self.telemetry is None
                        else lambda seconds: self.telemetry.record_transaction_seconds(
                            name,
                            seconds,
                        )
                    ),
                )
            except BaseException:
                try:
                    _reset_worker_connection(
                        conn,
                        statement_timeout_seconds=statement_timeout_seconds,
                        bounded_query_policy=bounded_query_policy,
                    )
                except Exception:
                    _discard_connection(self.worker_pool, conn)
                    returned = True
                else:
                    self.worker_pool.putconn(conn)
                    returned = True
                raise
            _reset_worker_connection(
                conn,
                statement_timeout_seconds=statement_timeout_seconds,
                bounded_query_policy=bounded_query_policy,
            )
            self.worker_pool.putconn(conn)
            returned = True
        except Exception:
            if not returned:
                _discard_connection(self.worker_pool, conn)
            raise

    async def aclose(self) -> None:
        await _close_pool(self.worker_pool)

    def acquire_steady_runtime_lock(self) -> Any:
        conn = self.worker_pool.getconn(timeout=_WORKER_CHECKOUT_TIMEOUT_SECONDS)
        try:
            gate_row = conn.execute(
                "SELECT pg_try_advisory_lock_shared(%s, %s) AS acquired",
                _RUNTIME_MAINTENANCE_GATE_LOCK_KEYS,
            ).fetchone()
            if gate_row is None or not bool(gate_row["acquired"]):
                raise RuntimeError("maintenance_runtime_active")
            row = conn.execute(
                "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
                _STEADY_WORKERS_SINGLETON_LOCK_KEYS,
            ).fetchone()
            if row is None or not bool(row["acquired"]):
                conn.execute(
                    "SELECT pg_advisory_unlock_shared(%s, %s)",
                    _RUNTIME_MAINTENANCE_GATE_LOCK_KEYS,
                )
                raise RuntimeError("steady_workers_runtime_already_active")
            conn.commit()
            return conn
        except Exception:
            conn.rollback()
            self.worker_pool.putconn(conn)
            raise

    def release_steady_runtime_lock(self, conn: Any) -> None:
        try:
            conn.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                _STEADY_WORKERS_SINGLETON_LOCK_KEYS,
            )
            conn.execute(
                "SELECT pg_advisory_unlock_shared(%s, %s)",
                _RUNTIME_MAINTENANCE_GATE_LOCK_KEYS,
            )
            conn.commit()
        finally:
            self.worker_pool.putconn(conn)

    def acquire_maintenance_runtime_lock(self) -> Any:
        conn = self.worker_pool.getconn(timeout=_WORKER_CHECKOUT_TIMEOUT_SECONDS)
        try:
            acquire_maintenance_advisory_lock(conn)
            conn.commit()
            return conn
        except Exception:
            conn.rollback()
            self.worker_pool.putconn(conn)
            raise

    def release_maintenance_runtime_lock(self, conn: Any) -> None:
        try:
            release_maintenance_advisory_lock(conn)
            conn.commit()
        finally:
            self.worker_pool.putconn(conn)

    @contextmanager
    def _checkout(self, pool: Any, *, pool_name: str) -> Iterator[Any]:
        started = time.perf_counter()
        context = pool.connection()
        conn = context.__enter__()
        self._record_pool_wait(pool_name, (time.perf_counter() - started) * 1000)
        clean_exit = False
        try:
            yield conn
            clean_exit = True
        except BaseException as exc:
            context.__exit__(type(exc), exc, exc.__traceback__)
            raise
        finally:
            if clean_exit:
                context.__exit__(None, None, None)

    def _record_pool_wait(self, pool_name: str, wait_ms: float) -> None:
        if self.telemetry is not None:
            self.telemetry.record_pool_wait(pool_name, wait_ms)


def _normalize_worker_name(name: str) -> str:
    return str(name).strip().replace(" ", "_") or "unknown"


def acquire_maintenance_advisory_lock(conn: Any) -> None:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
        _RUNTIME_MAINTENANCE_GATE_LOCK_KEYS,
    ).fetchone()
    if row is None or not bool(row["acquired"]):
        raise RuntimeError("steady_workers_runtime_active")


def release_maintenance_advisory_lock(conn: Any) -> None:
    row = conn.execute(
        "SELECT pg_advisory_unlock(%s, %s) AS released",
        _RUNTIME_MAINTENANCE_GATE_LOCK_KEYS,
    ).fetchone()
    if row is None or not bool(row["released"]):
        raise RuntimeError("maintenance_runtime_lock_not_owned")


def _statement_timeout_value(seconds: float) -> str:
    timeout_seconds = require_nonnegative_float(
        seconds,
        error_code="db_statement_timeout_seconds_required",
    )
    return f"{int(timeout_seconds * 1000)}ms"


def _set_config(conn: Any, name: str, value: str) -> None:
    conn.execute("SELECT set_config(%s, %s, false)", (str(name), str(value)))


def _set_worker_query_policy(conn: Any) -> None:
    for name, value in _BOUNDED_QUERY_CONFIG.items():
        _set_config(conn, name, value)


def _reset_worker_connection(
    conn: Any,
    *,
    statement_timeout_seconds: float | None,
    bounded_query_policy: bool,
) -> None:
    if statement_timeout_seconds is not None:
        _set_config(conn, "statement_timeout", _statement_timeout_value(_WORKER_STATEMENT_TIMEOUT_SECONDS))
    if bounded_query_policy:
        for name in _BOUNDED_QUERY_CONFIG:
            _reset_config(conn, name)
    _set_config(conn, "application_name", "tracefold_worker")


def _reset_config(conn: Any, name: str) -> None:
    if name not in _BOUNDED_QUERY_CONFIG:
        raise ValueError("db_reset_config_name_invalid")
    conn.execute(f"RESET {name}")


def _discard_connection(pool: Any, conn: Any) -> None:
    conn.close()
    pool.putconn(conn)


def _close_partial_pools(error: BaseException, *pools: object | None) -> None:
    seen: set[int] = set()
    for pool in pools:
        if pool is None or id(pool) in seen:
            continue
        seen.add(id(pool))
        try:
            cast(_SyncClosePool, pool).close()
        except Exception as exc:
            error.add_note(f"partial db pool cleanup failed: {type(exc).__name__}: {exc}")


async def _close_pool(pool: Any) -> None:
    result = pool.close()
    if result is not None:
        raise RuntimeError("db_pool_close_must_be_sync")
