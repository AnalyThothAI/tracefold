from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.query_audit import query_audit_catalog
from tracefold.platform.postgres.postgres_audit import (
    NEWS_TABLES,
    PostgresOperationalAudit,
    PostgresQueryAudit,
    ProjectionValidationAudit,
    QueryAuditCatalog,
    ReadQuerySpec,
    postgres_query_specs,
)
from tracefold.platform.postgres.postgres_migrations import latest_migration_version


def test_query_audit_requires_an_explicit_catalog_and_explains_only_its_queries():
    catalog = QueryAuditCatalog(
        queries=(ReadQuerySpec(name="owned_read", sql="SELECT 1"),),
        query_routes={"/owned": ("owned_read",)},
        no_sql_routes=frozenset(),
    )
    conn = RecordingJsonPlanConn(_aggregate_plan(input_rows=1, returned_rows=1))

    payload = PostgresQueryAudit(conn, catalog=catalog).run(analyze=True)

    assert payload["ok"] is True
    assert [query["name"] for query in payload["queries"]] == ["owned_read"]
    assert payload["route_coverage"] == {
        "query_routes": {"/owned": ("owned_read",)},
        "no_sql_routes": [],
        "missing_query_names": [],
    }


def test_app_catalog_composes_platform_and_injected_news_query_specs():
    observed_now_ms: list[int] = []

    def news_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
        observed_now_ms.append(now_ms)
        return tuple(ReadQuerySpec(name=name, sql="SELECT 1") for name in _NEWS_QUERY_NAMES)

    catalog = query_audit_catalog(now_ms=123_456, news_query_specs=news_specs)

    assert observed_now_ms == [123_456]
    names = {query.name for query in catalog.queries}
    assert {query.name for query in postgres_query_specs(now_ms=123_456)} < names
    assert set(_NEWS_QUERY_NAMES) < names
    assert catalog.query_routes["/api/news/feed"] == (
        "news_feed_events",
        "news_feed_symbol_filter",
        "news_feed_search",
    )
    assert catalog.query_routes["/api/news/events/{event_id}"] == (
        "news_event_detail",
        "news_event_members",
        "news_event_verdicts",
    )
    assert catalog.query_routes["/api/news/status"] == (
        "workers_runtime",
        "news_status_ingest",
        "news_status_incidents_open",
        "news_status_pipeline_24h",
        "news_status_delivery_1h",
        "news_control_state",
    )
    assert not any(
        route.startswith(("/api/news/stories", "/api/news/brief", "/api/news/sources"))
        for route in catalog.query_routes
    )
    assert [query.name for query in catalog.queries if query.amplification_basis == "aggregate_input"] == []


def test_default_news_query_specs_cover_every_news_route_query():
    catalog = query_audit_catalog(now_ms=123_456)
    names = {query.name for query in catalog.queries}
    for route, route_queries in catalog.query_routes.items():
        if route.startswith("/api/news/"):
            assert set(route_queries) <= names, route
    assert set(_NEWS_QUERY_NAMES) <= names


def test_app_catalog_rejects_unapproved_aggregate_input_queries():
    def news_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
        del now_ms
        return (
            ReadQuerySpec(
                name="news_status_pipeline_24h",
                sql="SELECT 1",
                amplification_basis="aggregate_input",
            ),
        )

    with pytest.raises(ValueError, match="only bounded aggregate reads"):
        query_audit_catalog(now_ms=0, news_query_specs=news_specs)


_NEWS_QUERY_NAMES = (
    "news_feed_events",
    "news_feed_symbol_filter",
    "news_feed_search",
    "news_event_detail",
    "news_event_members",
    "news_event_verdicts",
    "news_storyline_status",
    "news_band_lookup",
    "news_status_ingest",
    "news_status_incidents_open",
    "news_status_pipeline_24h",
    "news_status_delivery_1h",
    "news_control_state",
)


