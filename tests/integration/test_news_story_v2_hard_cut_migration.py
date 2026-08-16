from __future__ import annotations

import importlib

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.news.repository import NewsRepository
from tracefold.news.story_projection import NewsStoryFactSnapshot, build_story_projection
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0272_hard_cuts_story_identity_shape_and_resets_identity_bearing_state() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260815_0271")

        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, source_kind, enabled,
              created_at_ms, updated_at_ms
            ) VALUES ('news-opennews', 'OpenNews', 4, 'en', 'opennews', true, 1, 1);
            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, first_ingest_mode, reporting_origin,
              title, description, lang, published_at_ms,
              first_observed_at_ms, last_observed_at_ms,
              content_fingerprint, importance_score, importance_factors,
              active, created_at_ms, updated_at_ms
            ) VALUES (
              'news_item_11111111111111111111111111111111',
              'news-opennews', 'item-preserved',
              'provider-preserved',
              '{"strategies":[{"id":"1018","name":"News Score > 70"}]}'::jsonb,
              'live', 'OpenNews',
              'Major earthquake strikes preserved coastal region', '', 'en',
              1, 1, 1, repeat('f', 64),
              0, '{}'::jsonb, true, 1, 1
            );
            INSERT INTO news_stories(
              story_id, canonical_key, canonical_title,
              representative_item_id, representative_source_id,
              representative_title, representative_description,
              scoring_item_id, level, category, importance_score,
              importance_factors, facet_facts, item_count, source_count,
              first_published_at_ms, last_published_at_ms,
              state_fingerprint, created_at_ms, updated_at_ms
            ) VALUES (
              repeat('2', 64), 'old-key',
              'Major earthquake strikes preserved coastal region',
              'news_item_11111111111111111111111111111111',
              'news-opennews',
              'Major earthquake strikes preserved coastal region', '',
              'news_item_11111111111111111111111111111111',
              'info', 'general', 0, '{}'::jsonb,
              '{"source_ids":["news-opennews"],"reporting_origins":["OpenNews"]}'::jsonb,
              1, 1, 1, 1, repeat('e', 64), 1, 1
            );
            INSERT INTO news_story_members(story_id, item_id)
            VALUES (
              repeat('2', 64), 'news_item_11111111111111111111111111111111'
            );
            INSERT INTO news_item_title_presentations(
              item_id, source_title_fingerprint, original_title, state,
              created_at_ms, updated_at_ms
            ) VALUES (
              'news_item_11111111111111111111111111111111',
              encode(
                sha256(convert_to(
                  'Major earthquake strikes preserved coastal region', 'UTF8'
                )),
                'hex'
              ),
              'Major earthquake strikes preserved coastal region',
              'pending', 1, 1
            );
            INSERT INTO news_push_deliveries(
              item_id, live_observed_at_ms, source_payload,
              status, attempted_at_ms, receipt, sent_at_ms,
              created_at_ms, updated_at_ms, source_title_fingerprint
            ) VALUES (
              'news_item_11111111111111111111111111111111', 1,
              '{"schema_version":"news_item_push_v1"}'::jsonb,
                  'sent', 1, '{"provider":"feishu"}'::jsonb, 1, 1, 1,
              NULL
            );
            UPDATE news_projection_summary
               SET input_fingerprint=repeat('a', 64),
                   projection_version='old-story-version',
                   active_story_count=7
             WHERE singleton_key='current';
            INSERT INTO news_brief_selection_current(
              singleton_key, selection_fingerprint, projection_revision,
              selector_evaluated_at_ms, top_stories, selection_stats,
              selector_version, identity_version, updated_at_ms
            ) VALUES (
              true, repeat('b', 64), 'old-revision', 0, '[]'::jsonb,
              '{}'::jsonb, 'old-selector', 'old-identity', 0
            );
            """
        )
        conn.commit()

        command.upgrade(config, "20260815_0272")

        columns = {
            row["column_name"]: row
            for row in conn.execute(
                """
                SELECT column_name, is_nullable
                  FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='news_stories'
                """
            ).fetchall()
        }
        summary = conn.execute(
            "SELECT input_fingerprint, projection_version, active_story_count FROM news_projection_summary"
        ).fetchone()
        brief = conn.execute(
            "SELECT slot_at_ms, slot_status, active_selection, served_payload FROM news_brief_current"
        ).fetchone()

        assert "canonical_key" not in columns
        assert columns["identity_evidence"]["is_nullable"] == "NO"
        assert conn.execute("SELECT count(*) AS count FROM news_brief_selection_current").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM news_stories").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM news_story_members").fetchone()["count"] == 0
        assert conn.execute("SELECT count(*) AS count FROM news_items").fetchone()["count"] == 1
        assert conn.execute("SELECT count(*) AS count FROM news_item_title_presentations").fetchone()["count"] == 1
        assert conn.execute("SELECT count(*) AS count FROM news_push_deliveries").fetchone()["count"] == 1
        assert summary == {"input_fingerprint": None, "projection_version": None, "active_story_count": 0}
        assert brief == {
            "slot_at_ms": None,
            "slot_status": "due",
            "active_selection": None,
            "served_payload": None,
        }

        conn.commit()
        repository = NewsRepository(conn)
        loaded = repository.load_story_projection(now_ms=2)
        snapshot = NewsStoryFactSnapshot(
            material_snapshot_fingerprint=str(loaded["material_snapshot_fingerprint"]),
            evaluation_time_ms=int(loaded["evaluation_time_ms"]),
            published_material_snapshot_fingerprint=None,
            rows=tuple(dict(row) for row in loaded["rows"]),
        )
        projection = build_story_projection(snapshot)
        with conn.transaction():
            publication = repository.publish_story_projection(
                snapshot=snapshot,
                projection=projection.as_payload(),
                now_ms=2,
            )
        assert publication["projection_status"] == "rebuilt"
        assert conn.execute("SELECT count(*) AS count FROM news_stories").fetchone()["count"] == 1
        selection = conn.execute("SELECT identity_version, top_stories FROM news_brief_selection_current").fetchone()
        assert selection["identity_version"] == "news_story_identity_v3"
        assert len(selection["top_stories"]) == 1
        conn.commit()
        with conn.transaction():
            candidate = repository.peek_brief_candidate(now_ms=2)
        assert candidate == {"slot_at_ms": 0, "next_due_at_ms": 0}

        with pytest.raises(CheckViolation):
            conn.execute(
                """
                INSERT INTO news_stories(
                  story_id, canonical_title, representative_item_id,
                  representative_source_id, representative_title,
                  representative_description, scoring_item_id, level, category,
                  importance_score, importance_factors, facet_facts,
                  identity_evidence, item_count, source_count,
                  first_published_at_ms, last_published_at_ms,
                  state_fingerprint, created_at_ms, updated_at_ms
                ) VALUES (
                  'story-invalid', 'title', 'missing-item', 'missing-source',
                  'title', '', 'missing-item', 'info', 'general', 0,
                  '{}'::jsonb,
                  '{"source_ids":[],"reporting_origins":[]}'::jsonb,
                  '[]'::jsonb, 1, 1, 0, 0, repeat('c', 64), 0, 0
                )
                """
            )
    finally:
        conn.close()


def test_0272_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260815_0272_news_story_v2_hard_cut"
    )

    with pytest.raises(RuntimeError, match="irreversible"):
        migration.downgrade()
