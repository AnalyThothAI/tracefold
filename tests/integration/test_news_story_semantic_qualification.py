from __future__ import annotations

from typing import Any

from psycopg import sql

from scripts.news_story_semantic_qualification import run_read_only_qualification
from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
    prepare_postgres_database,
)
from tracefold.news.repository import NewsRepository
from tracefold.news.story_projection import NewsStoryFactSnapshot, build_story_projection
from tracefold.platform.config.settings import Settings

NOW_MS = 2_000_000_000_000


def _news_state_fingerprints(conn: Any) -> dict[str, tuple[int, str]]:
    tables = conn.execute(
        """
        SELECT table_name
          FROM information_schema.tables
         WHERE table_schema = 'public'
           AND table_name LIKE 'news\\_%' ESCAPE '\\'
           AND table_type = 'BASE TABLE'
         ORDER BY table_name
        """
    ).fetchall()
    result: dict[str, tuple[int, str]] = {}
    for row in tables:
        table_name = str(row["table_name"])
        fingerprint = conn.execute(
            sql.SQL(
                """
                SELECT count(*) AS row_count,
                       md5(COALESCE(string_agg(row_json, '' ORDER BY row_json), '')) AS fingerprint
                  FROM (SELECT row_to_json(value)::text AS row_json FROM {} AS value) AS rows
                """
            ).format(sql.Identifier(table_name))
        ).fetchone()
        result[table_name] = (int(fingerprint["row_count"]), str(fingerprint["fingerprint"]))
    return result


