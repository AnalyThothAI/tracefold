"""The hard-cut execution image requires an exact schema before owning the account slot.

A mismatch, whether older or unrecognized newer, stops before the account lock and
before construction of the Binance node. Deployment must switch the matching
schema and image together after execution writers are stopped.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from alembic.script import ScriptDirectory

from tracefold.app.nautilus import root as nautilus_root
from tracefold.integrations.nautilus.oi_runtime.config import BinanceRuntimeCredentials
from tracefold.platform.config.models import Settings
from tracefold.platform.postgres.migrations import alembic_config, latest_migration_version


def _revision_this_image_has_already_passed() -> str:
    """A real ancestor of the code head, so ancestry is what decides and not an unknown string."""

    ancestry = [
        script.revision
        for script in ScriptDirectory.from_config(alembic_config()).walk_revisions("base", latest_migration_version())
    ]
    assert len(ancestry) > 1, "a single-revision history cannot express 'older'"
    return ancestry[-1]


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
    return Settings(trading={"execution": {"enabled": True, "binance": {"environment": "DEMO"}}})


def test_a_database_behind_this_image_stops_the_runtime_before_it_takes_the_account_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _arrange(monkeypatch, migration_version=_revision_this_image_has_already_passed())

    with pytest.raises(RuntimeError, match="oi_runtime_schema_head_mismatch"):
        nautilus_root.run_nautilus(_paper_settings())

    assert not any("pg_try_advisory_lock" in statement for statement in conn.statements)
    assert conn.statements[:2] == ["SELECT 1 AS ok", "SELECT version_num FROM alembic_version LIMIT 1"]


def test_a_database_ahead_of_this_image_is_refused_before_the_account_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _arrange(monkeypatch, migration_version="29991231_9999")
    with pytest.raises(RuntimeError, match="oi_runtime_schema_head_mismatch"):
        nautilus_root.run_nautilus(_paper_settings())
    assert not any("pg_try_advisory_lock" in statement for statement in conn.statements)


def test_an_exact_schema_reaches_the_account_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _arrange(monkeypatch, migration_version=latest_migration_version())
    with pytest.raises(AssertionError, match="unexpected statement before the schema head was proven"):
        nautilus_root.run_nautilus(_paper_settings())
    assert any("pg_try_advisory_lock" in statement for statement in conn.statements)


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
