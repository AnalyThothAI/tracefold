from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from psycopg import OperationalError, pq
from psycopg.rows import RowMaker
from psycopg.sql import Composable

from tracefold.app.repository_session import RepositorySession, repositories_for_connection
from tracefold.platform.postgres.client import connect_postgres
from tracefold.platform.postgres.migrations import upgrade_head

DEFAULT_TEST_DSN = "postgresql://postgres:postgres@127.0.0.1:55432/tracefold_test"
TEST_DATABASE_NAME = "tracefold_test"


def test_postgres_dsn() -> str:
    return os.environ.get("TRACEFOLD_TEST_POSTGRES_DSN", DEFAULT_TEST_DSN)


def ensure_migrated_postgres_resource(dsn: str, *, resource_name: str) -> None:
    """Migrate one explicitly requested test database or fail its owning gate."""

    try:
        with connect_postgres(dsn) as conn:
            assert_dedicated_test_database(conn)
        upgrade_head(dsn)
    except Exception as exc:
        pytest.fail(f"alembic upgrade head failed for the declared {resource_name}: {exc}", pytrace=False)


def connect_postgres_test(*_: Any, read_only: bool = False, **__: Any):
    try:
        conn = connect_postgres(test_postgres_dsn())
        conn.row_factory = _compat_row
        assert_dedicated_test_database(conn)
    except OperationalError as exc:
        if os.environ.get("TRACEFOLD_TEST_EVIDENCE") == "1":
            pytest.fail(f"PostgreSQL test database is required in evidence mode: {exc}", pytrace=False)
        pytest.skip(f"PostgreSQL test database is not available: {exc}")
    if read_only:
        conn.execute("SET default_transaction_read_only = on")
        conn.commit()
    return _TestConnection(conn)


def postgres_settings_storage() -> dict[str, Any]:
    dsn = test_postgres_dsn()
    return {
        "postgres": {
            "serve_dsn": dsn,
            "workers_dsn": dsn,
            "migrate_dsn": dsn,
            "nautilus_dsn": dsn,
            "serve_password_file": None,
            "workers_password_file": None,
            "migrate_password_file": None,
            "nautilus_password_file": None,
        }
    }


def prepare_postgres_database() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        reset_postgres_schema(conn)
    finally:
        conn.close()


def reset_postgres_schema(conn) -> None:
    if _read_only(conn):
        return
    assert_dedicated_test_database(conn)
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()
    upgrade_head(test_postgres_dsn())


def reset_postgres_database(dsn: str) -> None:
    """Recreate the schema only after the server proves it is the dedicated test database."""

    with connect_postgres(dsn) as identity_conn:
        assert_dedicated_test_database(identity_conn)
    with connect_postgres(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
    upgrade_head(dsn)


def assert_dedicated_test_database(conn: Any) -> None:
    row = conn.execute("SELECT current_database()").fetchone()
    database_name = str(row["current_database"] if isinstance(row, dict) else row[0])
    if database_name != TEST_DATABASE_NAME:
        raise RuntimeError(f"postgres_test_database_identity_invalid:{database_name}; expected {TEST_DATABASE_NAME}")


@contextmanager
def repository_session_for_connection(conn: Any) -> Iterator[RepositorySession]:
    yield repositories_for_connection(conn)


def _read_only(conn) -> bool:
    row = conn.execute("SHOW default_transaction_read_only").fetchone()
    value = str(row["default_transaction_read_only"] if isinstance(row, dict) else row[0]).lower()
    conn.commit()
    return value in {"on", "true", "1"}


class CompatRow(dict):
    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _compat_row(cursor) -> RowMaker[CompatRow]:
    if cursor.description is None:
        return lambda values: CompatRow()
    columns = [column.name for column in cursor.description]

    def make_row(values: tuple[Any, ...]) -> CompatRow:
        return CompatRow(zip(columns, values, strict=True))

    return make_row


class _TestConnection:
    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = None, *args: Any, **kwargs: Any):
        return self._conn.execute(_postgres_sql(sql), params, *args, **kwargs)

    @property
    def in_transaction(self) -> bool:
        return self._conn.info.transaction_status != pq.TransactionStatus.IDLE

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _postgres_sql(sql: str | Composable) -> str | Composable:
    return sql if isinstance(sql, Composable) else str(sql)
