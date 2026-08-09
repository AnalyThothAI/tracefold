from __future__ import annotations

import importlib

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_audit import NEWS_TABLES
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0247_hard_cuts_news_to_public_current_state() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260807_0246")

        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, enabled, consecutive_failures,
              created_at_ms, updated_at_ms, source_kind, live_connected,
              gap_unclosed, gap_version
            ) VALUES (
              'news-opennews', 'OpenNews', 2, 'en', true, 0,
              0, 0, 'opennews', false, false, 0
            );

            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, provider_score_updated_at_ms,
              canonical_url, reporting_origin, title, description,
              lang, published_at_ms, first_observed_at_ms,
              last_observed_at_ms, content_fingerprint, level, category,
              classification_source, classification_confidence,
              importance_score, importance_factors, active,
              created_at_ms, updated_at_ms
            ) VALUES (
              'current-item', 'news-opennews', 'wire-1', 'wire-1',
              '{"source":"Reuters","score":88}'::jsonb, 2,
              'https://example.com/story', 'reuters',
              'Retained OpenNews material fact', 'Retained description',
              'en', 1, 1, 2,
              'retained-content-fingerprint', 'info', 'general', 'keyword', 1,
              0, '{}'::jsonb, true, 1, 2
            );

            INSERT INTO news_brief_selection_current(
              singleton_key, selection_fingerprint, projection_revision,
              selector_evaluated_at_ms, top_stories, selection_stats,
              selector_version, identity_version, updated_at_ms
            ) VALUES (
              true, repeat('a', 64), 'legacy-revision', 1,
              '[]'::jsonb, '{}'::jsonb, 'legacy-selector',
              'legacy-identity', 1
            );

            INSERT INTO news_brief_runs(
              run_id, target_fingerprint, selection_fingerprint, status,
              model_outcome, pointer_action, failure_count,
              created_at_ms, updated_at_ms
            ) VALUES (
              'legacy-run', repeat('b', 64), repeat('a', 64),
              'waiting_input', 'none', 'none', 0, 1, 1
            );

            UPDATE news_push_state
               SET baseline_at_ms = 42, updated_at_ms = 42
             WHERE singleton_key = 'current';

            INSERT INTO news_push_deliveries(
              story_id, selected_item_id, provider_score,
              threshold_observed_at_ms, source_payload,
              delivery_payload, payload_fingerprint, translation_status,
              status, delivery_attempts, next_attempt_at_ms,
              created_at_ms, updated_at_ms
            ) VALUES (
              repeat('c', 64), 'current-item', 88, 1,
              '{"schema_version":"news_story_push_v1"}'::jsonb,
              NULL, NULL, 'not_requested', 'pending_translation', 0, 1,
              1, 1
            ), (
              repeat('d', 64), 'legacy-item', 89, 1,
              '{"title":"legacy payload"}'::jsonb,
              NULL, NULL, 'not_requested', 'pending_translation', 0, 1,
              1, 1
            );
            """
        )
        conn.commit()

        command.upgrade(config, "20260809_0247")

        news_tables = {
            row["table_name"]
            for row in conn.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema = 'public'
                   AND table_type = 'BASE TABLE'
                   AND left(table_name, 5) = 'news_'
                """
            ).fetchall()
        }
        source_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'news_sources'
                """
            ).fetchall()
        }
        summary_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'news_projection_summary'
                """
            ).fetchall()
        }
        item_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'news_items'
                """
            ).fetchall()
        }
        item_nullability = {
            row["column_name"]: row["is_nullable"]
            for row in conn.execute(
                """
                SELECT column_name, is_nullable
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'news_items'
                   AND column_name IN (
                     'level', 'category', 'classification_source',
                     'classification_confidence'
                   )
                """
            ).fetchall()
        }
        source_indexes = {
            row["indexname"]
            for row in conn.execute(
                """
                SELECT indexname
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND tablename = 'news_sources'
                """
            ).fetchall()
        }
        current = dict(conn.execute("SELECT * FROM news_brief_current").fetchone())
        retained_item = conn.execute(
            """
            SELECT item_id, source_id, provider_record_id, provider_metadata,
                   content_fingerprint, active
              FROM news_items
             WHERE item_id = 'current-item'
            """
        ).fetchone()
        push_state = conn.execute(
            "SELECT baseline_at_ms FROM news_push_state WHERE singleton_key = 'current'"
        ).fetchone()
        push_rows = conn.execute("SELECT story_id FROM news_push_deliveries ORDER BY story_id").fetchall()

        assert news_tables == set(NEWS_TABLES)
        assert {
            "feed_url",
            "refresh_interval_seconds",
            "etag",
            "last_modified",
            "next_fetch_at_ms",
            "claim_token",
            "claim_lease_expires_at_ms",
            "last_outcome",
            "last_rejection_counts",
            "last_items_seen",
            "last_items_accepted",
        } <= source_columns
        assert {
            "gap_unclosed",
            "gap_boundary_provider_record_id",
            "gap_version",
        }.isdisjoint(source_columns)
        assert "unmaterialized_item_count" not in summary_columns
        assert "source_position" in item_columns
        assert item_nullability == {
            "level": "YES",
            "category": "YES",
            "classification_source": "YES",
            "classification_confidence": "YES",
        }
        assert "ix_news_sources_due_claim" in source_indexes
        assert current == {
            "singleton_key": True,
            "slot_at_ms": None,
            "slot_status": "due",
            "next_due_at_ms": 0,
            "completed_at_ms": None,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_ms": None,
            "attempt_count": 0,
            "failure_count": 0,
            "model_outcome": None,
            "pointer_action": "none",
            "last_error_code": None,
            "last_attempt_at_ms": None,
            "active_selection": None,
            "served_payload": None,
            "created_at_ms": 0,
            "updated_at_ms": 0,
        }
        assert conn.execute("SELECT count(*) AS count FROM news_brief_selection_current").fetchone()["count"] == 0
        assert push_state == {"baseline_at_ms": 42}
        assert push_rows == [{"story_id": "c" * 64}]
        assert retained_item == {
            "item_id": "current-item",
            "source_id": "news-opennews",
            "provider_record_id": "wire-1",
            "provider_metadata": {"source": "Reuters", "score": 88},
            "content_fingerprint": "retained-content-fingerprint",
            "active": True,
        }

        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, enabled,
              consecutive_failures, created_at_ms, updated_at_ms,
              source_kind, live_connected, feed_url,
              refresh_interval_seconds, next_fetch_at_ms
            ) VALUES (
              'rss-test', 'RSS Test', 2, 'en', true,
              0, 2, 2, 'rss', false, 'https://example.com/feed.xml',
              300, 2
            )
            """
        )
        conn.commit()

        with pytest.raises(CheckViolation):
            conn.execute("UPDATE news_brief_current SET slot_at_ms = 1 WHERE singleton_key")
        conn.rollback()
    finally:
        conn.close()


def test_0247_downgrade_is_deliberately_unsupported() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260809_0247_news_public_worldmonitor_current"
    )

    with pytest.raises(RuntimeError, match="irreversible public News hard cut"):
        migration.downgrade()
