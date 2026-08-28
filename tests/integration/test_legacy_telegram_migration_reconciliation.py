from __future__ import annotations

import psycopg
import pytest
from alembic import command
from psycopg import sql
from sqlalchemy.engine import make_url

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tracefold.platform.postgres.migrations import alembic_config, upgrade_head

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

REMOTE_COMMON_PARENT = "20260828_0316"
COLLIDING_LOCAL_HEAD = "20260828_0318"
CURRENT_HEAD = "20260828_0323"
MIGRATE_ROLE_PASSWORD = "M" * 43


def _fresh_schema_at(revision: str) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_app') THEN
                CREATE ROLE tracefold_app NOLOGIN;
              ELSE
                ALTER ROLE tracefold_app NOLOGIN;
              END IF;
            END
            $$
            """
        )
        conn.execute("GRANT tracefold_owner TO tracefold_migrate WITH ADMIN FALSE")
        conn.execute("GRANT tracefold_owner TO tracefold_migrate WITH INHERIT FALSE")
        conn.execute("GRANT tracefold_owner TO tracefold_migrate WITH SET TRUE")
        conn.execute(sql.SQL("ALTER ROLE tracefold_migrate PASSWORD {}").format(sql.Literal(MIGRATE_ROLE_PASSWORD)))
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public AUTHORIZATION tracefold_owner")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        # Extensions are cluster/bootstrap concerns: the NOINHERIT migration role
        # deliberately cannot create them on a fresh production database.
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public")
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
        conn.commit()
    finally:
        conn.close()
    config = alembic_config()
    # Bootstrap the baseline as the test-database superuser, then run every
    # chained migration as the locked-down migration role, matching production.
    config.attributes["database_url"] = postgres_test_dsn()
    command.upgrade(config, "20260818_0275")
    migrate_config = alembic_config()
    migrate_config.attributes["database_url"] = _migrate_role_dsn()
    command.upgrade(migrate_config, revision)


def _install_colliding_local_delivery_migrations() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            ALTER TABLE news_deliveries
              ADD COLUMN edit_state TEXT,
              ADD COLUMN pending_card JSONB,
              ADD COLUMN edit_error_code TEXT,
              ADD COLUMN edit_attempted_at_ms BIGINT,
              ADD COLUMN edit_settled_at_ms BIGINT,
              ADD CONSTRAINT news_deliveries_edit_state_check CHECK (
                edit_state IS NULL OR edit_state IN ('editing', 'edited', 'ambiguous')
              ),
              ADD CONSTRAINT news_deliveries_edit_shape_check CHECK (
                (edit_state IS NULL AND pending_card IS NULL AND edit_error_code IS NULL
                 AND edit_attempted_at_ms IS NULL AND edit_settled_at_ms IS NULL)
                OR
                (edit_state = 'editing' AND pending_card IS NOT NULL AND edit_error_code IS NULL
                 AND edit_attempted_at_ms IS NOT NULL AND edit_settled_at_ms IS NULL)
                OR
                (edit_state = 'edited' AND pending_card IS NULL AND edit_error_code IS NULL
                 AND edit_attempted_at_ms IS NOT NULL AND edit_settled_at_ms IS NOT NULL)
                OR
                (edit_state = 'ambiguous' AND pending_card IS NOT NULL AND edit_error_code IS NOT NULL
                 AND edit_attempted_at_ms IS NOT NULL AND edit_settled_at_ms IS NOT NULL)
              )
            """
        )
        conn.execute(
            """
            CREATE INDEX ix_news_deliveries_editing
              ON news_deliveries (edit_attempted_at_ms, event_id)
              WHERE edit_state = 'editing'
            """
        )
        conn.execute(
            """
            ALTER TABLE news_deliveries
              ADD COLUMN delete_state TEXT,
              ADD COLUMN delete_evidence JSONB,
              ADD COLUMN delete_reason TEXT,
              ADD COLUMN delete_error_code TEXT,
              ADD COLUMN delete_attempted_at_ms BIGINT,
              ADD COLUMN delete_settled_at_ms BIGINT,
              ADD CONSTRAINT news_deliveries_delete_state_check CHECK (
                delete_state IS NULL OR delete_state IN ('deleting', 'deleted', 'ambiguous')
              ),
              ADD CONSTRAINT news_deliveries_delete_shape_check CHECK (
                (delete_state IS NULL AND delete_evidence IS NULL AND delete_reason IS NULL
                 AND delete_error_code IS NULL AND delete_attempted_at_ms IS NULL
                 AND delete_settled_at_ms IS NULL)
                OR
                (delete_state = 'deleting' AND delete_evidence IS NOT NULL AND delete_reason IS NOT NULL
                 AND delete_error_code IS NULL AND delete_attempted_at_ms IS NOT NULL
                 AND delete_settled_at_ms IS NULL)
                OR
                (delete_state = 'deleted' AND delete_evidence IS NOT NULL AND delete_reason IS NOT NULL
                 AND delete_error_code IS NULL AND delete_attempted_at_ms IS NOT NULL
                 AND delete_settled_at_ms IS NOT NULL)
                OR
                (delete_state = 'ambiguous' AND delete_evidence IS NOT NULL AND delete_reason IS NOT NULL
                 AND delete_error_code IS NOT NULL AND delete_attempted_at_ms IS NOT NULL
                 AND delete_settled_at_ms IS NOT NULL)
              )
            """
        )
        conn.execute(
            """
            CREATE INDEX ix_news_deliveries_deleting
              ON news_deliveries (delete_attempted_at_ms, event_id)
              WHERE delete_state = 'deleting'
            """
        )
        conn.execute("UPDATE alembic_version SET version_num = %s", (COLLIDING_LOCAL_HEAD,))
        conn.commit()
    finally:
        conn.close()


