from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from typing import Any

import pytest
from psycopg_pool import PoolTimeout

from tracefold.app.database import ServeDatabase, ServeDatabaseBusy, WorkerDatabase


def test_background_projection_session_bounds_postgres_parallelism() -> None:
    conn = _FakeConnection()
    pool = _FakePool(conn)
    bundle = WorkerDatabase(worker_pool=pool, telemetry=None)

    with bundle.worker_session("steady_projection_coordinator"):
        pass

    configured = [params for sql, params in conn.executed if sql.startswith("SELECT set_config")]
    assert ("max_parallel_workers_per_gather", "0") in configured
    assert ("jit", "off") in configured
    assert ("work_mem", "16MB") in configured
    assert "RESET max_parallel_workers_per_gather" in [sql for sql, _params in conn.executed]
    assert "RESET jit" in [sql for sql, _params in conn.executed]
    assert "RESET work_mem" in [sql for sql, _params in conn.executed]


def test_foreground_worker_session_keeps_postgres_defaults() -> None:
    conn = _FakeConnection()
    pool = _FakePool(conn)
    bundle = WorkerDatabase(worker_pool=pool, telemetry=None)

    with bundle.worker_session("collector"):
        pass

    configured_names = {str(params[0]) for sql, params in conn.executed if sql.startswith("SELECT set_config")}
    assert "max_parallel_workers_per_gather" not in configured_names
    assert "jit" not in configured_names
    assert "work_mem" not in configured_names


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

    def close(self) -> None:
        return None


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
