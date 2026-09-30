from __future__ import annotations

import psycopg
import pytest

from tests.postgres_test_utils import postgres_migration_test_dsn, prepare_test_migration_database
from tracefold.app.restore_storage import run_restore_drill
from tracefold.platform.postgres.restore_drill import POSTGRES_PRODUCTION_IMAGE, _restored_function_search_path

pytestmark = pytest.mark.integration


@pytest.mark.scheduled
def test_production_image_dump_restore_migrate_audit_and_smoke(postgres_server_dsn: str) -> None:
    prepare_test_migration_database(postgres_server_dsn)
    evidence = run_restore_drill(postgres_server_dsn, postgres_migration_test_dsn(postgres_server_dsn))

    assert evidence["ok"] is True
    assert evidence["image_identity"] == POSTGRES_PRODUCTION_IMAGE
    assert evidence["source_head"] == evidence["restored_head"]
    assert all(evidence["smoke"].values())
    assert evidence["smoke"]["trading_case_fact"] is True
    assert evidence["smoke"]["trading_signal_fact"] is True
    assert evidence["audit"] == {
        "mode": "deep",
        "migration_status": "ready",
        "news_schema_exact": True,
        "trading_schema_exact": True,
    }


@pytest.mark.parametrize("copy_failed", [False, True])
def test_restore_copy_search_paths_revert_and_preserve_explicit_settings(postgres_clone_dsn, copy_failed):
    with psycopg.connect(postgres_clone_dsn, autocommit=True) as conn:
        conn.execute("CREATE FUNCTION public.restore_path_unset(integer) RETURNS integer LANGUAGE SQL AS 'SELECT $1'")
        conn.execute(
            "CREATE FUNCTION public.restore_path_explicit() RETURNS integer LANGUAGE SQL "
            "SET search_path = pg_catalog AS 'SELECT 1'"
        )

        def configurations():
            return conn.execute(
                "SELECT proname,proconfig FROM pg_proc WHERE proname LIKE 'restore_path_%' ORDER BY proname"
            ).fetchall()

        before = configurations()

        def copy():
            with _restored_function_search_path(postgres_clone_dsn):
                during = dict(configurations())
                assert during["restore_path_unset"] == ["search_path=pg_catalog, public"]
                assert during["restore_path_explicit"] == ["search_path=pg_catalog"]
                if copy_failed:
                    raise RuntimeError("recorded_copy_failure")

        if copy_failed:
            with pytest.raises(RuntimeError, match="recorded_copy_failure"):
                copy()
        else:
            copy()
        assert configurations() == before
