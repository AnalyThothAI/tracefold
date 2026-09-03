from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tracefold.app.query_audit import query_audit_catalog
from tracefold.platform.postgres.audit import (
    PostgresQueryAudit,
    QueryAuditCatalog,
    ReadQuerySpec,
    postgres_query_specs,
)
from tracefold.trading.storage.root import TradingRepository

_NEWS_QUERY_NAMES = (
    "news_feed_events",
    "news_search_identity",
    "news_search_event_symbols",
    "news_feed_asset_search",
    "news_feed_asset_search_counts",
    "news_feed_asset_search_cursor",
    "news_feed_text_search",
    "news_feed_text_search_counts",
    "news_feed_text_search_cursor",
    "news_event_detail",
    "news_event_asset_projection",
    "news_event_members",
    "news_event_verdicts",
    "news_storyline_status",
    "news_band_lookup",
    "news_status_ingest",
    "news_status_incidents_open",
    "news_status_recovery_backlog",
    "news_status_pipeline_24h",
    "news_status_delivery_1h",
    "news_status_learning_retention",
    "news_quote_snapshot_read",
    "news_reaction_due_scan",
    "news_reaction_attach",
    "news_review_task_queue",
    "news_review_task_evidence",
    "news_review_task_evidence_version",
    "news_review_active_agent",
    "news_review_coverage_source",
    "news_review_pairwise_queue",
    "news_review_proposal_candidates",
    "news_review_proposal_releases",
    "news_review_proposal_reports",
    "news_review_proposal_activations",
    "news_review_market",
)


def test_query_audit_requires_an_explicit_catalog_and_explains_only_its_queries():
    catalog = QueryAuditCatalog(
        queries=(ReadQuerySpec(name="owned_read", sql="SELECT 1", max_read_return_amplification=20.0),),
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
        "write_routes": [],
        "missing_query_names": [],
    }


def test_app_catalog_composes_platform_and_injected_news_query_specs():
    observed_now_ms: list[int] = []

    def news_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
        observed_now_ms.append(now_ms)
        return tuple(
            ReadQuerySpec(name=name, sql="SELECT 1", max_read_return_amplification=20.0) for name in _NEWS_QUERY_NAMES
        )

    catalog = query_audit_catalog(now_ms=123_456, news_query_specs=news_specs)

    assert observed_now_ms == [123_456]
    names = {query.name for query in catalog.queries}
    assert {query.name for query in postgres_query_specs(now_ms=123_456)} < names
    assert set(_NEWS_QUERY_NAMES) < names
    assert catalog.query_routes["/api/news/feed"] == (
        "news_feed_events",
        "news_search_identity",
        "news_search_event_symbols",
        "news_feed_asset_search",
        "news_feed_asset_search_counts",
        "news_feed_asset_search_cursor",
        "news_feed_text_search",
        "news_feed_text_search_counts",
        "news_feed_text_search_cursor",
        "news_event_asset_projection",
        "news_reaction_attach",
    )
    assert catalog.query_routes["/api/news/events/{event_id}"] == (
        "news_event_detail",
        "news_event_members",
        "news_event_verdicts",
        "news_event_asset_projection",
        "news_reaction_attach",
    )
    assert catalog.query_routes["/api/news/quotes"] == ("news_quote_snapshot_read",)
    assert "/api/news/review" not in catalog.query_routes
    # #475 PR-E adds one exact append-only operator control route; every other public route stays read-only.
    assert catalog.write_routes == {"/api/trading/execution/commands"}
    assert catalog.query_routes["/api/news/status"] == (
        "workers_runtime",
        "news_status_ingest",
        "news_status_incidents_open",
        "news_status_recovery_backlog",
        "news_status_pipeline_24h",
        "news_status_delivery_1h",
        "news_status_learning_retention",
    )
    # #510 PR-5a: each console read is certified twice, unfiltered and filtered, because the route
    # plans both and the audit must not certify a statement the route does not execute.
    assert catalog.query_routes["/api/trading/signals"] == (
        "trading_console_signals",
        "trading_console_signals_filtered",
    )
    assert catalog.query_routes["/api/trading/execution/observations"] == (
        "trading_console_observations",
        "trading_console_observations_filtered",
    )
    assert catalog.query_routes["/api/trading/execution/commands"] == (
        "trading_console_commands",
        "trading_console_commands_filtered",
    )
    assert not any(
        route.startswith(("/api/news/stories", "/api/news/brief", "/api/news/sources"))
        for route in catalog.query_routes
    )
    assert [query.name for query in catalog.queries if query.amplification_basis == "aggregate_input"] == [
        "trading_status_case_counts",
        "trading_status_signal_counts",
    ]


