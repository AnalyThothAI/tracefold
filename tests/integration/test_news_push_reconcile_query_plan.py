from __future__ import annotations

from typing import Any

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news.query_specs import story_push_reconcile_page_query
from tracefold.platform.postgres.postgres_audit import (
    PostgresQueryAudit,
    QueryAuditCatalog,
)

_STORY_COUNT = 10_000
_COLD_STORY_COUNT = 9_000
_CURSORS = (None, "story-09000", "story-09800")


def test_story_push_reconcile_page_stays_bounded_across_cold_and_hot_story_ranges(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        _insert_source(conn)
        _insert_story_range(conn, start=1, stop=_COLD_STORY_COUNT)
        conn.commit()
        for table_name in ("news_items", "news_story_members", "news_stories"):
            _vacuum_analyze(conn, table_name)

        _insert_story_range(conn, start=_COLD_STORY_COUNT + 1, stop=_STORY_COUNT)
        conn.commit()
        for table_name in ("news_items", "news_story_members", "news_stories"):
            conn.execute(f"ANALYZE {table_name}")
        conn.commit()

        query = story_push_reconcile_page_query()
        catalog = QueryAuditCatalog(
            queries=(query,),
            query_routes={"/audit/news/push-reconcile": (query.name,)},
            no_sql_routes=frozenset(),
        )
        audits: list[tuple[str | None, dict[str, Any]]] = []
        for cursor in _CURSORS:
            conn.execute(
                """
                UPDATE news_push_state
                   SET reconcile_cursor_story_id = %s
                 WHERE singleton_key = 'current'
                """,
                (cursor,),
            )
            conn.commit()
            audits.append(
                (
                    cursor,
                    PostgresQueryAudit(conn, catalog=catalog).run(analyze=True),
                )
            )
    finally:
        conn.close()

    for cursor, payload in audits:
        result = payload["queries"][0]
        metrics = result["metrics"]
        diagnostic = {
            "cursor": cursor,
            "audit_ok": payload["ok"],
            "query_ok": result["ok"],
            "violations": result["violations"],
            "metrics": metrics,
        }
        assert payload["ok"] is True, diagnostic
        assert result["name"] == "news_push_reconcile_page", diagnostic
        assert metrics["plan_json_valid"] is True, diagnostic
        assert not [scan for scan in metrics["large_seq_scans"] if scan["relation"] == "news_stories"], diagnostic
        assert metrics["temp_read_blocks"] == 0, diagnostic
        assert metrics["temp_written_blocks"] == 0, diagnostic
        assert metrics["read_return_amplification"] <= 20, diagnostic


def _insert_source(conn: Any) -> None:
    conn.execute(
        """
        INSERT INTO news_sources(
          source_id, name, tier, lang, enabled, consecutive_failures,
          created_at_ms, updated_at_ms, source_kind, live_connected
        ) VALUES (
          'push-plan-source', 'Push Plan Source', 1, 'en', true, 0,
          0, 0, 'opennews', false
        )
        """
    )


def _insert_story_range(conn: Any, *, start: int, stop: int) -> None:
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
               'push-plan-source',
               'item-' || lpad(series_no::text, 5, '0'),
               'record-' || lpad(series_no::text, 5, '0'),
               jsonb_build_object('score', 90),
               'Push Plan Wire',
               'Push reconcile plan story ' || series_no,
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
          FROM generate_series(%s::integer, %s::integer) series_no
        """,
        (start, stop),
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
               'Push reconcile plan story ' || series_no,
               'item-' || lpad(series_no::text, 5, '0'),
               'push-plan-source',
               'Push reconcile plan story ' || series_no,
               '',
               'item-' || lpad(series_no::text, 5, '0'),
               'info',
               'general',
               90,
               '{}'::jsonb,
               1,
               1,
               series_no,
               series_no,
               'story-fingerprint-' || lpad(series_no::text, 5, '0'),
               series_no,
               series_no,
               '{"source_ids":["push-plan-source"],"reporting_origins":["Push Plan Wire"]}'::jsonb
          FROM generate_series(%s::integer, %s::integer) series_no
        """,
        (start, stop),
    )
    conn.execute(
        """
        INSERT INTO news_story_members(story_id, item_id)
        SELECT 'story-' || lpad(series_no::text, 5, '0'),
               'item-' || lpad(series_no::text, 5, '0')
          FROM generate_series(%s::integer, %s::integer) series_no
        """,
        (start, stop),
    )


def _vacuum_analyze(conn: Any, table_name: str) -> None:
    conn.commit()
    raw_conn = conn._conn
    raw_conn.autocommit = True
    try:
        conn.execute(f"VACUUM (ANALYZE) {table_name}")
    finally:
        raw_conn.autocommit = False
