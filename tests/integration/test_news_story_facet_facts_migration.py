from __future__ import annotations

import importlib

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0256_backfills_deterministic_story_facet_facts_and_forces_rebuild() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260812_0255")

        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, enabled, consecutive_failures,
              created_at_ms, updated_at_ms, source_kind, live_connected
            ) VALUES
              ('source-alpha', 'Source Alpha', 2, 'en', true, 0,
               0, 0, 'opennews', false),
              ('source-beta', 'Source Beta', 2, 'en', true, 0,
               0, 0, 'opennews', false);

            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, canonical_url, reporting_origin, title,
              description, lang, published_at_ms, first_observed_at_ms,
              last_observed_at_ms, content_fingerprint, level, category,
              classification_source, classification_confidence,
              importance_score, importance_factors, active,
              created_at_ms, updated_at_ms
            ) VALUES
              ('item-z', 'source-beta', 'wire-z', 'wire-z', '{}'::jsonb,
               'https://example.com/z', 'Bloomberg', 'Headline Z', '', 'en',
               1, 1, 1, 'fingerprint-z', 'info', 'general', 'keyword', 1,
               1, '{}'::jsonb, true, 1, 1),
              ('item-a', 'source-alpha', 'wire-a', 'wire-a', '{}'::jsonb,
               'https://example.com/a', ' Reuters ', 'Headline A', '', 'en',
               1, 1, 1, 'fingerprint-a', 'info', 'general', 'keyword', 1,
               1, '{}'::jsonb, true, 1, 1),
              ('item-b', 'source-alpha', 'wire-b', 'wire-b', '{}'::jsonb,
               'https://example.com/b', 'Reuters', 'Headline B', '', 'en',
               1, 1, 1, 'fingerprint-b', 'info', 'general', 'keyword', 1,
               1, '{}'::jsonb, true, 1, 1);

            INSERT INTO news_stories(
              story_id, canonical_key, canonical_title,
              representative_item_id, representative_source_id,
              representative_title, representative_url,
              representative_description, scoring_item_id, level, category,
              importance_score, importance_factors, item_count, source_count,
              first_published_at_ms, last_published_at_ms,
              state_fingerprint, created_at_ms, updated_at_ms
            ) VALUES (
              'story-one', 'story-one-key', 'Story one',
              'item-a', 'source-alpha', 'Story one',
              'https://example.com/a', '', 'item-a', 'info', 'general',
              1, '{}'::jsonb, 3, 2, 1, 1, 'story-one-state', 1, 1
            );

            INSERT INTO news_story_members(story_id, item_id) VALUES
              ('story-one', 'item-z'),
              ('story-one', 'item-a'),
              ('story-one', 'item-b');

            UPDATE news_projection_summary
               SET input_fingerprint = repeat('f', 64)
             WHERE singleton_key = 'current';
            """
        )
        conn.commit()

        command.upgrade(config, "20260813_0256")

        row = conn.execute(
            """
            SELECT facet_facts
              FROM news_stories
             WHERE story_id = 'story-one'
            """
        ).fetchone()
        column = conn.execute(
            """
            SELECT is_nullable, column_default
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'news_stories'
               AND column_name = 'facet_facts'
            """
        ).fetchone()
        fingerprint = conn.execute(
            """
            SELECT input_fingerprint
              FROM news_projection_summary
             WHERE singleton_key = 'current'
            """
        ).fetchone()

        assert row == {
            "facet_facts": {
                "source_ids": ["source-alpha", "source-beta"],
                "reporting_origins": ["Bloomberg", "Reuters"],
            }
        }
        assert column == {"is_nullable": "NO", "column_default": None}
        assert fingerprint == {"input_fingerprint": None}

        with pytest.raises(CheckViolation):
            conn.execute("UPDATE news_stories SET facet_facts = '[]'::jsonb WHERE story_id = 'story-one'")
        conn.rollback()
        with pytest.raises(CheckViolation):
            conn.execute(
                """
                UPDATE news_stories
                   SET facet_facts = '{"source_ids":[]}'::jsonb
                 WHERE story_id = 'story-one'
                """
            )
    finally:
        conn.close()


def test_0256_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260813_0256_news_story_facet_facts"
    )

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
