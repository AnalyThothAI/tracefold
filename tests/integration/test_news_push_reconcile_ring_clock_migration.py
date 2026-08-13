from __future__ import annotations

import importlib

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0260_preserves_an_active_cursor_with_a_durable_ring_clock() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260813_0259")
        with conn.transaction():
            conn.execute(
                """
                UPDATE news_push_state
                   SET reconcile_cursor_story_id = %s,
                       updated_at_ms = 123
                 WHERE singleton_key = 'current'
                """,
                ("a" * 64,),
            )

        command.upgrade(config, "20260813_0260")

        state = conn.execute(
            """
            SELECT reconcile_cursor_story_id, reconcile_cycle_started_at_ms
              FROM news_push_state
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert state == {
            "reconcile_cursor_story_id": "a" * 64,
            "reconcile_cycle_started_at_ms": 123,
        }
        assert version == {"version_num": "20260813_0260"}

        with pytest.raises(CheckViolation):
            conn.execute(
                """
                UPDATE news_push_state
                   SET reconcile_cycle_started_at_ms = NULL
                 WHERE singleton_key = 'current'
                """
            )
        conn.rollback()
    finally:
        conn.close()


def test_0260_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260813_0260_news_push_reconcile_ring_clock"
    )

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
