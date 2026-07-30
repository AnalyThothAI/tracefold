from __future__ import annotations

import json
from pathlib import Path

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.platform.postgres.postgres_audit import (
    HOT_QUERIES,
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
    assert payload["projection_schema"]["token_radar_current_rows"] is True
    assert payload["projection_schema"]["token_radar_publication_state"] is True
    assert payload["projection_schema"]["news_projection_summary"] is True
    assert "projection_offsets" not in payload["projection_schema"]
    assert "projection_runs" not in payload["projection_schema"]
    assert payload["foreign_key_checks"]["token_radar_current_rows_missing_intents"] == 0


def test_query_audit_explains_hot_read_paths_without_analyze(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)

        payload = PostgresQueryAudit(conn, token_radar_projection_version="token-radar-test").run(analyze=False)
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
        conn.execute(
            """
            INSERT INTO news_story_facet_counts (
              facet_type, facet_value, story_count, updated_at_ms
            )
            VALUES ('category', 'stale-test-value', 1, 1)
            """
        )
        stale = ProjectionValidationAudit(conn).run(sample=100)
    finally:
        conn.close()

    assert initial["ok"] is True
    assert initial["mismatch_count"] == 0
    assert stale["ok"] is False
    assert stale["checks"]["news_story_facet_mismatch"] == 1


def test_query_audit_analyzes_all_route_query_families_on_empty_schema(
    tmp_path,
):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)

        payload = PostgresQueryAudit(
            conn,
            token_radar_projection_version="token-radar-test",
        ).run(analyze=True)
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


def test_query_audit_token_radar_latest_declares_caller_supplied_projection_version_param():
    query = next(item for item in HOT_QUERIES if item["name"] == "token_radar_latest")

    assert "%(token_radar_projection_version)s" in query["sql"]
    assert query["params"] == {"token_radar_projection_version": None}


def test_query_audit_search_paths_use_the_public_24h_window_and_bounded_routes():
    lexical = next(item for item in HOT_QUERIES if item["name"] == "search_v2_lexical")
    substring = next(item for item in HOT_QUERIES if item["name"] == "search_v2_substring")

    assert "received_at_ms >= %(search_cutoff_at_ms)s" in lexical["sql"]
    assert "received_at_ms >= %(search_cutoff_at_ms)s" in substring["sql"]
    assert lexical["params"]["search_cutoff_at_ms"] is None
    assert substring["params"]["search_cutoff_at_ms"] is None
    assert all(item["name"] != "search_v2_trigram" for item in HOT_QUERIES)


def test_query_audit_news_status_reads_the_singleton_projection_summary():
    query = next(item for item in HOT_QUERIES if item["name"] == "news_status")

    assert "news_projection_summary" in query["sql"]
    assert "FROM news_stories" not in query["sql"]


def test_query_audit_public_news_views_read_only_bounded_models():
    story_facets = next(item for item in HOT_QUERIES if item["name"] == "news_feed_story_facets")
    source_facets = next(item for item in HOT_QUERIES if item["name"] == "news_feed_source_facets")
    brief = next(item for item in HOT_QUERIES if item["name"] == "news_brief")

    assert "news_story_facet_counts" in story_facets["sql"]
    assert "FROM news_stories" not in story_facets["sql"]
    assert "news_source_facet_counts" in source_facets["sql"]
    assert "news_story_members" not in source_facets["sql"]
    assert "news_brief_selection_current" in brief["sql"]
    assert "row_number()" not in brief["sql"].lower()


def test_query_audit_stocks_radar_reads_only_current_projection():
    query = next(item for item in HOT_QUERIES if item["name"] == "stocks_radar_recent")

    assert "stocks_radar_current_rows" in query["sql"]
    assert "FROM events" not in query["sql"]
    assert "token_intent_resolutions" not in query["sql"]


def test_query_audit_does_not_restore_retired_token_factor_settlement_hot_path():
    assert all(item["name"] != "token_factor_settlement_rows" for item in HOT_QUERIES)


def test_query_audit_binds_caller_supplied_token_radar_projection_version():
    conn = RecordingExplainConn()

    payload = PostgresQueryAudit(
        conn,
        token_radar_projection_version="token-radar-custom",
    ).run(analyze=False)

    assert payload["ok"] is True
    assert {"token_radar_projection_version": "token-radar-custom"} in conn.params_seen


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
