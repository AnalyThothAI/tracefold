from __future__ import annotations

import json
from pathlib import Path

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.platform.postgres.postgres_audit import (
    HOT_QUERIES,
    NEWS_TABLES,
    PUBLIC_NO_SQL_ROUTES,
    PUBLIC_ROUTE_QUERY_COVERAGE,
    PostgresOperationalAudit,
    PostgresQueryAudit,
    ProjectionValidationAudit,
)
from tracefold.platform.postgres.postgres_migrations import latest_migration_version


def test_operational_audit_reports_counts_fk_checks_and_projection_schema(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)

        payload = PostgresOperationalAudit(conn).run()
    finally:
        conn.close()

    assert payload["ok"] is True
    assert payload["engine"] == "postgresql"
    assert payload["migration_version"] == latest_migration_version()
    assert payload["migration_status"] == "ready"
    assert payload["counts"]["events"] == 0
    assert payload["counts"]["registry_assets"] == 0
    assert payload["projection_schema"]["token_radar_current"] is True
    assert payload["projection_schema"]["news_projection_summary"] is True
    assert payload["projection_schema"]["news_brief_current"] is True
    assert payload["news_schema"] == {
        "expected_tables": list(NEWS_TABLES),
        "actual_tables": sorted(NEWS_TABLES),
        "exact": True,
    }
    assert len(payload["news_schema"]["actual_tables"]) == 9
    assert "news_story_title_translations" not in payload["projection_schema"]
    assert "projection_offsets" not in payload["projection_schema"]
    assert "projection_runs" not in payload["projection_schema"]
    assert "token_radar_current_rows_missing_intents" not in payload["foreign_key_checks"]


def test_query_audit_explains_hot_read_paths_without_analyze(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)

        payload = PostgresQueryAudit(conn).run(analyze=False)
    finally:
        conn.close()

    names = {item["name"] for item in payload["queries"]}
    assert payload["ok"] is True
    assert payload["analyze"] is False
    expected = {"recent_all", "search_v2_lexical", "search_v2_substring", "token_radar_latest", "target_posts_recent"}
    assert expected.issubset(names)
    assert "search_v2_trigram" not in names
    assert all(item["plan"] for item in payload["queries"])


def test_projection_validation_checks_bounded_public_models(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        initial = ProjectionValidationAudit(conn).run(sample=100)
        conn.execute("DELETE FROM news_brief_current")
        stale = ProjectionValidationAudit(conn).run(sample=100)
    finally:
        conn.close()

    assert initial["ok"] is True
    assert initial["mismatch_count"] == 0
    assert stale["ok"] is False
    assert stale["checks"]["news_brief_current_mismatch"] == 1


def test_query_audit_analyzes_all_route_query_families_on_empty_schema(
    tmp_path,
):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)

        payload = PostgresQueryAudit(conn).run(analyze=True)
    finally:
        conn.close()

    assert payload["ok"] is True
    assert payload["analyze"] is True
    assert all(item["metrics"]["plan_json_valid"] for item in payload["queries"])
    assert all(item["violations"] == [] for item in payload["queries"])


def test_query_audit_target_posts_uses_resolution_targets():
    query = next(item for item in HOT_QUERIES if item["name"] == "target_posts_recent")

    assert "target_type" in query["sql"]
    assert "target_id" in query["sql"]
    assert "first_seen_ms" not in query["sql"]
    assert "confidence" not in query["sql"]


def test_query_audit_token_radar_latest_reads_only_the_singleton_without_product_params():
    query = next(item for item in HOT_QUERIES if item["name"] == "token_radar_latest")

    assert "token_radar_current" in query["sql"]
    assert "singleton_key = true" in query["sql"]
    assert "token_profile_current" not in query["sql"]
    assert query["params"] == ()
    assert PUBLIC_ROUTE_QUERY_COVERAGE["/api/token-radar"] == ("token_radar_latest",)


