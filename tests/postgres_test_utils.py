from __future__ import annotations

import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from psycopg import OperationalError, conninfo, pq, sql
from psycopg.rows import RowMaker
from psycopg.sql import Composable

from tracefold.app.repository_session import RepositorySession, repositories_for_connection
from tracefold.platform.postgres.client import connect_postgres
from tracefold.platform.postgres.migrations import upgrade_head

DEFAULT_TEST_DSN = "postgresql://postgres:postgres@127.0.0.1:55432/tracefold_test"
TEST_DATABASE_NAME = "tracefold_test"
_CLONE_DATABASE_PATTERN = re.compile(r"tracefold_test_(?:baseline|case|migration)_[0-9a-f]{12}(?:_[0-9]+)?")


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


def connect_postgres_test(*_: Any, read_only: bool = False, dsn: str | None = None, **__: Any):
    selected_dsn = dsn or test_postgres_dsn()
    try:
        conn = connect_postgres(selected_dsn)
        conn.row_factory = _compat_row
        assert_dedicated_test_database(conn, expected_database=_database_name(selected_dsn))
    except OperationalError as exc:
        if os.environ.get("TRACEFOLD_TEST_RESOURCES_REQUIRED") == "1":
            pytest.fail(f"PostgreSQL test database is required for complete verification: {exc}", pytrace=False)
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


def assert_dedicated_test_database(conn: Any, *, expected_database: str | None = None) -> None:
    row = conn.execute("SELECT current_database()").fetchone()
    database_name = str(row["current_database"] if isinstance(row, dict) else row[0])
    if database_name != TEST_DATABASE_NAME and _CLONE_DATABASE_PATTERN.fullmatch(database_name) is None:
        raise RuntimeError(
            f"postgres_test_database_identity_invalid:{database_name}; expected {TEST_DATABASE_NAME} or an owned clone"
        )
    if expected_database is not None and database_name != expected_database:
        raise RuntimeError(f"postgres_test_database_identity_mismatch:{database_name}; expected {expected_database}")


@dataclass
class MigratedPostgresCloneFactory:
    """Migrate one private baseline, then clone isolated databases without rerunning Alembic."""

    server_dsn: str
    run_token: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    clone_count: int = field(default=0, init=False)
    _baseline_database: str = field(init=False)
    _created: set[str] = field(default_factory=set, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        if _database_name(self.server_dsn) != TEST_DATABASE_NAME:
            raise RuntimeError("postgres_clone_source_must_be_tracefold_test")
        self._baseline_database = f"{TEST_DATABASE_NAME}_baseline_{self.run_token}"
        self._create_database(self._baseline_database, template="template0")
        try:
            upgrade_head(_database_dsn(self.server_dsn, self._baseline_database))
        except BaseException:
            self._drop_database(self._baseline_database)
            raise

    @contextmanager
    def clone(self) -> Iterator[str]:
        with self._lock:
            self.clone_count += 1
            database = f"{TEST_DATABASE_NAME}_case_{self.run_token}_{self.clone_count}"
            self._create_database(database, template=self._baseline_database)
            self._created.add(database)
        try:
            yield _database_dsn(self.server_dsn, database)
        finally:
            with self._lock:
                self._drop_database(database)
                self._created.discard(database)

    def close(self) -> None:
        with self._lock:
            for database in sorted(self._created):
                self._drop_database(database)
            self._created.clear()
            self._drop_database(self._baseline_database)

    def _create_database(self, database: str, *, template: str) -> None:
        _require_owned_clone_name(database)
        with connect_postgres(_database_dsn(self.server_dsn, "postgres")) as admin:
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(database), sql.Identifier(template))
            )

    def _drop_database(self, database: str) -> None:
        _require_owned_clone_name(database)
        with connect_postgres(_database_dsn(self.server_dsn, "postgres")) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))


@contextmanager
def temporary_unmigrated_postgres_database(server_dsn: str) -> Iterator[str]:
    """Yield one empty, run-private database for historical migration owners."""

    if _database_name(server_dsn) != TEST_DATABASE_NAME:
        raise RuntimeError("postgres_migration_source_must_be_tracefold_test")
    database = f"{TEST_DATABASE_NAME}_migration_{uuid.uuid4().hex[:12]}"
    _require_owned_clone_name(database)
    admin_dsn = _database_dsn(server_dsn, "postgres")
    with connect_postgres(admin_dsn) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
    try:
        yield _database_dsn(server_dsn, database)
    finally:
        with connect_postgres(admin_dsn) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))


def _require_owned_clone_name(database: str) -> None:
    if _CLONE_DATABASE_PATTERN.fullmatch(database) is None:
        raise RuntimeError(f"postgres_clone_database_identity_invalid:{database}")


def _database_name(dsn: str) -> str:
    database = conninfo.conninfo_to_dict(dsn).get("dbname")
    if not database:
        raise RuntimeError("postgres_test_database_name_missing")
    return str(database)


def _database_dsn(dsn: str, database: str) -> str:
    if "://" in dsn:
        parsed = urlsplit(dsn)
        return urlunsplit((parsed.scheme, parsed.netloc, f"/{quote(database, safe='')}", parsed.query, parsed.fragment))
    return str(conninfo.make_conninfo(dsn, dbname=database))


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