def _migrate_role_dsn() -> str:
    return (
        make_url(postgres_test_dsn())
        .set(
            username="tracefold_migrate",
            password=MIGRATE_ROLE_PASSWORD,
        )
        .render_as_string(hide_password=False)
    )


def test_colliding_local_0318_is_reconciled_without_dropping_telegram_state() -> None:
    _fresh_schema_at(REMOTE_COMMON_PARENT)
    _install_colliding_local_delivery_migrations()

    with psycopg.connect(_migrate_role_dsn()) as role_conn:
        role_identity = role_conn.execute(
            """
            SELECT current_user,
                   EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_owner'),
                   pg_has_role(current_user, 'tracefold_owner', 'SET')
            """
        ).fetchone()
    assert role_identity == ("tracefold_migrate", True, True)

    upgrade_head(_migrate_role_dsn())

    conn = connect_postgres_test(read_only=False)
    try:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        epochs = {
            row["epoch_id"]
            for row in conn.execute(
                "SELECT epoch_id FROM news_learning_epochs WHERE epoch_id IN ('program_v8', 'program_v9')"
            ).fetchall()
        }
        state_constraint = conn.execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
              FROM pg_constraint
             WHERE conrelid = 'trading_cases'::regclass
               AND conname = 'trading_cases_state_check'
            """
        ).fetchone()["definition"]
        delivery_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = 'news_deliveries'
                """
            ).fetchall()
        }
        delivery_indexes = {
            row["indexname"]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'news_deliveries'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert version == CURRENT_HEAD
    assert epochs == {"program_v8", "program_v9"}
    assert "INTENT_EMITTED" in state_constraint
    assert {
        "edit_state",
        "pending_card",
        "edit_error_code",
        "edit_attempted_at_ms",
        "edit_settled_at_ms",
        "delete_state",
        "delete_evidence",
        "delete_reason",
        "delete_error_code",
        "delete_attempted_at_ms",
        "delete_settled_at_ms",
    } <= delivery_columns
    assert {"ix_news_deliveries_editing", "ix_news_deliveries_deleting"} <= delivery_indexes


def test_unrecognized_0318_lineage_fails_before_alembic_can_guess() -> None:
    _fresh_schema_at(REMOTE_COMMON_PARENT)
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("UPDATE alembic_version SET version_num = %s", (COLLIDING_LOCAL_HEAD,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="legacy_migration_lineage_unrecognized"):
        upgrade_head(postgres_test_dsn())
