from __future__ import annotations

import asyncio
import time
from threading import Event

import pytest
from psycopg.errors import IdleInTransactionSessionTimeout

from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers import root as workers_module
from tracefold.app.workers.wiring.database import (
    WorkerQuoteDatabase,
    WorkerReactionDatabase,
    WorkerTradingDatabase,
)
from tracefold.platform.postgres.client import connect_postgres, create_pool
from tracefold.platform.resource import ResourceAdmissionTimeout

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_quote_ordinary_lane_progresses_while_trading_holds_the_heavy_gate() -> None:
    """#304: real sessions prove Quote is ordinary and Reaction/Trading share one heavy permit."""

    pool = create_pool(
        _test_postgres_dsn(),
        min_size=1,
        max_size=4,
        max_waiting=3,
        connect_timeout_seconds=5.0,
        application_name="tracefold_quote_lane_test",
        statement_timeout_seconds=3.0,
        lock_timeout_seconds=0.25,
        idle_in_transaction_session_timeout_seconds=5.0,
    )
    pool.wait(timeout=5.0)
    database = WorkerDatabase(worker_pool=pool, telemetry=None)
    quote = WorkerQuoteDatabase(database)
    reaction = WorkerReactionDatabase(database)
    trading = WorkerTradingDatabase(database)
    trading_started = Event()
    reaction_started = Event()

    def hold_heavy(repos) -> str:
        trading_started.set()
        repos.trading.case_counts(since_ms=0)
        time.sleep(1.5)
        return "trading-finished"

    def read_one(repos) -> int:
        repos.price.quote_target_symbols(since_ms=0, limit=1)
        return 1

    def read_reaction(repos) -> int:
        reaction_started.set()
        return read_one(repos)

    async def scenario() -> None:
        held = asyncio.create_task(trading.read("trading_hold", hold_heavy, timeout_seconds=3.0))
        waiting: asyncio.Task[int] | None = None
        try:
            for _ in range(100):
                if trading_started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert trading_started.is_set()

            waiting = asyncio.create_task(reaction.read("reaction_wait", read_reaction, timeout_seconds=3.0))
            await asyncio.sleep(0.05)
            assert not reaction_started.is_set()

            assert (
                await asyncio.wait_for(
                    quote.read("quote_progress", read_one, timeout_seconds=1.0),
                    timeout=1.0,
                )
                == 1
            )
            assert not held.done()
            assert not reaction_started.is_set()

            assert await held == "trading-finished"
            assert await waiting == 1
            assert reaction_started.is_set()
        finally:
            await asyncio.gather(held, *(task for task in (waiting,) if task is not None), return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        database.close_executors()
        pool.close()


def test_native_postgres_timeout_is_recoverable_before_wrapper_watchdog() -> None:
    pool = create_pool(
        _test_postgres_dsn(),
        min_size=1,
        max_size=4,
        max_waiting=3,
        connect_timeout_seconds=5.0,
        application_name="tracefold_worker_timeout_test",
        statement_timeout_seconds=0.5,
        lock_timeout_seconds=0.25,
        idle_in_transaction_session_timeout_seconds=5.0,
    )
    pool.wait(timeout=5.0)
    database = WorkerDatabase(worker_pool=pool, telemetry=None)

    def slow_query() -> None:
        with database.worker_session(
            "statement_timeout_probe",
            statement_timeout_seconds=0.1,
        ) as repos:
            repos.conn.execute("SELECT pg_sleep(0.2)")

    def liveness_query() -> int:
        with database.worker_session("statement_timeout_recovery") as repos:
            row = repos.conn.execute("SELECT 1 AS ok").fetchone()
            return int(row["ok"])

    async def scenario() -> None:
        with pytest.raises(
            ResourceAdmissionTimeout,
            match="worker_database_statement_timeout:statement_timeout_probe",
        ):
            await database.run_business(
                "statement_timeout_probe",
                slow_query,
                operation_timeout_seconds=0.1,
            )
        assert await database.drain_business(timeout_seconds=1.0)
        assert (
            await database.run_business(
                "statement_timeout_recovery",
                liveness_query,
                operation_timeout_seconds=3.0,
            )
            == 1
        )

    try:
        asyncio.run(scenario())
    finally:
        database.close_executors()
        pool.close()


def test_control_loop_survives_one_idle_pool_connection_closed_by_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = create_pool(
        _test_postgres_dsn(),
        min_size=1,
        max_size=1,
        connect_timeout_seconds=5.0,
        application_name="tracefold_worker_control_recovery_test",
        statement_timeout_seconds=0.5,
        lock_timeout_seconds=0.25,
        idle_in_transaction_session_timeout_seconds=5.0,
    )
    pool.wait(timeout=5.0)
    database = WorkerDatabase(worker_pool=pool, telemetry=None)
    killer = connect_postgres(_test_postgres_dsn())
    try:
        conn = pool.getconn(timeout=1.0)
        backend_pid = int(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
        pool.putconn(conn)
        row = killer.execute("SELECT pg_terminate_backend(%s) AS terminated", (backend_pid,)).fetchone()
        assert row is not None and bool(row["terminated"])

        stop_event = asyncio.Event()
        attempts = 0

        async def heartbeat(**_kwargs: object) -> int:
            nonlocal attempts
            attempts += 1

            def liveness() -> int:
                with database.worker_session("workers_runtime_heartbeat", 1.0) as repos:
                    result = repos.conn.execute("SELECT 1 AS ok").fetchone()
                    return int(result["ok"])

            result = await database.run_control(
                "workers_runtime_heartbeat",
                liveness,
                operation_timeout_seconds=1.0,
            )
            stop_event.set()
            return result

        monkeypatch.setattr(workers_module, "_control_liveness_and_heartbeat", heartbeat)
        probe = workers_module._ProbeState(
            runtime_id="runtime-control-recovery",
            runtime_version="v2",
            started_at_ms=1,
            clock_ms=lambda: 1,
        )
        asyncio.run(
            asyncio.wait_for(
                workers_module._run_control(
                    db=database,
                    lock_conn=object(),
                    runtime_id=probe.runtime_id,
                    probe_state=probe,
                    stop_event=stop_event,
                ),
                timeout=10.0,
            )
        )

        assert attempts >= 2
        assert probe.heartbeat_at_ms == 1
    finally:
        killer.close()
        database.close_executors()
        pool.close()


def test_native_transaction_timeout_bounds_the_complete_worker_session() -> None:
    pool = create_pool(
        _test_postgres_dsn(),
        min_size=1,
        max_size=4,
        max_waiting=3,
        connect_timeout_seconds=5.0,
        application_name="tracefold_worker_transaction_timeout_test",
        statement_timeout_seconds=0.5,
        lock_timeout_seconds=0.25,
        idle_in_transaction_session_timeout_seconds=5.0,
    )
    pool.wait(timeout=5.0)
    database = WorkerDatabase(worker_pool=pool, telemetry=None)

    def two_individually_bounded_queries() -> None:
        with database.worker_session(
            "transaction_timeout_probe",
            statement_timeout_seconds=1.0,
            transaction_timeout_seconds=1.0,
        ) as repos:
            repos.conn.execute("SELECT pg_sleep(0.8)")
            repos.conn.execute("SELECT pg_sleep(0.8)")

    def liveness_query() -> int:
        with database.worker_session("transaction_timeout_recovery") as repos:
            row = repos.conn.execute("SELECT 1 AS ok").fetchone()
            return int(row["ok"])

    async def scenario() -> None:
        with pytest.raises(
            ResourceAdmissionTimeout,
            match="worker_database_transaction_timeout:transaction_timeout_probe",
        ):
            await database.run_business(
                "transaction_timeout_probe",
                two_individually_bounded_queries,
                operation_timeout_seconds=1.0,
            )
        assert await database.drain_business(timeout_seconds=1.0)
        assert (
            await database.run_business(
                "transaction_timeout_recovery",
                liveness_query,
                operation_timeout_seconds=3.0,
            )
            == 1
        )

    try:
        asyncio.run(scenario())
    finally:
        database.close_executors()
        pool.close()


def test_worker_statement_budget_does_not_cancel_a_multi_statement_transaction() -> None:
    pool = create_pool(
        _test_postgres_dsn(),
        min_size=1,
        max_size=4,
        max_waiting=3,
        connect_timeout_seconds=5.0,
        application_name="tracefold_worker_transaction_margin_test",
        statement_timeout_seconds=0.5,
        lock_timeout_seconds=0.25,
        idle_in_transaction_session_timeout_seconds=5.0,
    )
    pool.wait(timeout=5.0)
    database = WorkerDatabase(worker_pool=pool, telemetry=None)

    def two_individually_bounded_queries() -> int:
        with database.worker_session("transaction_margin_probe", statement_timeout_seconds=0.2) as repos:
            repos.conn.execute("SELECT pg_sleep(0.12)")
            repos.conn.execute("SELECT pg_sleep(0.12)")
            row = repos.conn.execute("SELECT 1 AS ok").fetchone()
            return int(row["ok"])

    async def scenario() -> None:
        assert (
            await database.run_business(
                "worker_transaction_margin_probe",
                two_individually_bounded_queries,
                operation_timeout_seconds=1.0,
            )
            == 1
        )

    try:
        asyncio.run(scenario())
    finally:
        database.close_executors()
        pool.close()


def test_idle_transaction_timeout_is_recoverable_only_on_the_business_lane() -> None:
    pool = create_pool(
        _test_postgres_dsn(),
        # Match the production Worker pool: one killed session must not make
        # recovery depend on how quickly psycopg replenishes its only connection.
        min_size=2,
        max_size=4,
        max_waiting=3,
        connect_timeout_seconds=5.0,
        application_name="tracefold_worker_idle_transaction_timeout_test",
        statement_timeout_seconds=0.5,
        lock_timeout_seconds=0.25,
        idle_in_transaction_session_timeout_seconds=5.0,
    )
    pool.wait(timeout=5.0)
    database = WorkerDatabase(worker_pool=pool, telemetry=None)

    def idle_in_transaction() -> None:
        with database.worker_session("idle_transaction_timeout_probe") as repos:
            repos.conn.execute("SET LOCAL idle_in_transaction_session_timeout = '100ms'")
            repos.conn.execute("SELECT 1")
            time.sleep(0.2)
            repos.conn.execute("SELECT 1")

    def liveness_query() -> int:
        with database.worker_session("idle_transaction_timeout_recovery") as repos:
            row = repos.conn.execute("SELECT 1 AS ok").fetchone()
            return int(row["ok"])

    async def scenario() -> None:
        with pytest.raises(
            ResourceAdmissionTimeout,
            match="worker_database_idle_transaction_timeout:business_idle_transaction",
        ):
            await database.run_business(
                "business_idle_transaction",
                idle_in_transaction,
                operation_timeout_seconds=0.5,
            )
        assert (
            await database.run_business(
                "business_idle_transaction_recovery",
                liveness_query,
                operation_timeout_seconds=1.0,
            )
            == 1
        )
        with pytest.raises(IdleInTransactionSessionTimeout):
            await database.run_control(
                "control_idle_transaction",
                idle_in_transaction,
                operation_timeout_seconds=0.5,
            )

    try:
        asyncio.run(scenario())
    finally:
        database.close_executors()
        pool.close()
