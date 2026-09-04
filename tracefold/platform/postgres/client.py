from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from psycopg import Connection, conninfo, pq
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from tracefold.platform.validation import require_nonnegative_float


def with_password_from_file(dsn: str, password_file: Path | None) -> str:
    if password_file is None:
        return dsn
    password = password_file.read_text(encoding="utf-8").strip()
    if "://" in dsn:
        return _url_dsn_with_password(dsn, password)
    parts: dict[str, Any] = dict(conninfo.conninfo_to_dict(dsn))
    parts["password"] = password
    return str(conninfo.make_conninfo(**parts))


def create_pool(
    dsn: str,
    *,
    min_size: int,
    max_size: int,
    max_waiting: int | None = None,
    connect_timeout_seconds: float,
    application_name: str | None = None,
    statement_timeout_seconds: float | None = None,
    lock_timeout_seconds: float | None = None,
    idle_in_transaction_session_timeout_seconds: float | None = None,
    session_settings: Mapping[str, str] | None = None,
    keepalives: bool | None = None,
    keepalives_idle: int | None = None,
    keepalives_interval: int | None = None,
    keepalives_count: int | None = None,
    read_only: bool = False,
) -> ConnectionPool:
    kwargs: dict[str, Any] = {
        "autocommit": True,
        "connect_timeout": int(connect_timeout_seconds),
        "row_factory": dict_row,
    }
    if application_name is not None:
        kwargs["application_name"] = application_name
    options = _postgres_runtime_options(
        statement_timeout_seconds=statement_timeout_seconds,
        lock_timeout_seconds=lock_timeout_seconds,
        idle_in_transaction_session_timeout_seconds=idle_in_transaction_session_timeout_seconds,
        session_settings=session_settings,
        read_only=read_only,
    )
    if options:
        kwargs["options"] = options
    if keepalives is not None:
        kwargs["keepalives"] = int(bool(keepalives))
    if keepalives_idle is not None:
        kwargs["keepalives_idle"] = int(keepalives_idle)
    if keepalives_interval is not None:
        kwargs["keepalives_interval"] = int(keepalives_interval)
    if keepalives_count is not None:
        kwargs["keepalives_count"] = int(keepalives_count)
    pool_kwargs: dict[str, Any] = {
        "conninfo": dsn,
        "min_size": min_size,
        "max_size": max_size,
        "kwargs": kwargs,
        "open": True,
    }
    if max_waiting is not None:
        pool_kwargs["max_waiting"] = int(max_waiting)
    return ConnectionPool(**pool_kwargs)


def _postgres_runtime_options(
    *,
    statement_timeout_seconds: float | None,
    lock_timeout_seconds: float | None,
    idle_in_transaction_session_timeout_seconds: float | None,
    session_settings: Mapping[str, str] | None = None,
    read_only: bool = False,
) -> str:
    options: list[str] = []
    if statement_timeout_seconds is not None:
        options.append(f"-c statement_timeout={_seconds_to_ms(statement_timeout_seconds)}")
    if lock_timeout_seconds is not None:
        options.append(f"-c lock_timeout={_seconds_to_ms(lock_timeout_seconds)}")
    if idle_in_transaction_session_timeout_seconds is not None:
        options.append(
            f"-c idle_in_transaction_session_timeout={_seconds_to_ms(idle_in_transaction_session_timeout_seconds)}"
        )
    if read_only:
        options.append("-c default_transaction_read_only=on")
    for name, value in sorted((session_settings or {}).items()):
        options.append(f"-c {name}={value}")
    return " ".join(options)


def _seconds_to_ms(seconds: float) -> int:
    timeout_seconds = require_nonnegative_float(
        seconds,
        error_code="postgres_runtime_timeout_seconds_required",
    )
    return int(timeout_seconds * 1000)


def _url_dsn_with_password(dsn: str, password: str) -> str:
    parsed = urlsplit(dsn)
    username = parsed.username or ""
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    auth = ""
    if username:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return urlunsplit((parsed.scheme, f"{auth}{host}", parsed.path, parsed.query, parsed.fragment))


