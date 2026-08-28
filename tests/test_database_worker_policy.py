from __future__ import annotations

import asyncio
import time
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg import InternalError, OperationalError
from psycopg.errors import IdleInTransactionSessionTimeout, LockNotAvailable, QueryCanceled, TransactionTimeout
from psycopg_pool import PoolTimeout

from tracefold.app import serve_database as serve_database_module
from tracefold.app import worker_database as worker_database_module
from tracefold.app.serve_database import ServeDatabase, ServeDatabaseBusy
from tracefold.app.worker_database import WorkerDatabase
from tracefold.platform.resource import ResourceAdmissionTimeout


def test_worker_pool_preopens_steady_lock_and_control_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakePool:
        def wait(self, *, timeout: float) -> None:
            captured["wait_timeout"] = timeout

    def fake_create_pool(dsn: str, **kwargs: Any) -> FakePool:
        captured.update({"dsn": dsn, **kwargs})
        return FakePool()

    monkeypatch.setattr(worker_database_module, "create_pool", fake_create_pool)
    settings = SimpleNamespace(
        storage=SimpleNamespace(postgres=SimpleNamespace(connect_timeout_seconds=5.0)),
        postgres_dsn=lambda _role: "postgresql://workers@example.test/tracefold",
        postgres_password_file=lambda _role: None,
    )

    database = WorkerDatabase.create(settings, telemetry=None)
    database.close_executors()

    assert captured["min_size"] == 2
    assert captured["wait_timeout"] == 5.0


def test_background_worker_session_bounds_postgres_parallelism() -> None:
    conn = _FakeConnection()
    pool = _FakePool(conn)
    bundle = WorkerDatabase(worker_pool=pool, telemetry=None)

    with bundle.worker_session("news_janitor"):
        pass

    configured = _combined_config(conn.executed[0])
    assert configured["max_parallel_workers_per_gather"] == "0"
    assert configured["jit"] == "off"
    assert configured["work_mem"] == "16MB"


def test_all_worker_sessions_use_the_uniform_bounded_postgres_policy() -> None:
    conn = _FakeConnection()
    pool = _FakePool(conn)
    bundle = WorkerDatabase(worker_pool=pool, telemetry=None)

    with bundle.worker_session("collector"):
        pass

    configured = _combined_config(conn.executed[0])
    assert configured.keys() >= {"max_parallel_workers_per_gather", "jit", "work_mem"}


def test_steady_worker_session_default_sql_budget_leaves_native_cleanup_grace() -> None:
    conn = _FakeConnection()
    bundle = WorkerDatabase(worker_pool=_FakePool(conn), telemetry=None)

    with bundle.worker_session("news_janitor"):
        pass

    assert len(conn.executed) == 1
    assert _combined_config(conn.executed[0])["statement_timeout"] == "3000ms"
    assert _combined_config(conn.executed[0])["transaction_timeout"] == "8000ms"
    assert "true" in conn.executed[0][0]


def test_worker_session_enforces_transaction_local_timeout() -> None:
    conn = _FakeConnection()
    pool = _FakePool(conn)
    bundle = WorkerDatabase(worker_pool=pool, telemetry=None)

    with bundle.worker_session(
        "news_janitor",
        transaction_timeout_seconds=0.5,
    ):
        pass

    assert _combined_config(conn.executed[0])["transaction_timeout"] == "500ms"
    assert len(conn.executed) == 1
    assert "true" in conn.executed[0][0]


def test_worker_session_applies_dynamic_policy_in_one_local_round_trip() -> None:
    conn = _FakeConnection()
    pool = _FakePool(conn)
    bundle = WorkerDatabase(worker_pool=pool, telemetry=None)

    with bundle.worker_session(
        "news_deduper",
        statement_timeout_seconds=0.5,
        transaction_timeout_seconds=0.5,
    ):
        pass

    assert len(conn.executed) == 1
    assert _combined_config(conn.executed[0]) == {
        "max_parallel_workers_per_gather": "0",
        "jit": "off",
        "work_mem": "16MB",
        "application_name": "tracefold_workers:news_deduper",
        "statement_timeout": "500ms",
        "transaction_timeout": "500ms",
    }


