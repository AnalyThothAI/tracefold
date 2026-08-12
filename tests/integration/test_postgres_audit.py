from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.query_audit import query_audit_catalog
from tracefold.news.repository import NewsRepository
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
        return tuple(
            ReadQuerySpec(
                name=name,
                sql="SELECT 1",
                amplification_basis=("aggregate_input" if name == "news_feed_focus_facets" else "returned_rows"),
            )
            for name in _NEWS_QUERY_NAMES
        )

    catalog = query_audit_catalog(now_ms=123_456, news_query_specs=news_specs)

    assert observed_now_ms == [123_456]
    names = {query.name for query in catalog.queries}
    assert {query.name for query in postgres_query_specs(now_ms=123_456)} < names
    assert set(_NEWS_QUERY_NAMES) < names
    assert catalog.query_routes["/api/news/feed"] == (
        "news_feed_focus_rows",
        "news_feed_focus_facets",
        "news_feed_push_contexts",
    )
    assert catalog.query_routes["/api/news/status"] == (
        "workers_runtime",
        "news_status_opennews",
        "news_status_rss",
        "news_status_projection",
        "news_brief",
        "news_push_state",
        "news_push_oldest_due",
        "news_push_oldest_waiting",
        "news_push_translation_24h",
        "news_push_delivery_24h",
    )
    assert [query.name for query in catalog.queries if query.amplification_basis == "aggregate_input"] == [
        "news_feed_focus_facets"
    ]


def test_app_catalog_rejects_aggregate_input_for_any_query_except_feed_facets():
    def news_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
        del now_ms
        return (
            ReadQuerySpec(
                name="news_feed_focus_facets",
                sql="SELECT 1",
                amplification_basis="aggregate_input",
            ),
            ReadQuerySpec(
                name="news_push_translation_24h",
                sql="SELECT 1",
                amplification_basis="aggregate_input",
            ),
        )

    with pytest.raises(ValueError, match="only news_feed_focus_facets"):
        query_audit_catalog(now_ms=0, news_query_specs=news_specs)