def test_trading_status_aggregates_bound_input_before_filtering() -> None:
    """#528 dropped the four unrendered counts, and with them the unbounded `OR` arm each needed."""

    queries = {query.name: query for query in query_audit_catalog(now_ms=123_456).queries}
    case_query = queries["trading_status_case_counts"]
    signal_query = queries["trading_status_signal_counts"]

    assert re.search(r"WHERE\s+created_at_ms\s*>=\s*%\(since\)s\s*$", case_query.sql.strip(), re.IGNORECASE)
    assert re.search(r"WHERE\s+observed_at_ns\s*>=\s*%\(since\)s\s*$", signal_query.sql.strip(), re.IGNORECASE)
    assert case_query.params == {"since": -86_276_544}
    assert signal_query.params == {"since": -86_276_544_000_000}


def test_default_news_query_specs_cover_every_news_route_query():
    catalog = query_audit_catalog(now_ms=123_456)
    names = {query.name for query in catalog.queries}
    for route, route_queries in catalog.query_routes.items():
        if route.startswith("/api/news/"):
            assert set(route_queries) <= names, route
    assert set(_NEWS_QUERY_NAMES) <= names
    assert [query.name for query in catalog.queries if query.amplification_basis == "aggregate_input"] == [
        "news_feed_asset_search_counts",
        "news_feed_text_search_counts",
        "trading_status_case_counts",
        "trading_status_signal_counts",
    ]


def test_app_catalog_rejects_unapproved_aggregate_input_queries():
    def news_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
        del now_ms
        return (
            ReadQuerySpec(
                name="news_status_pipeline_24h",
                sql="SELECT 1",
                amplification_basis="aggregate_input",
                max_read_return_amplification=20.0,
            ),
        )

    with pytest.raises(ValueError, match="only bounded aggregate reads"):
        query_audit_catalog(now_ms=0, news_query_specs=news_specs)


def test_trading_console_audit_explains_the_statements_the_routes_execute():
    """#510 PR-5a: the audited console SQL is the repository's own, byte for byte.

    The audit used to register a second copy of each console statement, without the optional
    predicates the route adds. Editing one and not the other left the plan audit passing on SQL nobody
    runs. Driving `TradingRepository` against a recording connection is what makes that unrepeatable.
    """

    now_ms = 123_456
    since_ms = now_ms - 24 * 3_600_000
    since_ns = since_ms * 1_000_000
    queries = {query.name: query for query in query_audit_catalog(now_ms=now_ms).queries}
    conn = RecordingStatementConn()
    repository = TradingRepository(conn)

    repository.console_cases(since_ms=since_ms, underlying_key=None, states=(), before=None, limit=101)
    repository.console_cases(
        since_ms=since_ms,
        underlying_key="crypto:BTC",
        states=("SIGNAL_EMITTED", "NO_TRADE"),
        before=(since_ms + 1, "z" * 32),
        limit=101,
    )
    repository.console_signals(since_ns=since_ns, market_key=None, before=None, limit=101)
    repository.console_signals(
        since_ns=since_ns,
        market_key="crypto:perp:BTC:USDT",
        before=(since_ns + 1, "z" * 64),
        limit=101,
    )
    repository.console_execution_observations(
        since_ns=since_ns, account_slot=None, normalized_kind=None, before=None, limit=101
    )
    repository.console_execution_observations(
        since_ns=since_ns,
        account_slot="query-audit-disabled",
        normalized_kind="signal_disposition",
        before=(since_ns + 1, "z" * 64),
        limit=101,
    )
    repository.console_operator_intents(since_ns=since_ns, account_slot=None, action=None, before=None, limit=101)
    repository.console_operator_intents(
        since_ns=since_ns,
        account_slot="query-audit-disabled",
        action="flatten",
        before=(since_ns + 1, "z" * 64),
        limit=101,
    )

    executed = conn.statements
    audited = [
        (queries[name].sql, queries[name].params)
        for name in (
            "trading_console_cases",
            "trading_console_cases_filtered",
            "trading_console_signals",
            "trading_console_signals_filtered",
            "trading_console_observations",
            "trading_console_observations_filtered",
            "trading_console_commands",
            "trading_console_commands_filtered",
        )
    ]
    assert executed == audited
    # The filtered half really is a different statement, or registering it twice proves nothing.
    assert audited[0][0] != audited[1][0]
    assert "underlying_key = %(underlying)s" in audited[1][0]
    assert "state = ANY(%(states)s)" in audited[1][0]
    assert "(created_at_ms, case_id) < (%(before_ms)s, %(before_id)s)" in audited[1][0]


