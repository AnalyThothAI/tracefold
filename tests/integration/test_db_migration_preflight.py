from __future__ import annotations

import pytest
from psycopg.conninfo import make_conninfo

from tracefold.app.cli.commands import db
from tracefold.platform.postgres.client import connect_postgres

pytestmark = [pytest.mark.integration, pytest.mark.migration]


def _as_migrator(dsn: str) -> str:
    return make_conninfo(
        dsn,
        options="-c role=tracefold_migrate -c default_transaction_read_only=on",
    )


def test_fresh_install_probe_works_through_noinherit_migrator(postgres_clone_dsn: str) -> None:
    migrator_dsn = _as_migrator(postgres_clone_dsn)
    with connect_postgres(migrator_dsn) as conn:
        identity = conn.execute(
            "SELECT current_user AS role_name, "
            "has_table_privilege(current_user, 'public.alembic_version', 'SELECT') AS can_read_version"
        ).fetchone()
    assert identity == {"role_name": "tracefold_migrate", "can_read_version": False}
    assert db._database_is_unmigrated(migrator_dsn) is False

    with connect_postgres(postgres_clone_dsn) as conn:
        conn.execute("DELETE FROM public.alembic_version")
    assert db._database_is_unmigrated(migrator_dsn) is False

    with connect_postgres(postgres_clone_dsn) as conn:
        conn.execute("DROP TABLE public.alembic_version")

    assert db._database_is_unmigrated(migrator_dsn) is True
