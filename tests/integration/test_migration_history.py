"""The whole historical migration chain still runs, and it runs from the flat package.

#373 moved the production package to the repository root, which moved `script_location`, the
`versions/` tree and the runtime SQL with it. `alembic.ini` is a string: a stale `script_location`
does not fail to compile, it fails to find revisions, and a chain that silently loses its tail can
still stamp a plausible-looking version. The other migration modules each start from a named
revision, so none of them walks the chain end to end.

This module does exactly that walk — empty database, every revision in order, one at a time — and
asserts the reachable graph matches the revision files on disk, so a revision that stops being
reachable from the flat package surfaces here rather than at deploy time.

On downgrade: this repository's migrations are hard cuts by contract, and 48 of the 55 revisions
declare their `downgrade()` irreversible. A full historical downgrade is therefore not a thing the
chain can do, and pretending otherwise would need either a fake downgrade or a skipped assertion.
What is checkable — and what is checked here — is that each revision's declared downgrade behaviour
is reached through the flat package and matches what its source says it is.
"""

from __future__ import annotations

import ast
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


def _config():
    config = alembic_config()
    config.attributes["database_url"] = postgres_test_dsn()
    return config


def _stamped_revision() -> str | None:
    conn = connect_postgres_test(read_only=False)
    try:
        if conn.execute("SELECT to_regclass('alembic_version') AS table_name").fetchone()["table_name"] is None:
            return None
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()
    return str(rows[0]["version_num"]) if rows else None


def _empty_the_schema() -> None:
    """The module-scoped database is shared, so each behavioural test states the state it needs."""

    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("ALTER SCHEMA public OWNER TO tracefold_owner")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    prepare_test_migration_database(admin_postgres_test_dsn())


def _base_to_head(script: ScriptDirectory) -> tuple[str, ...]:
    return tuple(revision.revision for revision in reversed(list(script.walk_revisions())))


def _declares_an_irreversible_downgrade(revision: str) -> bool:
    """Read the revision's own source: a lone `raise` is this repository's irreversible contract."""

    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assigned = {
            target.id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        declared = assigned.get("revision")
        if not isinstance(declared, ast.Constant) or declared.value != revision:
            continue
        downgrade = next(
            (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "downgrade"),
            None,
        )
        assert downgrade is not None, f"{path.name} has no downgrade()"
        body = [
            statement
            for statement in downgrade.body
            if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
        ]
        return len(body) == 1 and isinstance(body[0], ast.Raise)
    raise AssertionError(f"no revision file declares revision {revision}")


def test_the_migration_tree_resolves_inside_the_flat_package() -> None:
    script = ScriptDirectory.from_config(_config())

    assert Path(script.dir).resolve() == (VERSIONS.parent).resolve()
    assert Path(script.dir).resolve().is_relative_to(ROOT / "tracefold")


def test_the_migration_tree_resolves_from_a_working_directory_that_is_not_the_repository() -> None:
    """Alembic resolves a bare relative `script_location` against the current directory.

    Asserting resolution while pytest happens to run from the repository root proves only that the
    root is the root. `migrations.py` already resolves `alembic.ini` absolutely from its own
    `__file__` so that a caller's working directory cannot matter; before #373 the `script_location`
    inside that file quietly reintroduced the dependency, and this is what noticed.
    """

    origin = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="tracefold-alembic-cwd-") as elsewhere:
        os.chdir(elsewhere)
        try:
            script = ScriptDirectory.from_config(alembic_config())
            resolved = Path(script.dir).resolve()
        finally:
            os.chdir(origin)

    assert resolved == VERSIONS.parent.resolve()


def test_the_reachable_revision_graph_is_the_revision_files_on_disk() -> None:
    script = ScriptDirectory.from_config(_config())
    chain = _base_to_head(script)

    assert len(chain) == len(sorted(VERSIONS.glob("*.py")))
    assert len(set(chain)) == len(chain)
    assert chain[-1] == script.get_current_head()
    assert script.get_revision(chain[0]).down_revision is None


def test_every_historical_revision_upgrades_in_order_from_an_empty_database() -> None:
    config = _config()
    script = ScriptDirectory.from_config(config)
    chain = _base_to_head(script)
    _empty_the_schema()

    assert _stamped_revision() is None

    for revision in chain:
        command.upgrade(config, revision)
        assert _stamped_revision() == revision

    assert _stamped_revision() == script.get_current_head()


def test_the_head_downgrade_behaves_exactly_as_its_source_declares() -> None:
    config = _config()
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    assert head is not None
    _empty_the_schema()
    command.upgrade(config, "head")
    assert _stamped_revision() == head

    if _declares_an_irreversible_downgrade(head):
        with pytest.raises(RuntimeError):
            command.downgrade(config, "-1")
        assert _stamped_revision() == head
    else:
        command.downgrade(config, "-1")
        assert _stamped_revision() == script.get_revision(head).down_revision