def test_worker_lock_timeout_is_recoverable_bounded_contention() -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=_FakePool(_FakeConnection()), telemetry=None)

        def lock_timeout() -> None:
            raise LockNotAvailable("canceling statement due to lock timeout")

        try:
            with pytest.raises(
                ResourceAdmissionTimeout,
                match="worker_database_lock_timeout:gmgn_event_publish",
            ):
                await database.run_business(
                    "gmgn_event_publish",
                    lock_timeout,
                    operation_timeout_seconds=1.0,
                )
            assert (
                await database.run_business(
                    "after_lock_timeout",
                    lambda: 1,
                    operation_timeout_seconds=1.0,
                )
                == 1
            )
        finally:
            database.close_executors()

    asyncio.run(scenario())


def test_native_statement_timeout_finishes_before_the_wrapper_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=_FakePool(_FakeConnection()), telemetry=None)

        def native_statement_timeout() -> None:
            time.sleep(0.02)
            raise QueryCanceled("canceling statement due to statement timeout")

        try:
            with pytest.raises(
                ResourceAdmissionTimeout,
                match="worker_database_statement_timeout:news_triage_load",
            ):
                await database.run_business(
                    "news_triage_load",
                    native_statement_timeout,
                    operation_timeout_seconds=0.01,
                )
            assert await database.drain_business(timeout_seconds=1.0)
        finally:
            database.close_executors()

    monkeypatch.setattr(worker_database_module, "_WORKER_BUSINESS_OPERATION_COMPLETION_GRACE_SECONDS", 0.1)
    asyncio.run(scenario())


def test_native_control_statement_timeout_finishes_before_the_wrapper_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=_FakePool(_FakeConnection()), telemetry=None)

        def native_statement_timeout() -> None:
            time.sleep(0.02)
            raise QueryCanceled("canceling statement due to statement timeout")

        try:
            with pytest.raises(
                ResourceAdmissionTimeout,
                match="worker_database_statement_timeout:workers_runtime_heartbeat",
            ):
                await database.run_control(
                    "workers_runtime_heartbeat",
                    native_statement_timeout,
                    operation_timeout_seconds=0.01,
                )
            assert await database.drain_control(timeout_seconds=1.0)
        finally:
            database.close_executors()

    monkeypatch.setattr(worker_database_module, "_WORKER_CONTROL_OPERATION_COMPLETION_GRACE_SECONDS", 0.1)
    asyncio.run(scenario())


def test_native_transaction_timeout_is_recoverable() -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=_FakePool(_FakeConnection()), telemetry=None)

        def native_transaction_timeout() -> None:
            raise TransactionTimeout("terminating transaction due to timeout")

        try:
            with pytest.raises(
                ResourceAdmissionTimeout,
                match="worker_database_transaction_timeout:news_deduper_admit",
            ):
                await database.run_business(
                    "news_deduper_admit",
                    native_transaction_timeout,
                    operation_timeout_seconds=0.01,
                )
        finally:
            database.close_executors()

    asyncio.run(scenario())


def test_business_connection_loss_is_recoverable_but_control_loss_is_fatal() -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=_FakePool(_FakeConnection()), telemetry=None)

        def connection_lost() -> None:
            raise OperationalError("consuming input failed: server closed the connection unexpectedly")

        try:
            with pytest.raises(
                ResourceAdmissionTimeout,
                match="worker_database_connection_lost:news_delivery_load",
            ):
                await database.run_business(
                    "news_delivery_load",
                    connection_lost,
                    operation_timeout_seconds=0.5,
                )
            with pytest.raises(OperationalError):
                await database.run_control(
                    "workers_runtime_heartbeat",
                    connection_lost,
                    operation_timeout_seconds=0.5,
                )
        finally:
            database.close_executors()

    asyncio.run(scenario())


