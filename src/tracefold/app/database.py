from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from threading import BoundedSemaphore
from typing import Any, Protocol, cast

from psycopg import OperationalError
from psycopg.errors import IdleInTransactionSessionTimeout, LockNotAvailable, QueryCanceled, TransactionTimeout
from psycopg_pool import PoolClosed, PoolTimeout

from tracefold.app.repositories import RepositorySession, repositories_for_connection
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.postgres_client import create_pool, with_password_from_file
from tracefold.platform.resource import (
    ResourceAdmissionTimeout,
    await_concurrent_future,
)
from tracefold.platform.validation import require_nonnegative_float

_WORKER_STATEMENT_TIMEOUT_SECONDS = 3.0
_WORKER_CONNECTION_BASE_STATEMENT_TIMEOUT_SECONDS = 0.5
_WORKER_TRANSACTION_TIMEOUT_MARGIN_SECONDS = 5.0
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
WORKER_DATABASE_LOCK_TIMEOUT_SECONDS = 0.250
_WORKER_ADMISSION_TIMEOUT_SECONDS = 1.0
_WORKER_BUSINESS_OPERATION_COMPLETION_GRACE_SECONDS = 5.0
_WORKER_CONTROL_OPERATION_COMPLETION_GRACE_SECONDS = 5.0
_WORKER_POOL_MIN_SIZE = 1
_WORKER_POOL_MAX_SIZE = 4
_WORKER_POOL_MAX_WAITING = 3
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
    """One Workers pool with two business slots and one control slot."""

    worker_pool: Any
    telemetry: TelemetryRegistry | None = field(default_factory=TelemetryRegistry)
    _business_executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="tracefold-business-db",
        )
    )
    _control_executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tracefold-control-db",
        )
    )
    _business_gate: asyncio.BoundedSemaphore = field(default_factory=lambda: asyncio.BoundedSemaphore(2))
    _control_gate: asyncio.BoundedSemaphore = field(default_factory=lambda: asyncio.BoundedSemaphore(1))
    _pending_business: set[asyncio.Future[Any]] = field(default_factory=set)
    _pending_control: set[asyncio.Future[Any]] = field(default_factory=set)
    _accepting_business: bool = True
    _accepting_control: bool = True
    _executors_closed: bool = False

    @classmethod
    def create(cls, settings: Any, *, telemetry: TelemetryRegistry | None = None) -> WorkerDatabase:
        postgres = settings.storage.postgres
        dsn = with_password_from_file(
            settings.postgres_dsn("workers"),
            settings.postgres_password_file("workers"),
        )
        try:
            worker_pool = create_pool(
                dsn,
                min_size=_WORKER_POOL_MIN_SIZE,
                max_size=_WORKER_POOL_MAX_SIZE,
                max_waiting=_WORKER_POOL_MAX_WAITING,
                connect_timeout_seconds=postgres.connect_timeout_seconds,
                application_name="tracefold_workers",
                statement_timeout_seconds=_WORKER_CONNECTION_BASE_STATEMENT_TIMEOUT_SECONDS,
                lock_timeout_seconds=WORKER_DATABASE_LOCK_TIMEOUT_SECONDS,
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

    async def run_business[T](
        self,
        operation_name: str,
        function: Callable[..., T],
        /,
        *args: Any,
        operation_timeout_seconds: float,
        on_submitted: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> T:
        if not self._accepting_business or self._executors_closed:
            raise RuntimeError("worker_database_business_closed")
        return await self._run_executor(
            operation_name,
            function,
            args,
            kwargs,
            executor=self._business_executor,
            gate=self._business_gate,
            pending=self._pending_business,
            capability="database_business",
            operation_timeout_seconds=operation_timeout_seconds,
            on_submitted=on_submitted,
        )

    async def run_control[T](
        self,
        operation_name: str,
        function: Callable[..., T],
        /,
        *args: Any,
        operation_timeout_seconds: float = 1.0,
        allow_shutdown: bool = False,
        on_submitted: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> T:
        if (not self._accepting_control and not allow_shutdown) or self._executors_closed:
            raise RuntimeError("worker_database_control_closed")
        return await self._run_executor(
            operation_name,
            function,
            args,
            kwargs,
            executor=self._control_executor,
            gate=self._control_gate,
            pending=self._pending_control,
            capability="database_control",
            operation_timeout_seconds=operation_timeout_seconds,
            on_submitted=on_submitted,
        )

    async def _run_executor[T](
        self,
        operation_name: str,
        function: Callable[..., T],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        executor: ThreadPoolExecutor,
        gate: asyncio.BoundedSemaphore,
        pending: set[asyncio.Future[Any]],
        capability: str,
        operation_timeout_seconds: float,
        on_submitted: Callable[[], None] | None,
    ) -> T:
        started = time.perf_counter()
        try:
            await asyncio.wait_for(
                gate.acquire(),
                timeout=_WORKER_ADMISSION_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            self._record_pool_wait(
                "worker_db_admission",
                (time.perf_counter() - started) * 1000,
            )
            self._record_resource_admission(
                capability,
                operation_name,
                "timeout",
                time.perf_counter() - started,
            )
            raise ResourceAdmissionTimeout(
                f"worker_database_admission_timeout:{_normalize_operation_name(operation_name)}"
            ) from exc
        self._record_pool_wait(
            "worker_db_admission",
            (time.perf_counter() - started) * 1000,
        )
        self._record_resource_admission(
            capability,
            operation_name,
            "accepted",
            time.perf_counter() - started,
        )
        loop = asyncio.get_running_loop()
        submitted_at = time.perf_counter()
        try:
            underlying = executor.submit(partial(function, *args, **kwargs))
        except BaseException:
            gate.release()
            raise
        wrapped = asyncio.wrap_future(underlying)
        pending.add(wrapped)
        self._change_resource_active(capability, 1)
        _release_db_permit_on_completion(
            underlying,
            loop=loop,
            wrapped=wrapped,
            pending=pending,
            gate=gate,
            completed=lambda future: self._record_resource_completion(
                capability,
                operation_name,
                submitted_at,
                future,
            ),
        )
        if on_submitted is not None:
            on_submitted()
        try:
            return await await_concurrent_future(
                underlying,
                wrapped,
                timeout_seconds=(
                    float(operation_timeout_seconds)
                    + (
                        _WORKER_BUSINESS_OPERATION_COMPLETION_GRACE_SECONDS
                        if capability == "database_business"
                        else _WORKER_CONTROL_OPERATION_COMPLETION_GRACE_SECONDS
                    )
                ),
                overrun_code=f"resource_operation_overrun:db:{_normalize_operation_name(operation_name)}",
            )
        except LockNotAvailable as exc:
            raise ResourceAdmissionTimeout(
                f"worker_database_lock_timeout:{_normalize_operation_name(operation_name)}"
            ) from exc
        except QueryCanceled as exc:
            raise ResourceAdmissionTimeout(
                f"worker_database_statement_timeout:{_normalize_operation_name(operation_name)}"
            ) from exc
        except TransactionTimeout as exc:
            raise ResourceAdmissionTimeout(
                f"worker_database_transaction_timeout:{_normalize_operation_name(operation_name)}"
            ) from exc
        except IdleInTransactionSessionTimeout as exc:
            if capability != "database_business":
                raise
            raise ResourceAdmissionTimeout(
                f"worker_database_idle_transaction_timeout:{_normalize_operation_name(operation_name)}"
            ) from exc
        except PoolTimeout as exc:
            if capability != "database_business":
                raise
            raise ResourceAdmissionTimeout(
                f"worker_database_pool_timeout:{_normalize_operation_name(operation_name)}"
            ) from exc
        except OperationalError as exc:
            if capability != "database_business":
                raise
            raise ResourceAdmissionTimeout(
                f"worker_database_connection_lost:{_normalize_operation_name(operation_name)}"
            ) from exc

    def close_business_admission(self) -> None:
        self._accepting_business = False

    def close_control_admission(self) -> None:
        self._accepting_control = False

    async def drain_business(self, *, timeout_seconds: float) -> bool:
        return await _drain_db_futures(
            self._pending_business,
            timeout_seconds=timeout_seconds,
        )

    async def drain_control(self, *, timeout_seconds: float) -> bool:
        return await _drain_db_futures(
            self._pending_control,
            timeout_seconds=timeout_seconds,
        )

    def close_executors(self) -> None:
        if self._executors_closed:
            return
        self._executors_closed = True
        self._accepting_business = False
        self._accepting_control = False
        self._business_executor.shutdown(wait=False, cancel_futures=False)
        self._control_executor.shutdown(wait=False, cancel_futures=False)

    def prewarm_control_connection(self) -> None:
        with self.worker_pool.connection(timeout=_WORKER_CHECKOUT_TIMEOUT_SECONDS) as conn:
            row = conn.execute("SELECT 1 AS ok").fetchone()
            if row is None or int(row["ok"]) != 1:
                raise RuntimeError("worker_control_connection_prewarm_failed")

    @staticmethod
    def check_pinned_liveness(conn: Any) -> None:
        row = conn.execute("SELECT 1 AS ok").fetchone()
        if row is None or int(row["ok"]) != 1:
            raise RuntimeError("singleton_lost")

    @contextmanager
    def worker_session(
        self,
        name: str,
        statement_timeout_seconds: float | None = None,
        transaction_timeout_seconds: float | None = None,
    ) -> Iterator[RepositorySession]:
        started = time.perf_counter()
        conn = self.worker_pool.getconn(timeout=_WORKER_CHECKOUT_TIMEOUT_SECONDS)
        self._record_pool_wait("worker", (time.perf_counter() - started) * 1000)
        projection_transitions: list[tuple[str, str]] = []
        try:
            with conn.transaction():
                _set_worker_operation_config(
                    conn,
                    operation_name=name,
                    statement_timeout_seconds=statement_timeout_seconds,
                    transaction_timeout_seconds=transaction_timeout_seconds,
                )
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
                    projection_transitions=projection_transitions,
                )
        except BaseException:
            if bool(getattr(conn, "closed", False)):
                _discard_connection(self.worker_pool, conn)
            else:
                self.worker_pool.putconn(conn)
            raise
        else:
            self.worker_pool.putconn(conn)
            if self.telemetry is not None:
                for (domain, transition), count in Counter(projection_transitions).items():
                    self.telemetry.record_projection_transition(domain, transition, count)

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

    def _record_resource_admission(
        self,
        capability: str,
        operation_name: str,
        outcome: str,
        seconds: float,
    ) -> None:
        if self.telemetry is not None:
            self.telemetry.record_resource_admission(
                capability,
                _normalize_operation_name(operation_name),
                outcome,
                seconds,
            )

    def _record_resource_completion(
        self,
        capability: str,
        operation_name: str,
        submitted_at: float,
        future: Future[Any],
    ) -> None:
        if self.telemetry is None:
            return
        outcome = "cancelled" if future.cancelled() else "error" if future.exception() is not None else "success"
        self.telemetry.record_resource_service(
            capability,
            _normalize_operation_name(operation_name),
            outcome,
            max(0.0, time.perf_counter() - submitted_at),
        )
        self.telemetry.change_resource_active(capability, -1)

    def _change_resource_active(self, capability: str, delta: int) -> None:
        if self.telemetry is not None:
            self.telemetry.change_resource_active(capability, delta)


def _normalize_operation_name(name: str) -> str:
    return (str(name).strip().replace(" ", "_") or "unknown")[:96]


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


def _transaction_timeout_value(seconds: float) -> str:
    timeout_seconds = require_nonnegative_float(
        seconds,
        error_code="db_transaction_timeout_seconds_required",
    )
    return f"{int(timeout_seconds * 1000)}ms"


def _set_config(conn: Any, name: str, value: str) -> None:
    conn.execute("SELECT set_config(%s, %s, false)", (str(name), str(value)))


def _set_configs(conn: Any, values: dict[str, str], *, local: bool = False) -> None:
    if not values:
        return
    scope = "true" if local else "false"
    expressions = ", ".join(f"set_config(%s, %s, {scope})" for _ in values)
    params = tuple(part for item in values.items() for part in item)
    conn.execute(f"SELECT {expressions}", params)


def _set_worker_operation_config(
    conn: Any,
    *,
    operation_name: str,
    statement_timeout_seconds: float | None,
    transaction_timeout_seconds: float | None,
) -> None:
    effective_statement_timeout_seconds = (
        _WORKER_STATEMENT_TIMEOUT_SECONDS if statement_timeout_seconds is None else statement_timeout_seconds
    )
    effective_transaction_timeout_seconds = (
        effective_statement_timeout_seconds + _WORKER_TRANSACTION_TIMEOUT_MARGIN_SECONDS
        if transaction_timeout_seconds is None
        else transaction_timeout_seconds
    )
    _set_configs(
        conn,
        {
            **_BOUNDED_QUERY_CONFIG,
            "application_name": f"tracefold_workers:{_normalize_operation_name(operation_name)}",
            "statement_timeout": _statement_timeout_value(effective_statement_timeout_seconds),
            "transaction_timeout": _transaction_timeout_value(effective_transaction_timeout_seconds),
        },
        local=True,
    )


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


def _release_db_permit_on_completion(
    underlying: Future[Any],
    *,
    loop: asyncio.AbstractEventLoop,
    wrapped: asyncio.Future[Any],
    pending: set[asyncio.Future[Any]],
    gate: asyncio.BoundedSemaphore,
    completed: Callable[[Future[Any]], None] | None = None,
) -> None:
    def on_done(future: Future[Any]) -> None:
        def finalize() -> None:
            pending.discard(wrapped)
            gate.release()
            if completed is not None:
                completed(future)

        loop.call_soon_threadsafe(finalize)

    underlying.add_done_callback(on_done)


async def _drain_db_futures(
    pending: set[asyncio.Future[Any]],
    *,
    timeout_seconds: float,
) -> bool:
    active = {future for future in pending if not future.done()}
    if not active:
        return True
    _, unfinished = await asyncio.wait(
        active,
        timeout=max(0.0, float(timeout_seconds)),
    )
    return not unfinished