def test_query_audit_covers_every_public_openapi_route():
    root = Path(__file__).resolve().parents[2]
    openapi = json.loads((root / "docs/generated/openapi.json").read_text())
    openapi_paths = set(openapi["paths"])
    catalog = query_audit_catalog(now_ms=0)

    assert "/ws" not in catalog.query_routes
    assert set(catalog.query_routes) | set(catalog.no_sql_routes) | set(catalog.write_routes) == openapi_paths
    query_names = {item.name for item in catalog.queries}
    assert all(
        query_name in query_names for route_queries in catalog.query_routes.values() for query_name in route_queries
    )


def test_audited_public_and_high_risk_queries_name_their_columns():
    select_star = re.compile(r"\bSELECT\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?\*", re.IGNORECASE)
    catalog = query_audit_catalog(now_ms=0)
    public_names = {name for names in catalog.query_routes.values() for name in names}

    assert [
        query.name for query in catalog.queries if query.name in public_names and select_star.search(query.sql)
    ] == []


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
        catalog=_single_query_catalog(
            ReadQuerySpec(name="bounded_read", sql="SELECT 1", max_read_return_amplification=20.0)
        ),
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
        catalog=_single_query_catalog(
            ReadQuerySpec(name="bitmap_read", sql="SELECT 1", max_read_return_amplification=20.0)
        ),
    ).run(analyze=True)

    assert payload["ok"] is True
    assert payload["queries"][0]["metrics"]["read_rows"] == 116
    assert payload["queries"][0]["metrics"]["read_return_amplification"] == 2.32


def test_analyzed_query_audit_defaults_to_returned_rows_for_aggregate_amplification():
    conn = RecordingJsonPlanConn(_aggregate_plan(input_rows=500, returned_rows=1))

    payload = PostgresQueryAudit(
        conn,
        catalog=_single_query_catalog(
            ReadQuerySpec(name="aggregate_read", sql="SELECT 1", max_read_return_amplification=20.0)
        ),
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
                max_read_return_amplification=20.0,
            )
        ),
    ).run(analyze=True)
    facets = payload["queries"][0]

    assert facets["ok"] is True
    assert facets["metrics"]["amplification_basis"] == "aggregate_input"
    assert facets["metrics"]["amplification_basis_rows"] == 500
    assert facets["metrics"]["read_return_amplification"] == 1.0
    assert facets["violations"] == []


def test_each_query_owns_its_amplification_budget():
    conn = RecordingJsonPlanConn(_aggregate_plan(input_rows=10, returned_rows=1))
    query = ReadQuerySpec(
        name="tight_read",
        sql="SELECT 1",
        max_read_return_amplification=5.0,
    )

    payload = PostgresQueryAudit(conn, catalog=_single_query_catalog(query)).run(analyze=True)

    audited = payload["queries"][0]
    assert audited["budget"] == {"max_read_return_amplification": 5.0}
    assert audited["violations"] == ["read_return_amplification_exceeded"]


def test_catalog_rejects_a_query_without_its_own_amplification_budget():
    with pytest.raises(ValueError, match="amplification budget missing: unbudgeted"):
        _single_query_catalog(ReadQuerySpec(name="unbudgeted", sql="SELECT 1"))


class RecordingStatementConn:
    """Captures the exact SQL and bound parameters a repository method executes."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return self

    def fetchall(self):
        return []


class RecordingJsonPlanConn:
    def __init__(self, statement):
        self.statement = statement

    def execute(self, sql, params=None):
        del sql, params
        return self

    def fetchall(self):
        return [{"QUERY PLAN": [self.statement]}]


def _single_query_catalog(query: ReadQuerySpec) -> QueryAuditCatalog:
    return QueryAuditCatalog(
        queries=(query,),
        query_routes={"/audit": (query.name,)},
        no_sql_routes=frozenset(),
    )


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
