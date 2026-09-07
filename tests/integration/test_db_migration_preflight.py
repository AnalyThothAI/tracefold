from __future__ import annotations

import pytest

from tests.postgres_test_utils import postgres_migration_test_dsn
from tracefold.platform.postgres.client import connect_postgres
from tracefold.platform.postgres.migrations import upgrade_head

pytestmark = [pytest.mark.integration, pytest.mark.migration]


def test_the_migration_login_owns_the_schema_and_can_read_its_revision(postgres_clone_dsn: str) -> None:
    """`tracefold db migrate` is `upgrade_head(dsn)` and nothing else (#598 D5-d).

    The fresh-install probe that used to run first -- reading `to_regclass('public.alembic_version')`
    to decide whether to assemble News genesis cutover evidence -- is deleted with the genesis
    broker preflight it fed, and nothing read the environment variables it wrote. What is left of a
    migration precondition is real and unchanged: the connection Alembic gets is the owning
    application login, and it can read the revision table.
    """

    owner_dsn = postgres_migration_test_dsn(postgres_clone_dsn)

    with connect_postgres(owner_dsn) as conn:
        identity = conn.execute(
            "SELECT current_user AS role_name, "
            "has_table_privilege(current_user, 'public.alembic_version', 'SELECT') AS can_read_version"
        ).fetchone()

    assert identity == {"role_name": "tracefold", "can_read_version": True}


def test_alembic_refuses_a_non_owner_connection(postgres_clone_dsn: str) -> None:
    with pytest.raises(RuntimeError, match="migration_owner_identity_required"):
        upgrade_head(postgres_clone_dsn)
