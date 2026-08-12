from __future__ import annotations

import importlib

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0259_repairs_numeric_score_clock_and_enforces_the_invariant() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260813_0258")
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
              push_eligibility_updated_at_ms,
              reporting_origin, title, description, lang, published_at_ms,
              first_observed_at_ms, last_observed_at_ms, content_fingerprint,
              level, category, classification_source,
              classification_confidence, importance_score,
              importance_factors, active, created_at_ms, updated_at_ms
            ) VALUES (
              'missing-clock', 'news-opennews', 'missing-clock', 'missing-clock',
              '{"score":88,"coins":[{"symbol":"BTC","market_type":"spot"}]}'::jsonb,
              123, NULL,
              'wire', 'Missing clock', '', 'en', 100,
              100, 999, 'missing-clock-fingerprint',
              'info', 'general', 'keyword', 1, 1,
              '{}'::jsonb, true, 100, 999
            );
            """
        )
        conn.commit()

        command.upgrade(config, "20260813_0259")

        repaired = conn.execute(
            """
            SELECT push_eligibility_updated_at_ms
              FROM news_items
             WHERE item_id = 'missing-clock'
            """
        ).fetchone()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert repaired == {"push_eligibility_updated_at_ms": 123}
        assert version == {"version_num": "20260813_0259"}

        with pytest.raises(CheckViolation):
            conn.execute(
                """
                UPDATE news_items
                   SET push_eligibility_updated_at_ms = NULL
                 WHERE item_id = 'missing-clock'
                """
            )
        conn.rollback()
    finally:
        conn.close()


def test_0259_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260813_0259_news_push_eligibility_clock_repair"
    )

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
