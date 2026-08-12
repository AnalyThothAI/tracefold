from __future__ import annotations

import importlib

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0258_installs_reconcile_cursor_and_covering_hot_path_indexes() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260813_0257")
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, enabled, consecutive_failures,
              created_at_ms, updated_at_ms, source_kind, live_connected
            ) VALUES (
              'news-opennews', 'OpenNews', 1, 'en', true, 0,
              1, 1, 'opennews', false
            );
            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, provider_score_updated_at_ms,
              reporting_origin, title, description, lang, published_at_ms,
              first_observed_at_ms, last_observed_at_ms, content_fingerprint,
              level, category, classification_source,
              classification_confidence, importance_score,
              importance_factors, active, created_at_ms, updated_at_ms
            ) VALUES (
              'clock-item', 'news-opennews', 'clock-item', 'clock-item',
              '{"score":88,"coins":[{"symbol":"BTC","market_type":"spot"}]}'::jsonb,
              123, 'wire', 'Clock item', '', 'en', 100, 100, 999,
              'clock-fingerprint', 'info', 'general', 'keyword', 1, 1,
              '{}'::jsonb, true, 100, 999
            );
            """
        )
        conn.commit()

        command.upgrade(config, "20260813_0258")

        column = conn.execute(
            """
            SELECT data_type, is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'news_push_state'
               AND column_name = 'reconcile_cursor_story_id'
            """
        ).fetchone()
        cursor_value = conn.execute(
            """
            SELECT reconcile_cursor_story_id
              FROM news_push_state
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        eligibility_clock = conn.execute(
            """
            SELECT push_eligibility_updated_at_ms
              FROM news_items
             WHERE item_id = 'clock-item'
            """
        ).fetchone()
        cursor_foreign_keys = conn.execute(
            """
            SELECT count(*) AS count
              FROM pg_constraint constraint_row
              JOIN pg_class relation ON relation.oid = constraint_row.conrelid
             WHERE relation.relname = 'news_push_state'
               AND constraint_row.contype = 'f'
               AND constraint_row.conkey @> ARRAY[
                 (
                   SELECT attnum
                     FROM pg_attribute
                    WHERE attrelid = relation.oid
                      AND attname = 'reconcile_cursor_story_id'
                 )
               ]::smallint[]
            """
        ).fetchone()
        index_rows = conn.execute(
            """
            SELECT indexname, indexdef
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND indexname = ANY(%s)
             ORDER BY indexname
            """,
            (["idx_events_received", "ux_token_intent_current_resolution"],),
        ).fetchall()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()

    assert column == {"data_type": "text", "is_nullable": "YES"}
    assert cursor_value == {"reconcile_cursor_story_id": None}
    assert eligibility_clock == {"push_eligibility_updated_at_ms": 123}
    assert cursor_foreign_keys == {"count": 0}
    indexes = {str(row["indexname"]): " ".join(str(row["indexdef"]).split()) for row in index_rows}
    assert indexes["idx_events_received"] == (
        "CREATE INDEX idx_events_received ON public.events USING btree (received_at_ms, event_id)"
    )
    assert indexes["ux_token_intent_current_resolution"] == (
        "CREATE UNIQUE INDEX ux_token_intent_current_resolution "
        "ON public.token_intent_resolutions USING btree (intent_id) "
        "INCLUDE (resolution_status, target_type, target_id) WHERE (is_current = true)"
    )
    assert version == {"version_num": "20260813_0258"}


def test_0258_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260813_0258_news_push_reconcile_and_market_target_indexes"
    )

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
