from __future__ import annotations

import importlib

import pytest

from tests.postgres_test_utils import connect_postgres_test, prepare_postgres_database


def test_0274_removes_every_token_radar_schema_object() -> None:
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=True)
    try:
        table = conn.execute("SELECT to_regclass('token_radar_current') AS name").fetchone()
        indexes = conn.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname IN (
                'idx_events_token_radar_source_time',
                'idx_token_intent_resolutions_token_radar_material'
              )
            """
        ).fetchall()
        column = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'events'
              AND column_name = 'token_radar_text_fingerprint'
            """
        ).fetchone()
    finally:
        conn.close()

    assert table["name"] is None
    assert indexes == []
    assert column is None


def test_0274_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260818_0274_token_radar_removal_hard_cut"
    )

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
