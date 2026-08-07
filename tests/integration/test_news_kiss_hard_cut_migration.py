from __future__ import annotations

from alembic import command
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config

_NEWS_TABLES = {
    "news_sources",
    "news_source_memberships",
    "news_items",
    "news_stories",
    "news_story_members",
    "news_projection_summary",
    "news_story_facet_counts",
    "news_source_facet_counts",
    "news_brief_selection_current",
    "news_brief_runs",
    "news_brief_publications",
    "news_brief_current",
}
_NEWS_TABLES_AFTER_OPENNEWS_HARD_CUT = (
    _NEWS_TABLES
    | {
        "news_push_state",
        "news_push_deliveries",
        "news_story_title_translations",
    }
) - {"news_source_memberships"}


def test_news_hard_cut_moves_bounded_opennews_metadata_to_current_item() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    command.upgrade(config, "20260731_0233")
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, feed_url, tier, lang, enabled,
              refresh_interval_seconds, next_fetch_at_ms, created_at_ms, updated_at_ms
            ) VALUES
              ('news-opennews', 'OpenNews', 'https://example.com/news', 2, 'en', true, 120, 0, 0, 0),
              ('rss-source', 'RSS Source', 'https://example.com/rss', 2, 'en', true, 120, 0, 0, 0)
            """
        )
        for fetch_id, started_at_ms in (("fetch-a", 1), ("fetch-b", 2)):
            conn.execute(
                """
                INSERT INTO news_source_fetches(
                  fetch_id, source_id, started_at_ms, finished_at_ms, status,
                  fetch_path, entries_seen, observations_inserted, items_inserted,
                  items_updated, rejection_counts, created_at_ms
                ) VALUES (%s, 'news-opennews', %s, %s, 'success', 'direct', 1, 1, 1, 0, '{}', %s)
                """,
                (fetch_id, started_at_ms, started_at_ms, started_at_ms),
            )
            conn.execute(
                """
                INSERT INTO news_feed_observations(
                  observation_id, fetch_id, source_id, source_item_key,
                  observed_at_ms, title, url, published_at_ms, raw,
                  admitted, rejection_reason, created_at_ms
                ) VALUES (%s, %s, 'news-opennews', 'same-key', %s, 'same title',
                          'https://example.com/a', 1, %s, true, NULL, %s)
                """,
                (
                    f"obs-{fetch_id}",
                    fetch_id,
                    started_at_ms,
                    Jsonb(
                        {
                            "id": "provider-42",
                            "coins": [
                                {
                                    "symbol": "BTC",
                                    "market_type": "spot",
                                    "match": "Bitcoin",
                                    "private": "drop-me",
                                }
                            ],
                            "private": "drop-me",
                        }
                        if fetch_id == "fetch-a"
                        else {
                            "id": "provider-42",
                            "source": "jin10",
                            "aiRating": {"score": 80, "signal": "long", "grade": "A"},
                            "private": "drop-me",
                        }
                    ),
                    started_at_ms,
                ),
            )
        conn.execute(
            """
            INSERT INTO news_brief_publications(
              publication_id, fingerprint, evidence_cutoff_at_ms,
              published_at_ms, provider, model, prompt_version,
              workflow_version, schema_version, locale, selected_story_ids,
              lead, lines, sources, validation, raw_response, created_at_ms
            ) VALUES (
              'publication-before-cut', 'brief-before-cut', 1, 2,
              'provider', 'model', 'prompt', 'workflow', 'schema', 'zh-CN',
              '["old-story"]'::jsonb, 'sealed lead', '["sealed line"]'::jsonb,
              '[{"story_id":"old-story","url":null}]'::jsonb,
              '{"citation_closure":true}'::jsonb, 'sealed raw', 2
            );
            UPDATE news_brief_current
               SET publication_id='publication-before-cut',
                   target_fingerprint='brief-before-cut', updated_at_ms=2
             WHERE singleton_key=true
            """
        )
        conn.execute(
            """
            INSERT INTO news_items(
              item_id, source_id, source_item_key, canonical_url,
              reporting_origin, title, normalized_title, description, lang,
              published_at_ms, first_observed_at_ms, last_observed_at_ms,
              content_fingerprint, level, category, classification_source,
              classification_confidence, importance_score, importance_factors,
              brief_excluded, active, created_at_ms, updated_at_ms
            ) VALUES (
              'item-before-cut', 'news-opennews', 'item-before-cut',
              'https://example.com/story', 'source', 'story before cut',
              'story before cut', '', 'en', 1, 1, 1, 'item-fingerprint',
              'info', 'general', 'keyword', 1, 1, '{}'::jsonb,
              false, true, 1, 1
            );
            INSERT INTO news_stories(
              story_id, canonical_key, canonical_title,
              representative_item_id, representative_source_id,
              representative_title, representative_url,
              representative_description, scoring_item_id, level, category,
              importance_score, importance_factors, item_count, source_count,
              first_published_at_ms, last_published_at_ms, active,
              state_fingerprint, created_at_ms, updated_at_ms
            ) VALUES (
              'story-before-cut', 'canonical-before-cut', 'story before cut',
              'item-before-cut', 'news-opennews', 'story before cut',
              'https://example.com/story', '', 'item-before-cut', 'info',
              'general', 1, '{}'::jsonb, 1, 1, 1, 1, true,
              'story-fingerprint', 1, 1
            );
            INSERT INTO news_story_aliases(alias_key, story_id, expires_at_ms, created_at_ms)
            VALUES ('alias-before-cut', 'story-before-cut', 10, 1)
            """
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(config, "20260801_0234")
    conn = connect_postgres_test(read_only=False)
    try:
        observation_id = conn.execute(
            """
            SELECT observation_id
              FROM news_feed_observations
             WHERE source_id='news-opennews'
             ORDER BY observed_at_ms
             LIMIT 1
            """
        ).fetchone()["observation_id"]
        conn.execute("UPDATE news_sources SET source_kind='opennews' WHERE source_id='news-opennews'")
        conn.execute(
            "UPDATE news_items SET winning_observation_id=%s WHERE item_id='item-before-cut'",
            (observation_id,),
        )
        conn.commit()
    finally:
        conn.close()
    command.upgrade(config, "20260801_0235")
    conn = connect_postgres_test(read_only=False)
    try:
        tables = {
            row["tablename"]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'news_%'"
            ).fetchall()
        }
        assert tables == _NEWS_TABLES
        removed = conn.execute(
            """
            SELECT to_regclass('news_source_fetches') AS fetches,
                   to_regclass('news_feed_observations') AS observations
            """
        ).fetchone()
        assert removed == {"fetches": None, "observations": None}
        item = conn.execute(
            """
            SELECT item_id, provider_record_id, provider_metadata
              FROM news_items
             WHERE item_id='item-before-cut'
            """
        ).fetchone()
        assert item["provider_record_id"] == "provider-42"
        assert item["provider_metadata"] == {
            "score": 80,
            "source": "jin10",
            "signal": "long",
            "grade": "A",
            "coins": [{"symbol": "BTC", "market_type": "spot", "match": "Bitcoin"}],
        }
        source_states = conn.execute("SELECT source_id, enabled FROM news_sources ORDER BY source_id").fetchall()
        assert source_states == [
            {"source_id": "news-opennews", "enabled": True},
            {"source_id": "rss-source", "enabled": False},
        ]
        columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = 'news_story_members'
                """
            ).fetchall()
        }
        assert {"current", "first_joined_at_ms", "last_confirmed_at_ms"}.isdisjoint(columns)
        item_url = conn.execute(
            """
            SELECT is_nullable FROM information_schema.columns
             WHERE table_name='news_items' AND column_name='canonical_url'
            """
        ).fetchone()
        story_url = conn.execute(
            """
            SELECT is_nullable FROM information_schema.columns
             WHERE table_name='news_stories' AND column_name='representative_url'
            """
        ).fetchone()
        assert item_url["is_nullable"] == "YES"
        assert story_url["is_nullable"] == "YES"
        publication = conn.execute(
            """
            SELECT publication_id, selected_story_ids, sources
              FROM news_brief_publications
             WHERE publication_id='publication-before-cut'
            """
        ).fetchone()
        assert publication["selected_story_ids"] == ["old-story"]
        assert publication["sources"] == [{"story_id": "old-story", "url": None}]
        assert (
            conn.execute("SELECT publication_id FROM news_brief_current WHERE singleton_key=true").fetchone()[
                "publication_id"
            ]
            == "publication-before-cut"
        )
        conn.execute(
            """
            UPDATE news_sources
               SET gap_unclosed=true, last_live_at_ms=123
             WHERE source_id='news-opennews'
            """
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(config, "20260806_0243")
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            UPDATE news_projection_summary
               SET last_material_change_at_ms=321,
                   last_attempt_at_ms=456,
                   last_error='news_story_operation_timeout',
                   updated_at_ms=456
             WHERE singleton_key='current'
            """
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(config, "head")
    conn = connect_postgres_test(read_only=False)
    try:
        tables = {
            row["tablename"]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'news_%'"
            ).fetchall()
        }
        assert tables == _NEWS_TABLES_AFTER_OPENNEWS_HARD_CUT
        source_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='news_sources'
                """
            ).fetchall()
        }
        assert {
            "feed_url",
            "refresh_interval_seconds",
            "etag",
            "last_modified",
            "next_fetch_at_ms",
            "claim_token",
            "claim_lease_expires_at_ms",
        }.isdisjoint(source_columns)
        assert {
            "last_fetch_started_at_ms",
            "last_fetch_finished_at_ms",
            "last_success_at_ms",
            "last_http_status",
            "consecutive_failures",
            "last_error",
            "live_connected",
            "last_live_at_ms",
            "last_recovery_at_ms",
            "gap_unclosed",
            "gap_boundary_provider_record_id",
            "gap_version",
        } <= source_columns
        retired_due_indexes = {
            row["indexname"]
            for row in conn.execute(
                """
                SELECT indexname
                  FROM pg_indexes
                 WHERE schemaname='public'
                   AND indexname IN ('ix_news_sources_due', 'ix_news_sources_due_claim')
                """
            ).fetchall()
        }
        assert retired_due_indexes == set()
        assert conn.execute("SELECT count(*) AS count FROM news_sources").fetchone()["count"] == 2
        assert conn.execute("SELECT count(*) AS count FROM news_items").fetchone()["count"] == 1
        source = conn.execute(
            """
            SELECT gap_unclosed, gap_boundary_provider_record_id, gap_version
              FROM news_sources
             WHERE source_id='news-opennews'
            """
        ).fetchone()
        assert source == {
            "gap_unclosed": True,
            "gap_boundary_provider_record_id": "provider-42",
            "gap_version": 1,
        }
        summary = conn.execute(
            """
            SELECT last_attempt_at_ms, last_material_change_at_ms,
                   last_success_at_ms, last_error
              FROM news_projection_summary
             WHERE singleton_key='current'
            """
        ).fetchone()
        assert summary == {
            "last_attempt_at_ms": 456,
            "last_material_change_at_ms": 321,
            "last_success_at_ms": 321,
            "last_error": "news_story_operation_timeout",
        }
    finally:
        conn.close()