def _single_query_catalog(query: ReadQuerySpec) -> QueryAuditCatalog:
    return QueryAuditCatalog(
        queries=(query,),
        query_routes={"/audit": (query.name,)},
        no_sql_routes=frozenset(),
    )


def _composed_catalog(*, now_ms: int = 0) -> QueryAuditCatalog:
    return query_audit_catalog(now_ms=now_ms)


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
    assert "token_radar_current" not in payload["projection_schema"]
    assert payload["projection_schema"] == {}
    assert payload["news_schema"] == {
        "expected_tables": list(NEWS_TABLES),
        "actual_tables": sorted(NEWS_TABLES),
        "exact": True,
    }
    assert len(payload["news_schema"]["actual_tables"]) == 12
    assert {"news_stories", "news_brief_current", "news_push_state", "news_sources"}.isdisjoint(
        payload["news_schema"]["actual_tables"]
    )
    assert "projection_offsets" not in payload["projection_schema"]
    assert "projection_runs" not in payload["projection_schema"]
    assert not any(name.startswith("token_radar") for name in payload["foreign_key_checks"])


def test_query_audit_explains_hot_read_paths_without_analyze(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)

        payload = PostgresQueryAudit(conn, catalog=_composed_catalog()).run(analyze=False)
    finally:
        conn.close()

    names = {item["name"] for item in payload["queries"]}
    assert payload["ok"] is True
    assert payload["analyze"] is False
    expected = {"recent_all", "search_v2_lexical", "search_v2_substring", "target_posts_recent"}
    assert expected.issubset(names)
    assert "token_radar_latest" not in names
    assert "search_v2_trigram" not in names
    assert all(item["plan"] for item in payload["queries"])


def test_projection_validation_checks_bounded_public_models(tmp_path):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        initial = ProjectionValidationAudit(conn).run(sample=100)
        conn.execute("DELETE FROM news_control_state")
        stale = ProjectionValidationAudit(conn).run(sample=100)
    finally:
        conn.close()

    assert initial["ok"] is True
    assert initial["mismatch_count"] == 0
    assert set(initial["checks"]) == {
        "news_ingest_state_mismatch",
        "news_control_state_mismatch",
        "news_delivery_state_mismatch",
    }
    assert stale["ok"] is False
    assert stale["checks"]["news_control_state_mismatch"] == 1


def test_query_audit_analyzes_all_route_query_families_on_empty_schema(
    tmp_path,
):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)

        payload = PostgresQueryAudit(conn, catalog=_composed_catalog()).run(analyze=True)
    finally:
        conn.close()

    assert payload["ok"] is True
    assert payload["analyze"] is True
    assert all(item["metrics"]["plan_json_valid"] for item in payload["queries"])
    assert all(item["violations"] == [] for item in payload["queries"])


def test_query_audit_target_posts_uses_resolution_targets():
    query = next(item for item in postgres_query_specs(now_ms=0) if item.name == "target_posts_recent")

    assert "target_type" in query.sql
    assert "target_id" in query.sql
    assert "first_seen_ms" not in query.sql
    assert "confidence" not in query.sql


def test_query_audit_has_no_token_radar_surface():
    catalog = _composed_catalog()
    assert all(item.name != "token_radar_latest" for item in catalog.queries)
    assert all("token_radar_current" not in item.sql for item in catalog.queries)
    assert "/api/token-radar" not in catalog.query_routes
    assert catalog.query_routes["/api/live-market"] == ("live_market_current",)


def test_query_audit_search_paths_use_the_public_24h_window_and_bounded_routes():
    specs = postgres_query_specs(now_ms=86_400_123)
    lexical = next(item for item in specs if item.name == "search_v2_lexical")
    substring = next(item for item in specs if item.name == "search_v2_substring")

    assert "received_at_ms >= %(search_cutoff_at_ms)s" in lexical.sql
    assert "received_at_ms >= %(search_cutoff_at_ms)s" in substring.sql
    assert "ORDER BY received_at_ms DESC, event_id DESC" in lexical.sql
    assert "ts_rank_cd" in lexical.sql
    assert "LIMIT 50" in lexical.sql
    assert lexical.params["search_cutoff_at_ms"] == 123
    assert substring.params["search_cutoff_at_ms"] == 123
    assert all(item.name != "search_v2_trigram" for item in specs)


