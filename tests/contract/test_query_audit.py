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
    # #553: two statements per market list request and two per detail request. The timeline is its own
    # bounded read rather than a wider list scan, so both are named.
    "news_market_groups",
    "news_market_sources",
    "news_market_item",
    "news_market_group_timeline",
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
    # #553: the market surface plans its own statements, and every one of them is named here. A route
    # whose reads were not manifested would let `db query-audit --analyze` report full coverage while
    # never planning the widest scan on the public surface.
    assert catalog.query_routes["/api/news/market"] == ("news_market_groups", "news_market_sources")
    assert catalog.query_routes["/api/news/market/{item_id}"] == (
        "news_market_item",
        "news_market_group_timeline",
    )
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
    # #510 PR-5a: a console read the route plans two ways is certified twice, because the audit must
    # not certify a statement the route does not execute. #537 PR-5 deleted the three GET routes that
    # had the other three pairs, so the Case read is the last one with an optional predicate.
    assert catalog.query_routes["/api/trading/cases"] == (
        "trading_console_cases",
        "trading_console_cases_filtered",
        "trading_case_counts",
        "trading_case_reason_counts",
    )
    assert "/api/trading/signals" not in catalog.query_routes
    assert "/api/trading/execution/observations" not in catalog.query_routes
    # The Command path is a write now and only a write: its GET went with the other two (#537 PR-5).
    assert "/api/trading/execution/commands" not in catalog.query_routes
    # The two CLI-only ledger reads stay audited without belonging to a public route.
    assert {"trading_signal_ledger", "trading_observation_ledger"} <= {query.name for query in catalog.queries}
    assert not any(
        route.startswith(("/api/news/stories", "/api/news/brief", "/api/news/sources"))
        for route in catalog.query_routes
    )
    assert [query.name for query in catalog.queries if query.amplification_basis == "aggregate_input"] == []


def test_trading_status_asks_trading_cases_for_one_indexed_row() -> None:
    """#537 PR-5. The route's two 24 h `count(*)` scans are gone; its liveness probe is not."""

    queries = {query.name: query for query in query_audit_catalog(now_ms=123_456).queries}
    latest = queries["trading_status_latest_case"]

    assert latest.sql.strip() == "SELECT max(created_at_ms) AS latest FROM trading_cases"
    assert latest.params == ()
    assert {"trading_status_case_counts", "trading_status_signal_counts"}.isdisjoint(queries)


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

    repository.console_cases(since_ms=since_ms, underlying_key=None, states=(), limit=101)
    repository.console_cases(
        since_ms=since_ms,
        underlying_key="crypto:BTC",
        states=("SIGNAL_EMITTED", "NO_TRADE"),
        limit=101,
    )
    repository.console_operator_intents(since_ns=since_ns, action=None, limit=101)
    repository.console_operator_intents(since_ns=since_ns, action="flatten", limit=101)
    repository.signal_ledger(since_ns=since_ns, limit=101)
    repository.observation_ledger(since_ns=since_ns, limit=101)

    executed = conn.statements
    audited = [
        (queries[name].sql, queries[name].params)
        for name in (
            "trading_console_cases",
            "trading_console_cases_filtered",
            "trading_console_commands",
            "trading_console_commands_filtered",
            "trading_signal_ledger",
            "trading_observation_ledger",
        )
    ]
    assert executed == audited
    # The filtered half really is a different statement, or registering it twice proves nothing.
    assert audited[0][0] != audited[1][0]
    assert "underlying_key = %(underlying)s" in audited[1][0]
    assert "state = ANY(%(states)s)" in audited[1][0]
    # #537 PR-5: no keyset predicate anywhere. `/api/trading/cases` published a `next_cursor` no
    # reader ever sent back, and the three routes whose cursors were followed are gone.
    assert all("before_ms" not in sql and "before_ns" not in sql for sql, _ in audited)


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
    """No public read expands a star over a base relation, where the projection is whatever the schema holds.

    A star over a *derived* table is a different statement and not this risk: `SELECT * FROM (SELECT a,
    b ...) AS x` names every column it returns, in the same statement. #553's market reads use one to
    union three fact tables into a single observation shape without repeating thirty columns in four
    places. What must stay impossible is `SELECT *` over a table, where a migration adds a column and
    a public payload grows one nobody read.
    """

    catalog = query_audit_catalog(now_ms=0)
    public_names = {name for names in catalog.query_routes.values() for name in names}

    assert [
        query.name for query in catalog.queries if query.name in public_names and _base_relation_star_sources(query.sql)
    ] == []
    # Not vacuous: the same resolver names the base relation behind either spelling of the real risk.
    assert _base_relation_star_sources("SELECT * FROM news_items WHERE item_id = %s") == ["news_items"]
    assert _base_relation_star_sources("SELECT i.* FROM news_items i JOIN news_oi_signals o ON true") == ["news_items"]
    assert _base_relation_star_sources("SELECT o.oi_change_bps, i.* FROM news_items i") == ["news_items"]


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


