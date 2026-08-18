from __future__ import annotations

import asyncio
import time
from contextlib import ExitStack, contextmanager, nullcontext
from typing import Any

import pytest
from psycopg import InternalError, OperationalError
from psycopg.errors import IdleInTransactionSessionTimeout, LockNotAvailable, QueryCanceled, TransactionTimeout
from psycopg_pool import PoolTimeout

from tracefold.app.database import ServeDatabase, ServeDatabaseBusy, WorkerDatabase
from tracefold.macro.acquisition import MacroAcquisitionService
from tracefold.macro.projection import MacroProjectionService
from tracefold.market.pricing.event_anchor_backfill_worker import EventAnchorBackfill
from tracefold.market.profiles.profile_projection import ProfileProjectionService
from tracefold.platform.resource import ResourceAdmissionTimeout


@pytest.mark.parametrize(
    ("service_type", "worker_name", "kwargs"),
    [
        (MacroProjectionService, "macro_projection", {}),
        (ProfileProjectionService, "profile_projection", {"active_profile_provider_ids": ()}),
    ],
)
def test_projection_services_default_to_canonical_terminal_owner(
    service_type: Any,
    worker_name: str,
    kwargs: dict[str, Any],
) -> None:
    service = service_type(db=_RecordingSessionDatabase(), **kwargs)

    assert service.worker_name == worker_name


def test_background_projection_session_bounds_postgres_parallelism() -> None:
    conn = _FakeConnection()
    pool = _FakePool(conn)
    bundle = WorkerDatabase(worker_pool=pool, telemetry=None)

    with bundle.worker_session("profile_projection"):
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

    with bundle.worker_session("market_tick_poll"):
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
        "profile_projection",
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


def test_projection_transitions_flush_only_after_outer_worker_transaction_commits() -> None:
    telemetry = _RecordingTelemetry()
    bundle = WorkerDatabase(worker_pool=_FakePool(_FakeConnection()), telemetry=telemetry)

    with bundle.worker_session("profile_projection") as repos:
        repos.projection_frontiers._observe("profile", "arrival")
        repos.projection_frontiers._observe("profile", "arrival")
        repos.projection_frontiers._observe("profile", "completion")
        assert telemetry.transitions == []

    assert telemetry.transitions == [
        ("profile", "arrival", 2),
        ("profile", "completion", 1),
    ]


def test_projection_transitions_are_discarded_on_outer_or_nested_rollback() -> None:
    telemetry = _RecordingTelemetry()
    bundle = WorkerDatabase(worker_pool=_FakePool(_FakeConnection()), telemetry=telemetry)

    with pytest.raises(ValueError, match="outer"), bundle.worker_session("profile_projection") as repos:
        repos.projection_frontiers._observe("profile", "arrival")
        raise ValueError("outer")

    with (
        bundle.worker_session("profile_projection") as repos,
        pytest.raises(
            ValueError,
            match="nested",
        ),
        repos.transaction(),
    ):
        repos.projection_frontiers._observe("profile", "arrival")
        raise ValueError("nested")

    assert telemetry.transitions == []


def test_projection_maintenance_sessions_do_not_inherit_steady_sql_deadline() -> None:
    cases = (
        (
            MacroProjectionService,
            {},
            "macro_maintenance_rebuild",
        ),
        (
            ProfileProjectionService,
            {"active_profile_provider_ids": ()},
            "profile_maintenance_rebuild",
        ),
    )
    for service_type, kwargs, worker_name in cases:
        database = _RecordingSessionDatabase()
        service = service_type(
            db=database,
            worker_name=worker_name,
            **kwargs,
        )

        service._session()

        assert database.calls == [
            {
                "name": worker_name,
                "statement_timeout_seconds": 120.0,
                "transaction_timeout_seconds": None,
            }
        ]


def test_macro_acquisition_uses_one_native_budget_for_statement_and_transaction() -> None:
    database = _RecordingSessionDatabase()
    service = object.__new__(MacroAcquisitionService)
    service.db = database
    service.worker_name = "macro_acquisition"

    service._session()
    service._session(timeout_seconds=0.5)

    assert database.calls == [
        {
            "name": "macro_acquisition",
            "statement_timeout_seconds": 5.0,
            "transaction_timeout_seconds": 5.0,
        },
        {
            "name": "macro_acquisition",
            "statement_timeout_seconds": 0.5,
            "transaction_timeout_seconds": 0.5,
        },
    ]


def test_event_anchor_uses_one_native_budget_for_statement_and_transaction() -> None:
    database = _RecordingSessionDatabase()
    worker = object.__new__(EventAnchorBackfill)
    worker.db = database
    worker.name = "event_anchor_backfill"

    with worker._worker_session():
        pass
    with worker._worker_session(timeout_seconds=0.5):
        pass

    assert database.calls == [
        {
            "name": "event_anchor_backfill",
            "statement_timeout_seconds": 3.0,
            "transaction_timeout_seconds": 3.0,
        },
        {
            "name": "event_anchor_backfill",
            "statement_timeout_seconds": 0.5,
            "transaction_timeout_seconds": 0.5,
        },
    ]


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


def test_native_statement_timeout_finishes_before_the_wrapper_watchdog() -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=_FakePool(_FakeConnection()), telemetry=None)

        def native_statement_timeout() -> None:
            time.sleep(2.5)
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

    asyncio.run(scenario())


def test_native_control_statement_timeout_finishes_before_the_wrapper_watchdog() -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=_FakePool(_FakeConnection()), telemetry=None)

        def native_statement_timeout() -> None:
            time.sleep(2.5)
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


def test_serve_admission_reserves_search_and_control_lanes() -> None:
    conn = _FakeConnection()
    database = ServeDatabase(api_pool=_FakeApiPool(conn), telemetry=None)

    with ExitStack() as ordinary_sessions:
        for _ in range(6):
            ordinary_sessions.enter_context(database.api_session("ordinary"))

        started = time.perf_counter()
        with (
            pytest.raises(ServeDatabaseBusy, match="serve_database_busy:ordinary"),
            database.api_session("ordinary"),
        ):
            pass
        assert 0.04 <= time.perf_counter() - started < 0.20

        with database.api_session("search"), database.api_session("control"):
            pass


def test_serve_search_lane_rejects_second_concurrent_query_without_consuming_control() -> None:
    conn = _FakeConnection()
    database = ServeDatabase(api_pool=_FakeApiPool(conn), telemetry=None)

    with database.api_session("search"):
        with (
            pytest.raises(ServeDatabaseBusy, match="serve_database_busy:search"),
            database.api_session("search"),
        ):
            pass
        with database.api_session("control"):
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


class _RecordingSessionDatabase:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def worker_session(
        self,
        name: str,
        *,
        statement_timeout_seconds: float | None = None,
        transaction_timeout_seconds: float | None = None,
    ) -> object:
        self.calls.append(
            {
                "name": name,
                "statement_timeout_seconds": statement_timeout_seconds,
                "transaction_timeout_seconds": transaction_timeout_seconds,
            }
        )
        return nullcontext()


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.transitions: list[tuple[str, str, int]] = []

    def record_pool_wait(self, pool: str, wait_ms: float) -> None:
        return None

    def record_transaction_seconds(self, worker: str, seconds: float) -> None:
        return None

    def record_projection_transition(self, domain: str, transition: str, count: int) -> None:
        self.transitions.append((domain, transition, count))