def connect_postgres(
    dsn: str,
    *,
    connect_timeout_seconds: float = 5.0,
    application_name: str | None = None,
    session_settings: Mapping[str, str] | None = None,
    keepalives: bool | None = None,
    keepalives_idle: int | None = None,
    keepalives_interval: int | None = None,
    keepalives_count: int | None = None,
) -> Connection[dict[str, Any]]:
    """Open one connection.

    The keepalive and session-setting parameters mirror `create_pool` exactly. A pooled connection
    is replaced when it dies; a single long-lived session is not, and the execution runtime holds
    exactly one of those for the whole life of the process — it is the session whose advisory lock
    means "this process owns the account slot". Without TCP keepalives a killed container leaves
    that backend alive on the server, still holding the lock, and the next start fails with
    `oi_runtime_account_slot_already_owned` until the server notices (#537 D2).
    """

    kwargs: dict[str, Any] = {
        "autocommit": True,
        "connect_timeout": int(connect_timeout_seconds),
        "row_factory": dict_row,
    }
    if application_name is not None:
        kwargs["application_name"] = application_name
    options = _postgres_runtime_options(
        statement_timeout_seconds=None,
        lock_timeout_seconds=None,
        idle_in_transaction_session_timeout_seconds=None,
        session_settings=session_settings,
    )
    if options:
        kwargs["options"] = options
    if keepalives is not None:
        kwargs["keepalives"] = int(bool(keepalives))
    if keepalives_idle is not None:
        kwargs["keepalives_idle"] = int(keepalives_idle)
    if keepalives_interval is not None:
        kwargs["keepalives_interval"] = int(keepalives_interval)
    if keepalives_count is not None:
        kwargs["keepalives_count"] = int(keepalives_count)
    return Connection.connect(dsn, **kwargs)


@contextmanager
def transaction(conn: Connection) -> Iterator[None]:
    with conn.transaction():
        yield


def require_transaction(conn: Any, *, operation: str) -> None:
    try:
        status = conn.info.transaction_status
    except AttributeError as exc:
        raise RuntimeError(f"{operation}_requires_transaction_status_contract") from exc
    if status == pq.TransactionStatus.IDLE:
        raise RuntimeError(f"{operation}_requires_explicit_transaction")


def postgres_health_check(conn: Any, *, expected_migration_version: str | None = None) -> dict[str, object]:
    try:
        row = conn.execute("SELECT 1 AS ok").fetchone()
        if row is None or int(row["ok"]) != 1:
            conn.rollback()
            return {"ok": False, "probe": "postgres_liveness", "detail": "missing_select_result"}
        version_row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        migration_version = version_row["version_num"] if version_row else None
        migration_ok = expected_migration_version is None or migration_version == expected_migration_version
        conn.commit()
        return {
            "ok": migration_ok,
            "probe": "postgres_liveness",
            "migration_version": migration_version,
            **(
                {
                    "expected_migration_version": expected_migration_version,
                    "migration_status": "ready" if migration_ok else "stale",
                }
                if expected_migration_version is not None
                else {}
            ),
        }
    except Exception as exc:
        try:
            conn.rollback()
        except Exception as rollback_exc:
            return {
                "ok": False,
                "probe": "postgres_liveness",
                "error": type(rollback_exc).__name__,
                "detail": str(rollback_exc),
                "original_error": type(exc).__name__,
                "original_detail": str(exc),
            }
        return {"ok": False, "probe": "postgres_liveness", "error": type(exc).__name__, "detail": str(exc)}


def postgres_liveness_check(conn: Any) -> dict[str, object]:
    """Probe only whether PostgreSQL can serve a trivial query.

    Schema compatibility is a startup invariant. Runtime readiness uses this
    deliberately smaller probe so it does not re-read migration state for
    every request.
    """
    try:
        row = conn.execute("SELECT 1 AS ok").fetchone()
        if row is None or int(row["ok"]) != 1:
            conn.rollback()
            return {"ok": False, "probe": "postgres_liveness", "detail": "missing_select_result"}
        conn.commit()
        return {"ok": True, "probe": "postgres_liveness"}
    except Exception as exc:
        try:
            conn.rollback()
        except Exception as rollback_exc:
            return {
                "ok": False,
                "probe": "postgres_liveness",
                "error": type(rollback_exc).__name__,
                "detail": str(rollback_exc),
                "original_error": type(exc).__name__,
                "original_detail": str(exc),
            }
        return {"ok": False, "probe": "postgres_liveness", "error": type(exc).__name__, "detail": str(exc)}
