from __future__ import annotations

import pytest

from tests.postgres_test_utils import postgres_migration_test_dsn
from tracefold.app.cli.commands import db
from tracefold.platform.postgres.client import connect_postgres
from tracefold.platform.postgres.migrations import upgrade_head

pytestmark = [pytest.mark.integration, pytest.mark.migration]


def test_fresh_install_probe_works_through_application_login(postgres_clone_dsn: str) -> None:
    owner_dsn = postgres_migration_test_dsn(postgres_clone_dsn)
    with connect_postgres(owner_dsn) as conn:
        identity = conn.execute(
            "SELECT current_user AS role_name, "
            "has_table_privilege(current_user, 'public.alembic_version', 'SELECT') AS can_read_version"
        ).fetchone()
    assert identity == {"role_name": "tracefold", "can_read_version": True}
    assert db._database_is_unmigrated(owner_dsn) is False

    with connect_postgres(postgres_clone_dsn) as conn:
        conn.execute("DELETE FROM public.alembic_version")
    assert db._database_is_unmigrated(owner_dsn) is False

    with connect_postgres(postgres_clone_dsn) as conn:
        conn.execute("DROP TABLE public.alembic_version")

    assert db._database_is_unmigrated(owner_dsn) is True


def test_alembic_refuses_a_non_owner_connection(postgres_clone_dsn: str) -> None:
    with pytest.raises(RuntimeError, match="migration_owner_identity_required"):
        upgrade_head(postgres_clone_dsn)