def test_query_audit_search_paths_use_the_public_24h_window_and_bounded_routes():
    lexical = next(item for item in HOT_QUERIES if item["name"] == "search_v2_lexical")
    substring = next(item for item in HOT_QUERIES if item["name"] == "search_v2_substring")

    assert "received_at_ms >= %(search_cutoff_at_ms)s" in lexical["sql"]
    assert "received_at_ms >= %(search_cutoff_at_ms)s" in substring["sql"]
    assert "ORDER BY received_at_ms DESC, event_id DESC" in lexical["sql"]
    assert "ts_rank_cd" in lexical["sql"]
    assert "LIMIT 50" in lexical["sql"]
    assert lexical["params"]["search_cutoff_at_ms"] is None
    assert substring["params"]["search_cutoff_at_ms"] is None
    assert all(item["name"] != "search_v2_trigram" for item in HOT_QUERIES)


def test_query_audit_news_status_reads_the_singleton_projection_summary():
    query = next(item for item in HOT_QUERIES if item["name"] == "news_status")

    assert "news_projection_summary" in query["sql"]
    assert "FROM news_stories" not in query["sql"]


def test_query_audit_public_news_views_read_only_bounded_models():
    filtered_facets = next(item for item in HOT_QUERIES if item["name"] == "news_feed_filtered_facets")
    brief = next(item for item in HOT_QUERIES if item["name"] == "news_brief")
    sources = next(item for item in HOT_QUERIES if item["name"] == "news_sources")

    assert all(item["name"] != "news_feed_story_facets" for item in HOT_QUERIES)
    assert all(item["name"] != "news_feed_source_facets" for item in HOT_QUERIES)
    assert "current_member_scores AS MATERIALIZED" in filtered_facets["sql"]
    assert "FROM news_story_members members" in filtered_facets["sql"]
    assert "CROSS JOIN LATERAL" in filtered_facets["sql"]
    assert "WHERE items.item_id = members.item_id" in filtered_facets["sql"]
    assert "OFFSET 0" in filtered_facets["sql"]
    assert "news_story_members" in filtered_facets["sql"]
    assert "provider_metadata" in filtered_facets["sql"]
    assert "items.active" not in filtered_facets["sql"]
    assert "facet_facts" in filtered_facets["sql"]
    assert "source_ids" in filtered_facets["sql"]
    assert "reporting_origins" in filtered_facets["sql"]
    assert filtered_facets["amplification_basis"] == "aggregate_input"
    assert "LIMIT 101" in filtered_facets["sql"]
    assert "news_brief_current" in brief["sql"]
    assert "served_payload" in brief["sql"]
    assert "slot_status" in brief["sql"]
    assert "publication_id" not in brief["sql"]
    assert "news_brief_selection_current" not in brief["sql"]
    assert "row_number()" not in brief["sql"].lower()
    assert "next_fetch_at_ms" in sources["sql"]
    assert "claim_lease_expires_at_ms" in sources["sql"]
    assert "gap_unclosed" not in sources["sql"]


def test_query_audit_has_no_retired_stocks_product_surface():
    assert all(item["name"] != "stocks_radar_recent" for item in HOT_QUERIES)
    assert "/api/stocks-radar" not in PUBLIC_ROUTE_QUERY_COVERAGE


def test_query_audit_does_not_restore_retired_token_factor_settlement_hot_path():
    assert all(item["name"] != "token_factor_settlement_rows" for item in HOT_QUERIES)


def test_query_audit_covers_every_public_openapi_route_and_websocket():
    root = Path(__file__).resolve().parents[2]
    openapi = json.loads((root / "docs/generated/openapi.json").read_text())
    openapi_paths = set(openapi["paths"])
    covered_http_paths = set(PUBLIC_ROUTE_QUERY_COVERAGE) - {"/ws"}

    assert "/ws" in PUBLIC_ROUTE_QUERY_COVERAGE
    assert covered_http_paths | set(PUBLIC_NO_SQL_ROUTES) == openapi_paths
    query_names = {str(item["name"]) for item in HOT_QUERIES}
    assert all(
        query_name in query_names
        for route_queries in PUBLIC_ROUTE_QUERY_COVERAGE.values()
        for query_name in route_queries
    )