_NEWS_QUERY_NAMES = (
    "news_feed_focus_rows",
    "news_feed_focus_facets",
    "news_feed_push_contexts",
    "news_story",
    "news_story_members",
    "news_brief",
    "news_sources",
    "news_status_opennews",
    "news_status_rss",
    "news_status_projection",
    "news_push_state",
    "news_push_oldest_due",
    "news_push_oldest_waiting",
    "news_push_translation_24h",
    "news_push_delivery_24h",
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

        payload = PostgresQueryAudit(conn, catalog=_composed_catalog()).run(analyze=False)
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


def test_query_audit_token_radar_latest_reads_only_the_singleton_without_product_params():
    query = next(item for item in postgres_query_specs(now_ms=0) if item.name == "token_radar_latest")

    assert "token_radar_current" in query.sql
    assert "singleton_key = true" in query.sql
    assert "token_profile_current" not in query.sql
    assert query.params == ()
    assert _composed_catalog().query_routes["/api/token-radar"] == ("token_radar_latest",)


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
                name="news_feed_focus_facets",
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

            """
        )
        conn.commit()
        _vacuum_analyze(conn, "news_items")
        _vacuum_analyze(conn, "news_story_members")
        _vacuum_analyze(conn, "news_stories")

        payload = PostgresQueryAudit(conn, catalog=_composed_catalog()).run(analyze=True)
        facets = next(item for item in payload["queries"] if item["name"] == "news_feed_focus_facets")
    finally:
        conn.close()

    assert facets["ok"] is True
    assert facets["metrics"]["large_seq_scans"] == []
    assert facets["metrics"]["read_rows"] < 5_000
    assert facets["metrics"]["read_return_amplification"] <= 20
    assert facets["metrics"]["temp_read_blocks"] == 0
    assert facets["metrics"]["temp_written_blocks"] == 0


def test_news_push_health_audit_is_bounded_and_preserves_snapshot_semantics(tmp_path):
    audit_now_ms = 1_800_000_000_000
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        conn.execute(
            """
            UPDATE news_push_state
               SET baseline_at_ms = %s,
                   updated_at_ms = %s
             WHERE singleton_key = 'current'
            """,
            (audit_now_ms - 1, audit_now_ms - 1),
        )
        conn.execute(
            """
            INSERT INTO news_push_deliveries(
              story_id, selected_item_id, provider_score,
              threshold_observed_at_ms, source_payload, delivery_payload,
              payload_fingerprint, translation_status, status,
              delivery_attempts, next_attempt_at_ms, sent_at_ms,
              created_at_ms, updated_at_ms
            )
            SELECT lpad(to_hex(value), 64, '0'),
                   'selected-' || value,
                   90,
                   %s - 60000,
                   '{}'::jsonb,
                   jsonb_build_object(
                     'presentation', jsonb_build_object(
                       'prompt_version', 'title_zh_v2',
                       'fallback_code', CASE
                         WHEN value %% 10 = 0 THEN 'news_push_translation_rate_limited'
                         ELSE NULL
                       END,
                       'translation_attempted_at_ms', CASE
                         WHEN value <= 200 THEN %s - value
                         ELSE %s - 172800000 - value
                       END,
                       'translation_duration_ms', 1000 + value %% 2500
                     )
                   ),
                   repeat('a', 64),
                   CASE WHEN value %% 10 = 0 THEN 'unavailable' ELSE 'translated' END,
                   CASE WHEN value %% 2 = 0 THEN 'sent' ELSE 'terminal' END,
                   1,
                   NULL,
                   CASE WHEN value %% 2 = 0 THEN
                     CASE
                       WHEN value <= 200 THEN %s - value
                       ELSE %s - 172800000 - value
                     END
                   ELSE NULL END,
                   %s - 172800000 - value,
                   CASE
                     WHEN value <= 200 THEN %s - value
                     ELSE %s - 172800000 - value
                   END
              FROM generate_series(1, 12000) value
            """,
            (
                audit_now_ms,
                audit_now_ms,
                audit_now_ms,
                audit_now_ms,
                audit_now_ms,
                audit_now_ms,
                audit_now_ms,
                audit_now_ms,
            ),
        )
        conn.execute(
            """
            UPDATE news_push_deliveries
               SET translation_prompt_version = 'title_zh_v2',
                   translation_attempted_at_ms = (
                     delivery_payload #>> '{presentation,translation_attempted_at_ms}'
                   )::bigint,
                   translation_duration_ms = (
                     delivery_payload #>> '{presentation,translation_duration_ms}'
                   )::bigint,
                   translation_fallback_code = nullif(
                     delivery_payload #>> '{presentation,fallback_code}',
                     ''
                   );

            UPDATE news_push_state
               SET total_count = 12000,
                   sent_count = 6000,
                   terminal_count = 6000
             WHERE singleton_key = 'current';
            """
        )
        conn.commit()
        _vacuum_analyze(conn, "news_push_deliveries")

        snapshot = NewsRepository(conn).push_health_snapshot(now_ms=audit_now_ms)
        payload = PostgresQueryAudit(
            conn,
            catalog=_composed_catalog(now_ms=audit_now_ms),
        ).run(analyze=True)
        queries = {item["name"]: item for item in payload["queries"]}
    finally:
        conn.close()

    assert snapshot["total_count"] == 12000
    assert snapshot["pending_count"] == 0
    assert snapshot["sent_count"] == 6000
    assert snapshot["terminal_count"] == 6000
    assert snapshot["translation_24h"]["attempted"] == 200
    assert snapshot["translation_24h"]["succeeded"] == 180
    assert snapshot["translation_24h"]["failure_counts"] == {"news_push_translation_rate_limited": 20}
    assert snapshot["delivery_24h"]["completed"] == 200
    assert snapshot["delivery_24h"]["over_120s"] == 0

    for query_name in (
        "news_push_state",
        "news_push_oldest_due",
        "news_push_oldest_waiting",
        "news_push_translation_24h",
        "news_push_delivery_24h",
    ):
        query = queries[query_name]
        assert query["ok"] is True
        assert query["metrics"]["large_seq_scans"] == []
        assert query["metrics"]["temp_read_blocks"] == 0
        assert query["metrics"]["temp_written_blocks"] == 0
        assert query["metrics"]["read_return_amplification"] <= 20


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
