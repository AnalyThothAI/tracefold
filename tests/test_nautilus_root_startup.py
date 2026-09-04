"""The execution runtime asserts the schema head before it becomes the account-slot owner.

#537 PR-2 took the `migrate: service_completed_successfully` edge off the `nautilus` service so that
a News broker outage, or a one-shot migration container that has not been rerun since the last boot,
can never be what keeps the process holding a live Binance position from coming back. The
replacement is stronger than the ordering edge it removed: the ordering edge only proved that a
migration ran at some point during this boot, while reading `alembic_version` proves the database is
at the exact head this image was built for.

What it must cost when it fails is the point of these tests. A stale image must take no advisory
lock and construct no `TradingNode`: taking the lock would lock a healthy image out of its own
account slot, and building the node would open credentials against a schema this build cannot read.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from tracefold.app.nautilus import root as nautilus_root
from tracefold.integrations.nautilus.oi_runtime.config import BinanceRuntimeCredentials
from tracefold.platform.config.models import Settings


class _RecordingConnection:
    """The smallest connection the startup probe can run against, recording every statement."""

    def __init__(self, *, migration_version: str | None) -> None:
        self.statements: list[str] = []
        self._migration_version = migration_version

    def execute(self, statement: str, *_arguments: Any) -> _RecordingConnection:
        self.statements.append(statement)
        return self

    def fetchone(self) -> dict[str, Any] | None:
        statement = self.statements[-1]
        if "SELECT 1 AS ok" in statement:
            return {"ok": 1}
        if "alembic_version" in statement:
            return None if self._migration_version is None else {"version_num": self._migration_version}
        raise AssertionError(f"unexpected statement before the schema head was proven: {statement}")

    def commit(self) -> None:
        self.statements.append("COMMIT")

    def rollback(self) -> None:
        self.statements.append("ROLLBACK")


def _arrange(monkeypatch: pytest.MonkeyPatch, *, migration_version: str | None) -> _RecordingConnection:
    conn = _RecordingConnection(migration_version=migration_version)

    @contextmanager
    def _connection(_settings: Any, **kwargs: Any) -> Any:
        # The singleton session is the one connection that stays open for the life of the process,
        # so it is the one that must carry keepalives (#537 D2).
        assert kwargs.get("long_lived") is True
        yield conn

    monkeypatch.setattr(nautilus_root, "postgres_connection", _connection)
    monkeypatch.setattr(
        nautilus_root,
        "_read_credentials",
        lambda _settings: BinanceRuntimeCredentials(api_key="k" * 24, api_secret="s" * 24),
    )
    return conn


def _paper_settings() -> Settings:
    return Settings(trading={"execution": {"mode": "paper"}})


def test_a_stale_schema_head_stops_the_runtime_before_it_takes_the_account_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _arrange(monkeypatch, migration_version="20260101_0001")

    with pytest.raises(RuntimeError, match="oi_runtime_schema_head_mismatch"):
        nautilus_root.run_nautilus(_paper_settings())

    assert not any("pg_try_advisory_lock" in statement for statement in conn.statements)
    assert conn.statements[:2] == ["SELECT 1 AS ok", "SELECT version_num FROM alembic_version LIMIT 1"]


def test_an_unmigrated_database_is_a_head_mismatch_not_a_silent_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _arrange(monkeypatch, migration_version=None)

    with pytest.raises(RuntimeError, match="oi_runtime_schema_head_mismatch"):
        nautilus_root.run_nautilus(_paper_settings())

    assert not any("pg_try_advisory_lock" in statement for statement in conn.statements)


def test_an_unreadable_schema_probe_is_its_own_named_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _arrange(monkeypatch, migration_version="20260101_0001")

    def _broken(_statement: str, *_arguments: Any) -> Any:
        conn.statements.append(_statement)
        raise RuntimeError("connection is closed")

    monkeypatch.setattr(conn, "execute", _broken)

    with pytest.raises(RuntimeError, match="oi_runtime_schema_probe_failed"):
        nautilus_root.run_nautilus(_paper_settings())

    assert not any("pg_try_advisory_lock" in statement for statement in conn.statements)
