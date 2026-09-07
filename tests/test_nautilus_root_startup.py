"""The execution runtime asserts the schema direction before it becomes the account-slot owner.

#537 PR-2 took the `migrate: service_completed_successfully` edge off the `nautilus` service so that
a News broker outage, or a one-shot migration container that has not been rerun since the last boot,
can never be what keeps the process holding a live Binance position from coming back. The
replacement reads `alembic_version`, which proves what the database is rather than that some
migration ran during this boot.

Head *equality* then proved one revision too much (#598 D5-c). The application and the runtime are
deployed separately on purpose -- `make up` migrates, `make runtime-up` does not -- so the ordinary
release order leaves the live database ahead of a runtime image that reads it perfectly well, and
the equality check turned that into a refusal to restart the process holding an open position. What
the runtime cannot survive is the other direction, a database missing migrations this build was
compiled against, so ancestry of the code head is what these tests pin.

What a refusal must cost is the rest of the point. An image that cannot read the schema takes no
advisory lock and constructs no `TradingNode`: taking the lock would lock a healthy image out of
its own account slot, and building the node would open credentials against a schema it cannot read.
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
    return Settings(trading={"execution": {"mode": "paper"}})


def test_a_database_behind_this_image_stops_the_runtime_before_it_takes_the_account_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _arrange(monkeypatch, migration_version=_revision_this_image_has_already_passed())

    with pytest.raises(RuntimeError, match="oi_runtime_schema_head_mismatch"):
        nautilus_root.run_nautilus(_paper_settings())

    assert not any("pg_try_advisory_lock" in statement for statement in conn.statements)
    assert conn.statements[:2] == ["SELECT 1 AS ok", "SELECT version_num FROM alembic_version LIMIT 1"]


def test_a_database_ahead_of_this_image_lets_the_runtime_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forward-migrated database a normal release produces, which the equality check refused.

    `make up` applies the new revisions; the runtime keeps running its own image until an operator
    chooses `make runtime-up`. Restarting that image -- the one holding the open position -- must
    not be the thing the check stops. A revision this image has never heard of can only have been
    written by a newer deploy, so it is ahead, and the runtime proceeds past the probe.
    """

    conn = _arrange(monkeypatch, migration_version="29991231_9999")

    # Past the schema probe, `run_nautilus` goes on to the account-slot lock, which this connection
    # deliberately cannot serve. Reaching that statement at all is the proof the probe let it by.
    with pytest.raises(AssertionError, match="unexpected statement before the schema head was proven"):
        nautilus_root.run_nautilus(_paper_settings())

    assert conn.statements[:2] == ["SELECT 1 AS ok", "SELECT version_num FROM alembic_version LIMIT 1"]
    assert any("pg_try_advisory_lock" in statement for statement in conn.statements)


def test_the_direction_test_is_ancestry_and_not_string_inequality() -> None:
    head = latest_migration_version()

    assert nautilus_root._database_precedes_image(None, image_head=head) is True
    assert nautilus_root._database_precedes_image("", image_head=head) is True
    assert nautilus_root._database_precedes_image(head, image_head=head) is False
    assert nautilus_root._database_precedes_image(_revision_this_image_has_already_passed(), image_head=head) is True
    # Not in this image's history at all: a newer deploy wrote it, so it is ahead.
    assert nautilus_root._database_precedes_image("29991231_9999", image_head=head) is False


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