# A qualified star is matched wherever it sits, not only first in the select list: `SELECT a, t.*`
# expands `t` just as completely as `SELECT t.*` does. A bare star is anchored to `SELECT` so that
# `count(*)` is not mistaken for a projection.
_STAR_PROJECTION = re.compile(
    r"\bSELECT\s+(?P<bare>\*)|\b(?P<qualifier>[A-Za-z_][A-Za-z0-9_]*)\.\*",
    re.IGNORECASE,
)
_CTE_DEFINITION = re.compile(
    r"(?:\bWITH\b|,)\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+AS\s+(?:MATERIALIZED\s+)?\(",
    re.IGNORECASE,
)
_RELATION_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?P<relation>[A-Za-z_][A-Za-z0-9_]*)(?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_][A-Za-z0-9_]*))?",
    re.IGNORECASE,
)
_RELATION_TAIL = re.compile(r"\bFROM\s*(?P<source>\(|[A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_SUBQUERY_ALIAS = re.compile(r"\s*(?:AS\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
# Words that can follow a relation name where an alias would sit, and are not one.
_NOT_AN_ALIAS = frozenset(
    {
        "as",
        "cross",
        "full",
        "group",
        "having",
        "inner",
        "join",
        "left",
        "limit",
        "offset",
        "on",
        "order",
        "right",
        "union",
        "using",
        "where",
        "window",
    }
)


def _base_relation_star_sources(sql: str) -> list[str]:
    """Every relation a star expands that the statement does not itself define, in order.

    The resolution model is the one a reader applies: a qualified star names the relation bound to that
    alias by its own SELECT's FROM clause, which follows it; an unqualified star expands whatever that
    FROM clause names. A name the statement defines -- a CTE, or a parenthesised subquery with an alias
    -- has its columns spelled out in the same statement, so a star over it is fully named.
    """

    defined = _statement_defined_names(sql)
    sources: list[str] = []
    for star in _STAR_PROJECTION.finditer(sql):
        source = _star_source(sql, star, star.group("qualifier"))
        if source is not None and source.lower() not in defined:
            sources.append(source)
    return sources


def _statement_defined_names(sql: str) -> set[str]:
    names = {match.group("name").lower() for match in _CTE_DEFINITION.finditer(sql)}
    for opened in re.finditer(r"\b(?:FROM|JOIN)\s*\(", sql, re.IGNORECASE):
        depth, index = 1, opened.end()
        while depth and index < len(sql):
            depth += (sql[index] == "(") - (sql[index] == ")")
            index += 1
        alias = _SUBQUERY_ALIAS.match(sql, index)
        # `FROM (...) WHERE` is a subquery with no alias, and `where` is not one of its names.
        if alias is not None and alias.group("name").lower() not in _NOT_AN_ALIAS:
            names.add(alias.group("name").lower())
    return names


def _star_source(sql: str, star: re.Match[str], qualifier: str | None) -> str | None:
    if qualifier is None:
        tail = _RELATION_TAIL.search(sql, star.end())
        # A parenthesised subquery is written out where the star reads it; nothing is hidden.
        return None if tail is None or tail.group("source") == "(" else tail.group("source")
    for reference in _RELATION_REFERENCE.finditer(sql, star.end()):
        alias = reference.group("alias")
        if alias and alias.lower() not in _NOT_AN_ALIAS and alias.lower() == qualifier.lower():
            return reference.group("relation")
    return qualifier


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
