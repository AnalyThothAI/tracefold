from __future__ import annotations

from typing import Any

from tracefold.app.database import DBPoolBundle


def test_background_projection_session_bounds_postgres_parallelism() -> None:
    conn = _FakeConnection()
    pool = _FakePool(conn)
    bundle = DBPoolBundle(api_pool=None, worker_pool=pool, telemetry=None)

    with bundle.worker_session("news_pipeline"):
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
    bundle = DBPoolBundle(api_pool=None, worker_pool=pool, telemetry=None)

    with bundle.worker_session("collector"):
        pass

    configured_names = {str(params[0]) for sql, params in conn.executed if sql.startswith("SELECT set_config")}
    assert "max_parallel_workers_per_gather" not in configured_names
    assert "jit" not in configured_names
    assert "work_mem" not in configured_names


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

    def getconn(self) -> _FakeConnection:
        return self.conn

    def putconn(self, conn: _FakeConnection) -> None:
        assert conn is self.conn
