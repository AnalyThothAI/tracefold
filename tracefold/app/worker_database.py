from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol, cast

from psycopg import OperationalError
from psycopg.errors import IdleInTransactionSessionTimeout, LockNotAvailable, QueryCanceled, TransactionTimeout
from psycopg_pool import PoolTimeout

from tracefold.app.repository_session import RepositorySession, repositories_for_connection
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.postgres.client import create_pool, with_password_from_file
from tracefold.platform.postgres.maintenance_gate import acquire_steady_gate, release_steady_gate
from tracefold.platform.resource import (
    ResourceAdmissionTimeout,
    ResourceCapability,
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
_WORKER_CHECKOUT_TIMEOUT_SECONDS = 0.250
WORKER_DATABASE_LOCK_TIMEOUT_SECONDS = 0.250
_WORKER_ADMISSION_TIMEOUT_SECONDS = 1.0
# One bounded heavy DB operation may legitimately consume its full native
# transaction envelope. Wait for that physical slot before competing for one
# of the two general business slots.
_WORKER_HEAVY_ADMISSION_TIMEOUT_SECONDS = 16.0
# Stay beyond worker_session's transaction deadline so PostgreSQL's native
# timeout classification wins before the outer executor envelope.
_WORKER_BUSINESS_OPERATION_COMPLETION_GRACE_SECONDS = 6.0
_WORKER_CONTROL_OPERATION_COMPLETION_GRACE_SECONDS = 6.0
# The singleton lock pins one connection; pre-open a second for the control lane.
_WORKER_POOL_MIN_SIZE = 2
# 1 steady singleton lock + 2 business slots + 4 News-lane slots + 1 control slot.
_WORKER_POOL_MAX_SIZE = 8
_WORKER_POOL_MAX_WAITING = 3
_NEWS_LANE_WIDTH = 4
_STEADY_WORKERS_SINGLETON_LOCK_KEYS = (0x54524644, 1)


class _SyncClosePool(Protocol):
    def close(self) -> None: ...


@dataclass(slots=True)
class WorkerDatabase:
    """One Workers pool with two business slots, a four-slot News lane, and one control slot."""

    worker_pool: Any
    # Every process that owns this pool owns a registry too, and `/metrics` is the only reader of
    # either. An optional registry only bought fourteen `is not None` branches no caller reached
    # (#589 P-F14).
    telemetry: TelemetryRegistry
    _business_executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="tracefold-business-db",
        )
    )
    _news_executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=_NEWS_LANE_WIDTH,
            thread_name_prefix="tracefold-news-db",
        )
    )
    _control_executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tracefold-control-db",
        )
    )
    _business_gate: asyncio.BoundedSemaphore = field(default_factory=lambda: asyncio.BoundedSemaphore(2))
    _heavy_business_gate: asyncio.BoundedSemaphore = field(default_factory=lambda: asyncio.BoundedSemaphore(1))
    _news_gate: asyncio.BoundedSemaphore = field(default_factory=lambda: asyncio.BoundedSemaphore(_NEWS_LANE_WIDTH))
    _control_gate: asyncio.BoundedSemaphore = field(default_factory=lambda: asyncio.BoundedSemaphore(1))
    _pending_business: set[asyncio.Future[Any]] = field(default_factory=set)
    _pending_control: set[asyncio.Future[Any]] = field(default_factory=set)
    _accepting_business: bool = True
    _accepting_control: bool = True
    _executors_closed: bool = False

    @classmethod
    def create(cls, settings: Any, *, telemetry: TelemetryRegistry) -> WorkerDatabase:
        postgres = settings.storage.postgres
        dsn = with_password_from_file(
            postgres.dsn,
            settings.postgres_password_file(),
        )
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
        # There is one pool, and it exists by the time anything can fail: a `create_pool` that raises
        # left nothing open, and only the first connection wait can strand it (#589 P-F16).
        try:
            worker_pool.wait(timeout=float(postgres.connect_timeout_seconds))
        except Exception as exc:
            try:
                cast(_SyncClosePool, worker_pool).close()
            except Exception as close_exc:
                exc.add_note(f"partial db pool cleanup failed: {type(close_exc).__name__}: {close_exc}")
            raise
        return cls(worker_pool=worker_pool, telemetry=telemetry)

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
            gates=((self._business_gate, _WORKER_ADMISSION_TIMEOUT_SECONDS),),
            pending=self._pending_business,
            capability=ResourceCapability.DATABASE_BUSINESS,
            operation_timeout_seconds=operation_timeout_seconds,
            on_submitted=on_submitted,
        )

    async def run_news[T](
        self,
        operation_name: str,
        function: Callable[..., T],
        /,
        *args: Any,
        operation_timeout_seconds: float,
        on_submitted: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> T:
        """The News consumers' own DB lane, separate from the control lane."""

        if not self._accepting_business or self._executors_closed:
            raise RuntimeError("worker_database_business_closed")
        return await self._run_executor(
            operation_name,
            function,
            args,
            kwargs,
            executor=self._news_executor,
            gates=((self._news_gate, _WORKER_ADMISSION_TIMEOUT_SECONDS),),
            pending=self._pending_business,
            capability=ResourceCapability.DATABASE_BUSINESS,
            operation_timeout_seconds=operation_timeout_seconds,
            on_submitted=on_submitted,
        )

    def heavy_business(self) -> _HeavyBusinessDatabase:
        """Share the business pool while limiting measured heavy DB work to one slot."""

        return _HeavyBusinessDatabase(self)

    async def _run_heavy_business[T](
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
            gates=(
                (self._heavy_business_gate, _WORKER_HEAVY_ADMISSION_TIMEOUT_SECONDS),
                (self._business_gate, _WORKER_ADMISSION_TIMEOUT_SECONDS),
            ),
            pending=self._pending_business,
            capability=ResourceCapability.DATABASE_BUSINESS,
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
            gates=((self._control_gate, _WORKER_ADMISSION_TIMEOUT_SECONDS),),
            pending=self._pending_control,
            capability=ResourceCapability.DATABASE_CONTROL,
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
        gates: tuple[tuple[asyncio.BoundedSemaphore, float], ...],
        pending: set[asyncio.Future[Any]],
        capability: ResourceCapability,
        operation_timeout_seconds: float,
        on_submitted: Callable[[], None] | None,
    ) -> T:
        started = time.perf_counter()
        try:
            acquired_gates = await _acquire_db_gates(gates)
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
            _release_db_gates(acquired_gates)
            raise
        wrapped = asyncio.wrap_future(underlying)
        pending.add(wrapped)
        self._change_resource_active(capability, 1)
        _release_db_permit_on_completion(
            underlying,
            loop=loop,
            wrapped=wrapped,
            pending=pending,
            gates=acquired_gates,
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
                        if capability is ResourceCapability.DATABASE_BUSINESS
                        else _WORKER_CONTROL_OPERATION_COMPLETION_GRACE_SECONDS
                    )
                ),
                capability=capability,
                operation_name=_normalize_operation_name(operation_name),
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
            if capability is not ResourceCapability.DATABASE_BUSINESS:
                raise
            raise ResourceAdmissionTimeout(
                f"worker_database_idle_transaction_timeout:{_normalize_operation_name(operation_name)}"
            ) from exc
        except PoolTimeout as exc:
            if capability is not ResourceCapability.DATABASE_BUSINESS:
                raise
            raise ResourceAdmissionTimeout(
                f"worker_database_pool_timeout:{_normalize_operation_name(operation_name)}"
            ) from exc
        except OperationalError as exc:
            if capability is not ResourceCapability.DATABASE_BUSINESS:
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
        self._news_executor.shutdown(wait=False, cancel_futures=False)
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
        telemetry = self.telemetry
        started = time.perf_counter()
        conn = self.worker_pool.getconn(timeout=_WORKER_CHECKOUT_TIMEOUT_SECONDS)
        self._record_pool_wait("worker", (time.perf_counter() - started) * 1000)
        transaction_started = time.perf_counter()
        try:
            with conn.transaction():
                _set_worker_operation_config(
                    conn,
                    statement_timeout_seconds=statement_timeout_seconds,
                    transaction_timeout_seconds=transaction_timeout_seconds,
                )
                yield repositories_for_connection(conn)
        except BaseException:
            if bool(getattr(conn, "closed", False)):
                _discard_connection(self.worker_pool, conn)
            else:
                self.worker_pool.putconn(conn)
            raise
        else:
            self.worker_pool.putconn(conn)
        finally:
            telemetry.record_transaction_seconds(
                name,
                max(0.0, time.perf_counter() - transaction_started),
            )

    async def aclose(self) -> None:
        await _close_pool(self.worker_pool)

    def acquire_steady_runtime_lock(self) -> Any:
        conn = self.worker_pool.getconn(timeout=_WORKER_CHECKOUT_TIMEOUT_SECONDS)
        try:
            acquire_steady_gate(conn)
            row = conn.execute(
                "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
                _STEADY_WORKERS_SINGLETON_LOCK_KEYS,
            ).fetchone()
            if row is None or not bool(row["acquired"]):
                release_steady_gate(conn)
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
            release_steady_gate(conn)
            conn.commit()
        finally:
            self.worker_pool.putconn(conn)

    def _record_pool_wait(self, pool_name: str, wait_ms: float) -> None:
        self.telemetry.record_pool_wait(pool_name, wait_ms)

    def _record_resource_admission(
        self,
        capability: ResourceCapability,
        operation_name: str,
        outcome: str,
        seconds: float,
    ) -> None:
        self.telemetry.record_resource_admission(
            capability.value,
            _normalize_operation_name(operation_name),
            outcome,
            seconds,
        )

    def _record_resource_completion(
        self,
        capability: ResourceCapability,
        operation_name: str,
        submitted_at: float,
        future: Future[Any],
    ) -> None:
        outcome = "cancelled" if future.cancelled() else "error" if future.exception() is not None else "success"
        self.telemetry.record_resource_service(
            capability.value,
            _normalize_operation_name(operation_name),
            outcome,
            max(0.0, time.perf_counter() - submitted_at),
        )
        self.telemetry.change_resource_active(capability.value, -1)

    def _change_resource_active(self, capability: ResourceCapability, delta: int) -> None:
        self.telemetry.change_resource_active(capability.value, delta)


