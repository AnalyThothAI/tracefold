from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from tracefold.app.repositories import RepositorySession, repositories_for_connection
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.postgres_client import create_pool, with_password_from_file
from tracefold.platform.validation import require_nonnegative_float

_API_STATEMENT_TIMEOUT_SECONDS = 5.0
_WORKER_STATEMENT_TIMEOUT_SECONDS = 30.0
_WORKER_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS = 60.0


class _SyncClosePool(Protocol):
    def close(self) -> None: ...


@dataclass(slots=True)
class DBPoolBundle:
    api_pool: Any
    worker_pool: Any
    telemetry: TelemetryRegistry | None = field(default_factory=TelemetryRegistry)

    @classmethod
    def create(cls, settings: Any, *, telemetry: TelemetryRegistry | None = None) -> DBPoolBundle:
        postgres = settings.storage.postgres
        dsn = with_password_from_file(postgres.dsn, settings.postgres_password_file)
        api_pool_max = max(2, int(postgres.pool_max_size))
        worker_pool_max = max(2, int(postgres.pool_max_size))
        try:
            api_pool = create_pool(
                dsn,
                min_size=1,
                max_size=api_pool_max,
                connect_timeout_seconds=postgres.connect_timeout_seconds,
                application_name="tracefold_api",
                statement_timeout_seconds=_API_STATEMENT_TIMEOUT_SECONDS,
            )
            worker_pool = create_pool(
                dsn,
                min_size=max(0, min(int(postgres.pool_min_size), worker_pool_max)),
                max_size=worker_pool_max,
                connect_timeout_seconds=postgres.connect_timeout_seconds,
                application_name="tracefold_worker",
                statement_timeout_seconds=_WORKER_STATEMENT_TIMEOUT_SECONDS,
                idle_in_transaction_session_timeout_seconds=_WORKER_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            _close_partial_pools(
                exc,
                locals().get("api_pool"),
                locals().get("worker_pool"),
            )
            raise
        return cls(
            api_pool=api_pool,
            worker_pool=worker_pool,
            telemetry=telemetry if telemetry is not None else TelemetryRegistry(),
        )

    @contextmanager
    def api_session(self) -> Iterator[RepositorySession]:
        with self._checkout(self.api_pool, pool_name="api") as conn:
            yield repositories_for_connection(conn)

    @contextmanager
    def worker_session(
        self,
        name: str,
        statement_timeout_seconds: float | None = None,
    ) -> Iterator[RepositorySession]:
        started = time.perf_counter()
        conn = self.worker_pool.getconn()
        self._record_pool_wait("worker", (time.perf_counter() - started) * 1000)
        returned = False
        try:
            _set_config(conn, "application_name", f"worker:{_normalize_worker_name(name)}")
            if statement_timeout_seconds is not None:
                _set_config(conn, "statement_timeout", _statement_timeout_value(statement_timeout_seconds))
            try:
                yield repositories_for_connection(conn)
            except BaseException:
                try:
                    _reset_worker_connection(conn, statement_timeout_seconds=statement_timeout_seconds)
                except Exception:
                    _discard_connection(self.worker_pool, conn)
                    returned = True
                else:
                    self.worker_pool.putconn(conn)
                    returned = True
                raise
            _reset_worker_connection(conn, statement_timeout_seconds=statement_timeout_seconds)
            self.worker_pool.putconn(conn)
            returned = True
        except Exception:
            if not returned:
                _discard_connection(self.worker_pool, conn)
            raise

    async def aclose(self) -> None:
        errors: list[Exception] = []
        for pool in (self.api_pool, self.worker_pool):
            try:
                await _close_pool(pool)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("db_pool_bundle_close_failed", errors)

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


def _statement_timeout_value(seconds: float) -> str:
    timeout_seconds = require_nonnegative_float(
        seconds,
        error_code="db_statement_timeout_seconds_required",
    )
    return f"{int(timeout_seconds * 1000)}ms"


def _set_config(conn: Any, name: str, value: str) -> None:
    conn.execute("SELECT set_config(%s, %s, false)", (str(name), str(value)))


def _reset_worker_connection(conn: Any, *, statement_timeout_seconds: float | None) -> None:
    if statement_timeout_seconds is not None:
        _set_config(conn, "statement_timeout", _statement_timeout_value(_WORKER_STATEMENT_TIMEOUT_SECONDS))
    _set_config(conn, "application_name", "tracefold_worker")


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
