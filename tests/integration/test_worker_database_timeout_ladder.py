from __future__ import annotations

import asyncio

import pytest

from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.app.database import WorkerDatabase
from tracefold.platform.postgres.postgres_client import create_pool
from tracefold.platform.resource import ResourceAdmissionTimeout


@pytest.mark.integration
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


@pytest.mark.integration
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