@dataclass(frozen=True, slots=True)
class _HeavyBusinessDatabase:
    """Internal adapter for DB work proven capable of monopolizing a slot."""

    database: WorkerDatabase

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
        return await self.database._run_heavy_business(
            operation_name,
            function,
            *args,
            operation_timeout_seconds=operation_timeout_seconds,
            on_submitted=on_submitted,
            **kwargs,
        )


def _normalize_operation_name(name: str) -> str:
    return (str(name).strip().replace(" ", "_") or "unknown")[:96]


def _timeout_value(seconds: float, *, error_code: str) -> str:
    timeout_seconds = require_nonnegative_float(seconds, error_code=error_code)
    return f"{int(timeout_seconds * 1000)}ms"


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
            "statement_timeout": _timeout_value(
                effective_statement_timeout_seconds,
                error_code="db_statement_timeout_seconds_required",
            ),
            "transaction_timeout": _timeout_value(
                effective_transaction_timeout_seconds,
                error_code="db_transaction_timeout_seconds_required",
            ),
        },
        local=True,
    )


def _discard_connection(pool: Any, conn: Any) -> None:
    conn.close()
    pool.putconn(conn)


async def _close_pool(pool: Any) -> None:
    result = pool.close()
    if result is not None:
        raise RuntimeError("db_pool_close_must_be_sync")


async def _acquire_db_gates(
    gates: tuple[tuple[asyncio.BoundedSemaphore, float], ...],
) -> tuple[asyncio.BoundedSemaphore, ...]:
    acquired: list[asyncio.BoundedSemaphore] = []
    try:
        for gate, timeout_seconds in gates:
            await asyncio.wait_for(gate.acquire(), timeout=float(timeout_seconds))
            acquired.append(gate)
    except BaseException:
        _release_db_gates(tuple(acquired))
        raise
    return tuple(acquired)


def _release_db_gates(gates: tuple[asyncio.BoundedSemaphore, ...]) -> None:
    for gate in reversed(gates):
        gate.release()


def _release_db_permit_on_completion(
    underlying: Future[Any],
    *,
    loop: asyncio.AbstractEventLoop,
    wrapped: asyncio.Future[Any],
    pending: set[asyncio.Future[Any]],
    gates: tuple[asyncio.BoundedSemaphore, ...],
    completed: Callable[[Future[Any]], None] | None = None,
) -> None:
    def on_done(future: Future[Any]) -> None:
        def finalize() -> None:
            pending.discard(wrapped)
            _release_db_gates(gates)
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