def test_business_idle_transaction_timeout_is_recoverable_but_control_and_other_internal_errors_are_fatal() -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=_FakePool(_FakeConnection()), telemetry=None)

        def idle_transaction_timeout() -> None:
            raise IdleInTransactionSessionTimeout("terminating connection due to idle-in-transaction timeout")

        def other_internal_error() -> None:
            raise InternalError("unexpected internal database error")

        try:
            with pytest.raises(
                ResourceAdmissionTimeout,
                match="worker_database_idle_transaction_timeout:gmgn_event_publish",
            ):
                await database.run_business(
                    "gmgn_event_publish",
                    idle_transaction_timeout,
                    operation_timeout_seconds=0.5,
                )
            with pytest.raises(IdleInTransactionSessionTimeout):
                await database.run_control(
                    "workers_runtime_heartbeat",
                    idle_transaction_timeout,
                    operation_timeout_seconds=0.5,
                )
            with pytest.raises(InternalError, match="unexpected internal database error"):
                await database.run_business(
                    "unknown_internal_failure",
                    other_internal_error,
                    operation_timeout_seconds=0.5,
                )
        finally:
            database.close_executors()

    asyncio.run(scenario())


def test_business_pool_checkout_timeout_is_recoverable_but_control_timeout_is_fatal() -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=_TimedOutWorkerPool(), telemetry=None)

        def checkout() -> None:
            with database.worker_session("pool_checkout"):
                pass

        try:
            with pytest.raises(
                ResourceAdmissionTimeout,
                match="worker_database_pool_timeout:business_checkout",
            ):
                await database.run_business(
                    "business_checkout",
                    checkout,
                    operation_timeout_seconds=1.0,
                )
            with pytest.raises(PoolTimeout):
                await database.run_control(
                    "control_checkout",
                    checkout,
                    operation_timeout_seconds=1.0,
                )
        finally:
            database.close_executors()

    asyncio.run(scenario())


def test_serve_admission_reserves_the_control_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConnection()
    database = ServeDatabase(api_pool=_FakeApiPool(conn), telemetry=None)
    monkeypatch.setattr(serve_database_module, "_SERVE_PERMIT_TIMEOUT_SECONDS", 0.0)

    with ExitStack() as ordinary_sessions:
        for _ in range(6):
            ordinary_sessions.enter_context(database.api_session("ordinary"))

        with (
            pytest.raises(ServeDatabaseBusy, match="serve_database_busy:ordinary"),
            database.api_session("ordinary"),
        ):
            pass

        with database.api_session("control"):
            pass
    with pytest.raises(ValueError, match="serve_database_lane_invalid:search"), database.api_session("search"):
        pass


def test_serve_pool_checkout_timeout_is_typed_busy() -> None:
    database = ServeDatabase(api_pool=_TimedOutApiPool(), telemetry=None)

    with (
        pytest.raises(ServeDatabaseBusy, match="serve_database_pool_busy:ordinary"),
        database.api_session(),
    ):
        pass


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, params))

    @contextmanager
    def transaction(self):
        yield

    def close(self) -> None:
        return None


def _combined_config(executed: tuple[str, tuple[Any, ...]]) -> dict[str, str]:
    sql, params = executed
    assert sql.startswith("SELECT set_config")
    assert len(params) % 2 == 0
    return {str(params[index]): str(params[index + 1]) for index in range(0, len(params), 2)}


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self.conn = conn

    def getconn(self, timeout: float | None = None) -> _FakeConnection:
        return self.conn

    def putconn(self, conn: _FakeConnection) -> None:
        assert conn is self.conn


class _FakeApiPool:
    def __init__(self, conn: _FakeConnection) -> None:
        self.conn = conn

    @contextmanager
    def connection(self, timeout: float | None = None):
        yield self.conn


class _TimedOutApiPool:
    @contextmanager
    def connection(self, timeout: float | None = None):
        raise PoolTimeout("test")
        yield


class _TimedOutWorkerPool:
    def getconn(self, timeout: float | None = None) -> _FakeConnection:
        raise PoolTimeout("test")