def test_analyzed_query_audit_rejects_large_seq_scan_temp_spill_and_amplification():
    conn = RecordingJsonPlanConn(
        {
            "Plan": {
                "Node Type": "Aggregate",
                "Actual Rows": 1,
                "Actual Loops": 1,
                "Temp Read Blocks": 2,
                "Temp Written Blocks": 3,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "events",
                        "Plan Rows": 50_000,
                        "Actual Rows": 500,
                        "Actual Loops": 1,
                    }
                ],
            },
            "Planning Time": 0.5,
            "Execution Time": 2.0,
        }
    )

    payload = PostgresQueryAudit(conn).run(analyze=True)

    assert payload["ok"] is False
    assert set(payload["queries"][0]["violations"]) == {
        "unexpected_large_table_seq_scan",
        "temp_spill",
        "read_return_amplification_exceeded",
    }


def test_analyzed_query_audit_counts_bitmap_heap_rows_not_index_candidates():
    conn = RecordingJsonPlanConn(
        {
            "Plan": {
                "Node Type": "Limit",
                "Actual Rows": 50,
                "Actual Loops": 1,
                "Plans": [
                    {
                        "Node Type": "Bitmap Heap Scan",
                        "Relation Name": "events",
                        "Actual Rows": 116,
                        "Actual Loops": 1,
                        "Plans": [
                            {
                                "Node Type": "BitmapAnd",
                                "Actual Rows": 0,
                                "Actual Loops": 1,
                                "Plans": [
                                    {
                                        "Node Type": "Bitmap Index Scan",
                                        "Index Name": "idx_events_search_tsv",
                                        "Actual Rows": 5_239,
                                        "Actual Loops": 1,
                                    },
                                    {
                                        "Node Type": "Bitmap Index Scan",
                                        "Index Name": "idx_events_received",
                                        "Actual Rows": 78_859,
                                        "Actual Loops": 1,
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
            "Planning Time": 1.0,
            "Execution Time": 50.0,
        }
    )

    payload = PostgresQueryAudit(conn).run(analyze=True)

    assert payload["ok"] is True
    assert payload["queries"][0]["metrics"]["read_rows"] == 116
    assert payload["queries"][0]["metrics"]["read_return_amplification"] == 2.32


def test_analyzed_query_audit_defaults_to_returned_rows_for_aggregate_amplification():
    conn = RecordingJsonPlanConn(_aggregate_plan(input_rows=500, returned_rows=1))

    payload = PostgresQueryAudit(conn).run(analyze=True)
    readiness = next(item for item in payload["queries"] if item["name"] == "readiness_schema")

    assert readiness["ok"] is False
    assert readiness["metrics"]["amplification_basis"] == "returned_rows"
    assert readiness["metrics"]["amplification_basis_rows"] == 1
    assert readiness["metrics"]["read_return_amplification"] == 500.0
    assert readiness["violations"] == ["read_return_amplification_exceeded"]


def test_analyzed_query_audit_can_use_explicit_aggregate_input_amplification():
    conn = RecordingJsonPlanConn(_aggregate_plan(input_rows=500, returned_rows=1))

    payload = PostgresQueryAudit(conn).run(analyze=True)
    facets = next(item for item in payload["queries"] if item["name"] == "news_feed_filtered_facets")

    assert facets["ok"] is True
    assert facets["metrics"]["amplification_basis"] == "aggregate_input"
    assert facets["metrics"]["amplification_basis_rows"] == 500
    assert facets["metrics"]["read_return_amplification"] == 1.0
    assert facets["violations"] == []


def test_news_filtered_facet_audit_is_bounded_by_current_membership_not_item_history(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, tier, lang, enabled, consecutive_failures,
              created_at_ms, updated_at_ms, source_kind, live_connected
            ) VALUES (
              'audit-source', 'Audit Source', 2, 'en', true, 0,
              0, 0, 'opennews', false
            );

            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, reporting_origin, title, description, lang,
              published_at_ms, first_observed_at_ms, last_observed_at_ms,
              content_fingerprint, level, category, classification_source,
              classification_confidence, importance_score, importance_factors,
              active, created_at_ms, updated_at_ms
            )
            SELECT 'history-' || value, 'audit-source', 'history-' || value,
                   'history-' || value, jsonb_build_object('score', 90),
                   'History Wire', 'Historical report ' || value, '', 'en',
                   value, value, value, 'history-fingerprint-' || value,
                   'info', 'general', 'keyword', 1, 1, '{}'::jsonb,
                   false, value, value
              FROM generate_series(1, 12000) value;

            INSERT INTO news_items(
              item_id, source_id, source_item_key, provider_record_id,
              provider_metadata, reporting_origin, title, description, lang,
              published_at_ms, first_observed_at_ms, last_observed_at_ms,
              content_fingerprint, level, category, classification_source,
              classification_confidence, importance_score, importance_factors,
              active, created_at_ms, updated_at_ms
            )
            SELECT 'member-' || value, 'audit-source', 'member-' || value,
                   'member-' || value, jsonb_build_object('score', 80),
                   'Audit Wire', 'Current report ' || value, '', 'en',
                   20000 + value, 20000 + value, 20000 + value,
                   'member-fingerprint-' || value,
                   'info', 'general', 'keyword', 1, 1, '{}'::jsonb,
                   true, 20000 + value, 20000 + value
              FROM generate_series(1, 200) value;

            INSERT INTO news_stories(
              story_id, canonical_key, canonical_title,
              representative_item_id, representative_source_id,
              representative_title, representative_description,
              scoring_item_id, level, category, importance_score,
              importance_factors, item_count, source_count,
              first_published_at_ms, last_published_at_ms,
              state_fingerprint, created_at_ms, updated_at_ms, facet_facts
            )
            SELECT 'story-' || value, 'story-key-' || value,
                   'Story ' || value, 'member-' || (value * 2 - 1),
                   'audit-source', 'Story ' || value, '',
                   'member-' || (value * 2 - 1), 'info', 'general', 1,
                   '{}'::jsonb, 2, 1, 20000 + value * 2 - 1,
                   20000 + value * 2, 'story-fingerprint-' || value,
                   30000 + value, 30000 + value,
                   '{"source_ids":["audit-source"],"reporting_origins":["Audit Wire"]}'::jsonb
              FROM generate_series(1, 100) value;

            INSERT INTO news_story_members(story_id, item_id)
            SELECT 'story-' || ((value + 1) / 2), 'member-' || value
              FROM generate_series(1, 200) value;

            ANALYZE news_items;
            ANALYZE news_story_members;
            ANALYZE news_stories;
            """
        )
        conn.commit()

        payload = PostgresQueryAudit(conn).run(analyze=True)
        facets = next(item for item in payload["queries"] if item["name"] == "news_feed_filtered_facets")
    finally:
        conn.close()

    assert facets["ok"] is True
    assert facets["metrics"]["large_seq_scans"] == []
    assert facets["metrics"]["read_rows"] < 5_000
    assert facets["metrics"]["read_return_amplification"] <= 20


class RecordingExplainConn:
    def __init__(self):
        self.params_seen = []

    def execute(self, sql, params=None):
        self.params_seen.append(params)
        return self

    def fetchall(self):
        return [{"QUERY PLAN": "ok"}]


class RecordingJsonPlanConn:
    def __init__(self, statement):
        self.statement = statement

    def execute(self, sql, params=None):
        del sql, params
        return self

    def fetchall(self):
        return [{"QUERY PLAN": [self.statement]}]


def _aggregate_plan(*, input_rows: int, returned_rows: int) -> dict:
    return {
        "Plan": {
            "Node Type": "Aggregate",
            "Actual Rows": returned_rows,
            "Actual Loops": 1,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "events",
                    "Plan Rows": input_rows,
                    "Actual Rows": input_rows,
                    "Actual Loops": 1,
                }
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 2.0,
    }