def test_qualification_reads_real_postgres_without_mutating_news_state() -> None:
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, source_kind, enabled,
              created_at_ms, updated_at_ms
            ) VALUES ('news-opennews', 'OpenNews', 4, 'en', 'opennews', true, 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, first_ingest_mode, reporting_origin,
              title, description, lang, published_at_ms,
              first_observed_at_ms, last_observed_at_ms,
              content_fingerprint, importance_score, importance_factors,
              active, created_at_ms, updated_at_ms
            ) VALUES (
              'news_item_11111111111111111111111111111111',
              'news-opennews', 'qualification-item', 'provider-item',
              '{"strategies":[{"id":"1018","name":"News Score > 70"}]}'::jsonb,
              'live', 'OpenNews',
              'Central bank holds rates steady', '', 'en', %s,
              %s, %s, repeat('f', 64), 0, '{}'::jsonb,
              true, %s, %s
            )
            """,
            (NOW_MS, NOW_MS, NOW_MS, NOW_MS, NOW_MS),
        )
        conn.commit()
        before_summary = conn.execute(
            """
            SELECT input_fingerprint, projection_version, last_attempt_at_ms,
                   last_success_at_ms, last_error, updated_at_ms
              FROM news_projection_summary
             WHERE singleton_key='current'
            """
        ).fetchone()
        before_counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM news_items) AS items,
              (SELECT count(*) FROM news_stories) AS stories,
              (SELECT count(*) FROM news_story_members) AS memberships,
              (SELECT count(*) FROM news_brief_selection_current) AS selections,
              (SELECT count(*) FROM news_push_deliveries) AS pushes,
              (SELECT count(*) FROM news_item_title_presentations) AS presentations
            """
        ).fetchone()
        before_all_news_state = _news_state_fingerprints(conn)
    finally:
        conn.close()

    report = run_read_only_qualification(
        settings=Settings(storage=postgres_settings_storage()),
        now_ms=NOW_MS,
    )

    conn = connect_postgres_test(read_only=False)
    try:
        after_summary = conn.execute(
            """
            SELECT input_fingerprint, projection_version, last_attempt_at_ms,
                   last_success_at_ms, last_error, updated_at_ms
              FROM news_projection_summary
             WHERE singleton_key='current'
            """
        ).fetchone()
        after_counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM news_items) AS items,
              (SELECT count(*) FROM news_stories) AS stories,
              (SELECT count(*) FROM news_story_members) AS memberships,
              (SELECT count(*) FROM news_brief_selection_current) AS selections,
              (SELECT count(*) FROM news_push_deliveries) AS pushes,
              (SELECT count(*) FROM news_item_title_presentations) AS presentations
            """
        ).fetchone()
        after_all_news_state = _news_state_fingerprints(conn)
    finally:
        conn.close()

    assert report["mode"] == "read_only_zero_write"
    assert report["production_baseline"]["story_count"] == 1
    assert before_summary == after_summary
    assert before_counts == after_counts
    assert before_all_news_state == after_all_news_state
    assert "news_brief_current" in before_all_news_state
    assert "news_opennews_incidents" in before_all_news_state
    assert "news_projection_summary" in before_all_news_state


def test_repeated_title_outside_event_window_publishes_distinct_stories() -> None:
    """Drive the complete projection/publish seam for the stable-ID collision."""

    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, source_kind, enabled,
              created_at_ms, updated_at_ms
            ) VALUES ('news-opennews', 'OpenNews', 4, 'en', 'opennews', true, 1, 1)
            """
        )
        for index in (1, 2):
            conn.execute(
                """
                INSERT INTO news_items(
                  item_id, source_id, source_item_key, provider_record_id,
                  provider_metadata, first_ingest_mode, reporting_origin,
                  title, description, lang, published_at_ms,
                  first_observed_at_ms, last_observed_at_ms,
                  content_fingerprint, importance_score, importance_factors,
                  active, created_at_ms, updated_at_ms
                ) VALUES (
                  %s, 'news-opennews', %s, %s,
                  '{"strategies":[{"id":"1018","name":"News Score > 70"}]}'::jsonb,
                  'live', 'OpenNews',
                  'BTC OI Rise 3.4%% OI Value $21B Whale/OI Ratio 1.2', '', 'en', %s,
                  %s, %s, %s, 0, '{}'::jsonb,
                  true, %s, %s
                )
                """,
                (
                    f"news_item_{index:032d}",
                    f"collision-{index}",
                    f"provider-{index}",
                    NOW_MS - (7 - index * 3) * 60 * 60 * 1_000,
                    NOW_MS,
                    NOW_MS,
                    str(index).rjust(64, "0"),
                    NOW_MS,
                    NOW_MS,
                ),
            )
        conn.commit()
        repository = NewsRepository(conn)
        loaded = repository.load_story_projection(now_ms=NOW_MS)
        snapshot = NewsStoryFactSnapshot(
            material_snapshot_fingerprint=str(loaded["material_snapshot_fingerprint"]),
            evaluation_time_ms=int(loaded["evaluation_time_ms"]),
            published_material_snapshot_fingerprint=None,
            rows=tuple(dict(row) for row in loaded["rows"]),
        )
        projection = build_story_projection(snapshot)

        assert len(projection.stories) == 2
        assert len({story["story_id"] for story in projection.stories}) == 2
        with conn.transaction():
            publication = repository.publish_story_projection(
                snapshot=snapshot,
                projection=projection.as_payload(),
                now_ms=NOW_MS,
            )

        assert publication["projection_status"] == "rebuilt"
        assert conn.execute("SELECT count(*) AS count FROM news_stories").fetchone()["count"] == 2
        assert conn.execute("SELECT count(*) AS count FROM news_story_members").fetchone()["count"] == 2
        identity_versions = conn.execute(
            "SELECT DISTINCT identity_evidence->>'identity_version' AS version FROM news_stories"
        ).fetchall()
        assert identity_versions == [{"version": "news_story_identity_v3"}]
        selection = conn.execute("SELECT identity_version FROM news_brief_selection_current").fetchone()
        assert selection == {"identity_version": "news_story_identity_v3"}
        summary = conn.execute("SELECT projection_version FROM news_projection_summary").fetchone()
        assert "news_story_identity_v3" in summary["projection_version"]
    finally:
        conn.close()
