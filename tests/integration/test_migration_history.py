"""The migration tree is one irreversible baseline plus ordered hard cuts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from tests.postgres_test_utils import connect_postgres_test, prepare_test_migration_database
from tests.postgres_test_utils import postgres_migration_test_dsn as postgres_test_dsn
from tests.postgres_test_utils import test_postgres_dsn as admin_postgres_test_dsn
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "tracefold" / "platform" / "postgres" / "alembic" / "versions"
BASELINE = "20260831_0340"
HEAD = "20260901_0342"


def _config():
    config = alembic_config()
    config.attributes["database_url"] = postgres_test_dsn()
    return config


def _empty_the_schema() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()
    prepare_test_migration_database(admin_postgres_test_dsn())


def _stamped_revision() -> str | None:
    conn = connect_postgres_test(read_only=False)
    try:
        if conn.execute("SELECT to_regclass('alembic_version') AS table_name").fetchone()["table_name"] is None:
            return None
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return None if row is None else str(row["version_num"])
    finally:
        conn.close()


def test_migration_tree_is_one_root_and_head_in_the_flat_package() -> None:
    script = ScriptDirectory.from_config(_config())
    revisions = list(script.walk_revisions())

    assert Path(script.dir).resolve() == VERSIONS.parent.resolve()
    assert [revision.revision for revision in revisions] == [HEAD, "20260901_0341", BASELINE]
    assert revisions[0].down_revision == "20260901_0341"
    assert revisions[1].down_revision == BASELINE
    assert revisions[2].down_revision is None
    assert sorted(path.name for path in VERSIONS.glob("*.py")) == [
        "20260831_0340_baseline.py",
        "20260901_0341_trading_signal_hard_cut.py",
        "20260901_0342_trading_notification_deliveries.py",
    ]


def test_migration_tree_resolves_outside_the_repository() -> None:
    origin = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="tracefold-alembic-cwd-") as elsewhere:
        os.chdir(elsewhere)
        try:
            resolved = Path(ScriptDirectory.from_config(alembic_config()).dir).resolve()
        finally:
            os.chdir(origin)

    assert resolved == VERSIONS.parent.resolve()


def test_fresh_database_upgrades_through_baseline_and_signal_cut() -> None:
    config = _config()
    _empty_the_schema()
    assert _stamped_revision() is None

    command.upgrade(config, "head")
    assert _stamped_revision() == HEAD
    command.upgrade(config, "head")
    assert _stamped_revision() == HEAD


def test_current_head_downgrade_is_irreversible() -> None:
    config = _config()
    _empty_the_schema()
    command.upgrade(config, "head")

    with pytest.raises(RuntimeError, match="irreversible Trading notification delivery ledger"):
        command.downgrade(config, "base")

    assert _stamped_revision() == HEAD
