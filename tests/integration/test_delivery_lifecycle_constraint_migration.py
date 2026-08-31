from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy.exc import ProgrammingError

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_migration_test_dsn,
    prepare_test_migration_database,
)
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_0324_refuses_an_invalid_lifecycle_row_admitted_by_0323_without_advancing() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("ALTER SCHEMA public OWNER TO tracefold_owner")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    prepare_test_migration_database(postgres_test_dsn())

    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, "20260828_0323")

    event_id = "delivery-lifecycle-0324"
    item_id = "delivery-lifecycle-0324-item"
    now_ms = 1_788_000_000_000
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_items (
              item_id, source_id, source_item_key, title, published_at_ms, observed_at_ms,
              provider_metadata, first_ingest_mode, created_at_ms, updated_at_ms
            ) VALUES (%s, 'opennews', %s, 'Delivery lifecycle migration fixture', %s, %s,
                      '{}'::jsonb, 'live', %s, %s)
            """,
            (item_id, item_id, now_ms, now_ms, now_ms, now_ms),
        )
        conn.execute(
            """
            INSERT INTO news_events (
              event_id, leader_item_id, family, comparison_fingerprint, comparison_title, leader_title,
              focus_fact_id, opened_at_ms, last_member_at_ms, expires_at_ms, admission, storyline_key,
              ingest_mode, grounded_assets, event_kind, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, 'general', %s, 'delivery lifecycle', 'Delivery lifecycle migration fixture',
                      %s, %s, %s, %s, 'candidate', 'delivery:lifecycle',
                      'live', '[]'::jsonb, 'news', %s, %s)
            """,
            (
                event_id,
                item_id,
                event_id,
                f"fact:{event_id}",
                now_ms,
                now_ms,
                now_ms + 3_600_000,
                now_ms,
                now_ms,
            ),
        )
        conn.execute(
            """
            INSERT INTO news_deliveries(
              event_id, kind, state, card, receipt, error_code,
              attempted_at_ms, settled_at_ms, created_at_ms,
              edit_state, pending_card, edit_error_code,
              edit_attempted_at_ms, edit_settled_at_ms
            ) VALUES (
              %s, 'first', 'sent', '{}'::jsonb, NULL, NULL,
              1, 2, 1,
              NULL, '{}'::jsonb, NULL, 1, NULL
            )
            """,
            (event_id,),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        with pytest.raises(ProgrammingError, match="news_delivery_lifecycle_shape_invalid"):
            command.upgrade(config, "20260828_0324")
        conn = connect_postgres_test(read_only=False)
        try:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == (
                "20260828_0323"
            )
            conn.execute("DELETE FROM news_deliveries WHERE event_id = %s", (event_id,))
            conn.commit()
        finally:
            conn.close()
    finally:
        command.upgrade(config, "20260828_0324")
