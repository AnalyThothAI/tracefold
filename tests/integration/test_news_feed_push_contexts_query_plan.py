from __future__ import annotations

from typing import Any

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news.query_specs import story_push_contexts_query
from tracefold.platform.postgres.postgres_audit import (
    PostgresQueryAudit,
    QueryAuditCatalog,
)

_ITEM_COUNT = 40_000
_STORY_COUNT = 16_000
_REQUESTED_STORY_IDS = tuple(f"story-{story_no:05d}" for story_no in range(_STORY_COUNT - 99, _STORY_COUNT + 1))


def test_news_feed_push_contexts_stays_bounded_to_requested_story_members(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        _insert_production_shaped_news(conn)
        conn.commit()
        for table_name in (
            "news_items",
            "news_story_members",
            "news_stories",
            "news_push_deliveries",
        ):
            _vacuum_analyze(conn, table_name)

        query = story_push_contexts_query(story_ids=_REQUESTED_STORY_IDS)
        catalog = QueryAuditCatalog(
            queries=(query,),
            query_routes={"/api/news/feed": (query.name,)},
            no_sql_routes=frozenset(),
        )
        payload = PostgresQueryAudit(conn, catalog=catalog).run(analyze=True)
    finally:
        conn.close()

    result = payload["queries"][0]
    metrics = result["metrics"]
    diagnostic = {
        "audit_ok": payload["ok"],
        "query_ok": result["ok"],
        "violations": result["violations"],
        "metrics": metrics,
    }
    assert payload["ok"] is True, diagnostic
    assert result["name"] == "news_feed_push_contexts", diagnostic
    assert metrics["plan_json_valid"] is True, diagnostic
    assert not [
        scan
        for scan in metrics["large_seq_scans"]
        if scan["relation"] in {"news_items", "news_story_members", "news_stories"}
    ], diagnostic
    assert metrics["temp_read_blocks"] == 0, diagnostic
    assert metrics["temp_written_blocks"] == 0, diagnostic
    assert metrics["read_return_amplification"] <= 20, diagnostic


def _insert_production_shaped_news(conn: Any) -> None:
    conn.execute(
        """
        INSERT INTO news_sources(
          source_id, name, tier, lang, enabled, consecutive_failures,
          created_at_ms, updated_at_ms, source_kind, live_connected
        ) VALUES (
          'feed-plan-source', 'Feed Plan Source', 1, 'en', true, 0,
          0, 0, 'opennews', false
        )
        """
    )
    conn.execute(
        """
        INSERT INTO news_items(
          item_id, source_id, source_item_key, provider_record_id,
          provider_metadata, reporting_origin, title, description, lang,
          published_at_ms, first_observed_at_ms, last_observed_at_ms,
          content_fingerprint, level, category, classification_source,
          classification_confidence, importance_score, importance_factors,
          active, created_at_ms, updated_at_ms,
          provider_score_updated_at_ms, push_eligibility_updated_at_ms
        )
        SELECT 'item-' || lpad(series_no::text, 5, '0'),
               'feed-plan-source',
               'item-' || lpad(series_no::text, 5, '0'),
               'record-' || lpad(series_no::text, 5, '0'),
               jsonb_build_object(
                 'score', 70 + (series_no %% 30),
                 'coins', jsonb_build_array(
                   jsonb_build_object('symbol', 'ASSET' || (series_no %% 200))
                 )
               ),
               'Feed Plan Wire',
               'Feed push-context plan item ' || series_no,
               '',
               'en',
               series_no,
               series_no,
               series_no,
               'item-fingerprint-' || lpad(series_no::text, 5, '0'),
               'info',
               'general',
               'keyword',
               1,
               90,
               '{}'::jsonb,
               true,
               series_no,
               series_no,
               series_no,
               series_no
          FROM generate_series(1, %s::integer) series_no
        """,
        (_ITEM_COUNT,),
    )
    conn.execute(
        """
        INSERT INTO news_stories(
          story_id, canonical_key, canonical_title,
          representative_item_id, representative_source_id,
          representative_title, representative_description,
          scoring_item_id, level, category, importance_score,
          importance_factors, item_count, source_count,
          first_published_at_ms, last_published_at_ms,
          state_fingerprint, created_at_ms, updated_at_ms, facet_facts
        )
        SELECT 'story-' || lpad(series_no::text, 5, '0'),
               'story-key-' || lpad(series_no::text, 5, '0'),
               'Feed push-context plan story ' || series_no,
               'item-' || lpad(series_no::text, 5, '0'),
               'feed-plan-source',
               'Feed push-context plan story ' || series_no,
               '',
               'item-' || lpad(series_no::text, 5, '0'),
               'info',
               'general',
               90,
               '{}'::jsonb,
               3,
               1,
               series_no,
               series_no,
               'story-fingerprint-' || lpad(series_no::text, 5, '0'),
               series_no,
               series_no,
               '{"source_ids":["feed-plan-source"],"reporting_origins":["Feed Plan Wire"]}'::jsonb
          FROM generate_series(1, %s::integer) series_no
        """,
        (_STORY_COUNT,),
    )
    conn.execute(
        """
        INSERT INTO news_story_members(story_id, item_id)
        SELECT 'story-' || lpad((((series_no - 1) %% %s) + 1)::text, 5, '0'),
               'item-' || lpad(series_no::text, 5, '0')
          FROM generate_series(1, %s::integer) series_no
        """,
        (_STORY_COUNT, _ITEM_COUNT),
    )


def _vacuum_analyze(conn: Any, table_name: str) -> None:
    conn.commit()
    raw_conn = conn._conn
    raw_conn.autocommit = True
    try:
        conn.execute(f"VACUUM (ANALYZE) {table_name}")
    finally:
        raw_conn.autocommit = False
