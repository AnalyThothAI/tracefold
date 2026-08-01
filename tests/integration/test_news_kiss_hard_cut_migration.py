from __future__ import annotations

from alembic import command
from psycopg.types.json import Jsonb

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config

_NEWS_TABLES = {
    "news_sources",
    "news_source_memberships",
    "news_source_fetches",
    "news_feed_observations",
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


def test_news_hard_cut_consolidates_observations_and_leaves_exact_storage_boundary() -> None:
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
            ) VALUES ('source', 'Source', 'https://example.com/rss', 2, 'en', true, 120, 0, 0, 0)
            """
        )
        for fetch_id, started_at_ms in (("fetch-a", 1), ("fetch-b", 2)):
            conn.execute(
                """
                INSERT INTO news_source_fetches(
                  fetch_id, source_id, started_at_ms, finished_at_ms, status,
                  fetch_path, entries_seen, observations_inserted, items_inserted,
                  items_updated, rejection_counts, created_at_ms
                ) VALUES (%s, 'source', %s, %s, 'success', 'direct', 1, 1, 1, 0, '{}', %s)
                """,
                (fetch_id, started_at_ms, started_at_ms, started_at_ms),
            )
            conn.execute(
                """
                INSERT INTO news_feed_observations(
                  observation_id, fetch_id, source_id, source_item_key,
                  observed_at_ms, title, url, published_at_ms, raw,
                  admitted, rejection_reason, created_at_ms
                ) VALUES (%s, %s, 'source', 'same-key', %s, 'same title',
                          'https://example.com/a', 1, %s, true, NULL, %s)
                """,
                (f"obs-{fetch_id}", fetch_id, started_at_ms, Jsonb({"same": True}), started_at_ms),
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
        assert tables == _NEWS_TABLES
        rows = conn.execute(
            """
            SELECT observation_id, fetch_id, observation_kind, payload_fingerprint
              FROM news_feed_observations
            """
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["fetch_id"] == "fetch-a"
        assert rows[0]["observation_kind"] == "report"
        assert len(rows[0]["payload_fingerprint"]) == 64
        assert rows[0]["observation_id"].startswith("news_observation_")
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
    finally:
        conn.close()