def test_query_audit_has_no_retired_stocks_product_surface():
    catalog = _composed_catalog()
    assert all(item.name != "stocks_radar_recent" for item in catalog.queries)
    assert "/api/stocks-radar" not in catalog.query_routes


def test_query_audit_does_not_restore_retired_token_factor_settlement_hot_path():
    assert all(item.name != "token_factor_settlement_rows" for item in _composed_catalog().queries)


def test_query_audit_covers_every_public_openapi_route_and_websocket():
    root = Path(__file__).resolve().parents[2]
    openapi = json.loads((root / "docs/generated/openapi.json").read_text())
    openapi_paths = set(openapi["paths"])
    catalog = _composed_catalog()
    covered_http_paths = set(catalog.query_routes) - {"/ws"}

    assert "/ws" in catalog.query_routes
    assert covered_http_paths | set(catalog.no_sql_routes) == openapi_paths
    query_names = {item.name for item in catalog.queries}
    assert all(
        query_name in query_names for route_queries in catalog.query_routes.values() for query_name in route_queries
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

    payload = PostgresQueryAudit(
        conn,
        catalog=_single_query_catalog(ReadQuerySpec(name="bounded_read", sql="SELECT 1")),
    ).run(analyze=True)

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

    payload = PostgresQueryAudit(
        conn,
        catalog=_single_query_catalog(ReadQuerySpec(name="bitmap_read", sql="SELECT 1")),
    ).run(analyze=True)

    assert payload["ok"] is True
    assert payload["queries"][0]["metrics"]["read_rows"] == 116
    assert payload["queries"][0]["metrics"]["read_return_amplification"] == 2.32


def test_analyzed_query_audit_defaults_to_returned_rows_for_aggregate_amplification():
    conn = RecordingJsonPlanConn(_aggregate_plan(input_rows=500, returned_rows=1))

    payload = PostgresQueryAudit(
        conn,
        catalog=_single_query_catalog(ReadQuerySpec(name="aggregate_read", sql="SELECT 1")),
    ).run(analyze=True)
    readiness = payload["queries"][0]

    assert readiness["ok"] is False
    assert readiness["metrics"]["amplification_basis"] == "returned_rows"
    assert readiness["metrics"]["amplification_basis_rows"] == 1
    assert readiness["metrics"]["read_return_amplification"] == 500.0
    assert readiness["violations"] == ["read_return_amplification_exceeded"]


def test_analyzed_query_audit_can_use_explicit_aggregate_input_amplification():
    conn = RecordingJsonPlanConn(_aggregate_plan(input_rows=500, returned_rows=1))

    payload = PostgresQueryAudit(
        conn,
        catalog=_single_query_catalog(
            ReadQuerySpec(
                name="bounded_aggregate",
                sql="SELECT 1",
                amplification_basis="aggregate_input",
            )
        ),
    ).run(analyze=True)
    facets = payload["queries"][0]

    assert facets["ok"] is True
    assert facets["metrics"]["amplification_basis"] == "aggregate_input"
    assert facets["metrics"]["amplification_basis_rows"] == 500
    assert facets["metrics"]["read_return_amplification"] == 1.0
    assert facets["violations"] == []


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


def _vacuum_analyze(conn, table_name: str) -> None:
    conn.commit()
    raw_conn = conn._conn
    raw_conn.autocommit = True
    try:
        conn.execute(f"VACUUM (ANALYZE) {table_name}")
    finally:
        raw_conn.autocommit = False
