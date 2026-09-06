from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tracefold.app.query_audit import PUBLIC_ROUTE_QUERY_COVERAGE, query_audit_catalog
from tracefold.news.market_review import instrument_storage, quote_storage
from tracefold.news.market_review.quote_storage import QuoteStorage
from tracefold.news.storage import events, feed_sql
from tracefold.news.storage.events import EventStorage
from tracefold.news.storage.feed import FeedStorage
from tracefold.news.storage.root import NewsRepository
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
    "news_band_lookup",
    "news_status_ingest",
    "news_status_incidents_open",
    "news_status_recovery_backlog",
    # #570 A2: the statements a status request executes, not the two counts that stood for them.
    "news_status_pipeline",
    "news_status_source_contracts",
    "news_status_delivery",
    "news_status_funnel_suppressed",
    "news_status_funnel_verdicts",
    "news_status_funnel_reviews",
    "news_status_funnel_totals",
    "news_status_learning_retention",
    "news_quote_snapshot_read",
    # #553: three statements per market list request and four per detail request. The timeline, the
    # card that spoke for an observation and the observations that card covered are each their own
    # bounded read rather than a wider list scan, so every one of them is named.
    "news_market_groups",
    "news_market_sources",
    "news_market_delivery_summary",
    "news_market_item",
    "news_market_item_delivery",
    "news_market_item_covered",
    "news_market_group_timeline",
    # PR-2's notification take. It serves no route, but it runs every two seconds for the life of the
    # process, which makes it the market read most able to grow without anyone noticing.
    "news_market_notify_backlog",
    # #582 PR-2. The OI card's two News reads: they serve no route either, and they run inside the
    # send lane's own 1.5 s budget, which is what makes an unplanned scan visible to a reader.
    "news_market_news_pushed",
    "news_market_news_total",
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
        queries=(
            ReadQuerySpec(
                name="owned_read", sql="SELECT 1", max_read_return_amplification=20.0, max_scanned_rows=1_000
            ),
        ),
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
            ReadQuerySpec(name=name, sql="SELECT 1", max_read_return_amplification=20.0, max_scanned_rows=1_000)
            for name in _NEWS_QUERY_NAMES
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
    assert catalog.query_routes["/api/news/market"] == (
        "news_market_groups",
        "news_market_sources",
        "news_market_delivery_summary",
    )
    assert catalog.query_routes["/api/news/market/{item_id}"] == (
        "news_market_item",
        "news_market_item_delivery",
        "news_market_item_covered",
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
        "news_status_pipeline",
        "news_status_source_contracts",
        "news_status_delivery",
        "news_status_funnel_suppressed",
        "news_status_funnel_verdicts",
        "news_status_funnel_reviews",
        "news_status_funnel_totals",
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
    # #589 PR-2. The admission ledger's two routes are gone and `tracefold trading gate` is what runs
    # their statements now. A statement stops being audited when nothing executes it, not when a route
    # is deleted, so both stay here beside the other two CLI-only ledger reads -- and the grouped
    # 24 h distribution, which had no caller left at all, does not.
    assert not any(route.startswith("/api/trading/gate") for route in catalog.query_routes)
    query_names = {query.name for query in catalog.queries}
    assert {
        "trading_signal_ledger",
        "trading_observation_ledger",
        "trading_gate_decisions_since",
        "trading_gate_decision_for_source_key",
    } <= query_names
    assert "trading_gate_decision_counts" not in query_names
    assert not any(
        route.startswith(("/api/news/stories", "/api/news/brief", "/api/news/sources"))
        for route in catalog.query_routes
    )


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


def test_status_audit_explains_the_statements_the_status_route_executes():
    """#570 A2: `/api/news/status` is audited by driving the production status read, not a sketch of it.

    Two registered specs used to stand for the whole page: a `count(news_verdicts)` and a
    `count(news_deliveries)` that no route runs. The statements it does run -- the correlated
    latest-Evidence subquery, the percentile aggregates, the four funnel passes -- were planned by
    nobody, so a green `db query-audit --analyze` said nothing about the slowest read on the surface.

    Running `status_snapshot` against a recording connection is what makes that unrepeatable: the SQL and
    the bound parameters both have to match, so a copy that drifts in either fails here, and a statement
    added to `status_snapshot` without a spec fails on the count. The claim stops at `status_snapshot`:
    `get_news_status` also runs `universe_summary`, `asset_usage_24h`, `price_status` and `asset_refs`,
    and this test says nothing about those.
    """

    now_ms = 123_456
    queries = {query.name: query for query in query_audit_catalog(now_ms=now_ms).queries}
    conn = RecordingStatementConn()

    NewsRepository(conn).status_snapshot(now_ms=now_ms)

    audited = [
        (queries[name].sql, tuple(queries[name].params))
        for name in PUBLIC_ROUTE_QUERY_COVERAGE["/api/news/status"]
        if name != "workers_runtime"
    ]
    executed = [(sql, () if params is None else tuple(params)) for sql, params in conn.statements]
    assert sorted(executed, key=repr) == sorted(audited, key=repr)
    # Not vacuous: the pipeline read really is the whole status statement, not a count of one table.
    pipeline = queries["news_status_pipeline"].sql
    assert "percentile_cont(0.95)" in pipeline
    assert "news_event_evidence_snapshots" in pipeline
    assert queries["news_status_funnel_totals"].sql.count("news_event_evidence_snapshots") == 2


def test_the_oi_cards_news_read_is_audited_as_the_statements_the_port_executes():
    """#582 §3.3: the registered pair is the storage module's own SQL, with its own bound values.

    Driving `pushed_news_for_symbol` against a recording connection is what keeps the audit honest.
    A registered look-alike would plan a statement the send lane never runs -- and this read runs
    inside the card's 1.5 s budget, so a plan nobody checked is a card nobody gets.
    """

    now_ms = 123_456
    queries = {query.name: query for query in query_audit_catalog(now_ms=now_ms).queries}
    conn = RecordingStatementConn()

    NewsRepository(conn).pushed_news_for_symbol("BTC", now_ms=now_ms)

    assert conn.statements == [
        (queries[name].sql, tuple(queries[name].params))
        for name in ("news_market_news_pushed", "news_market_news_total")
    ]
    # Not vacuous: both statements are bounded, and the pair really is two different reads.
    pushed, total = queries["news_market_news_pushed"], queries["news_market_news_total"]
    assert "LIMIT %s" in pushed.sql and "d.settled_at_ms >= %s" in pushed.sql
    assert "ea.opened_at_ms >= %s" in total.sql and "count(DISTINCT ea.event_id)" in total.sql
    assert pushed.sql != total.sql
    # And both resolve the symbol through the alias table rather than matching it literally.
    assert all("equivalent_symbols" in query.sql for query in (pushed, total))


def test_status_audit_reads_its_sql_from_the_production_module_only():
    """The status route may not hold SQL of its own; renaming the shared constant breaks this test.

    Byte-identity above proves the two agree today. This proves they cannot disagree tomorrow: every
    statement the status read executes is a module constant it imports, so there is one place the SQL
    comes from and an edit there moves the route and its audit together.
    """

    module = ast.parse(Path(inspect.getfile(FeedStorage)).read_text(encoding="utf-8"))
    storage = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "FeedStorage")
    status_methods = [
        node
        for node in storage.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"status_snapshot", "_funnel_24h", "_source_contracts_24h"}
    ]
    referenced = {
        call.args[0].id if isinstance(call.args[0], ast.Name) else None
        for method in status_methods
        for call in ast.walk(method)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "execute"
    }

    assert len(status_methods) == 3
    assert None not in referenced, "a status statement is written inline instead of imported"
    assert referenced == {
        "STATUS_INGEST_SQL",
        "STATUS_PIPELINE_SQL",
        "STATUS_SOURCE_CONTRACTS_SQL",
        "STATUS_DELIVERY_SQL",
        "STATUS_FUNNEL_SUPPRESSED_SQL",
        "STATUS_FUNNEL_VERDICTS_SQL",
        "STATUS_FUNNEL_REVIEWS_SQL",
        "STATUS_FUNNEL_TOTALS_SQL",
        "STATUS_LEARNING_RETENTION_SQL",
    }
    assert all(getattr(feed_sql, name) for name in referenced)


def _executed_constants(owner: type, method_name: str) -> set[str | None]:
    """The names one method hands to `.execute(...)`; `None` stands for an inline literal."""

    module = ast.parse(Path(inspect.getfile(owner)).read_text(encoding="utf-8"))
    owner_node = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == owner.__name__)
    method = next(node for node in owner_node.body if isinstance(node, ast.FunctionDef) and node.name == method_name)
    return {
        call.args[0].id if isinstance(call.args[0], ast.Name) else None
        for call in ast.walk(method)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "execute"
    }


def test_news_audit_plans_the_statements_the_deduper_reaction_and_detail_reads_execute():
    """#589 L-F1. Five audited News reads carried a hand-written restatement of a production query.

    The copies had drifted, which is the whole defect: the member spec named three columns where the
    Event-detail route returns twelve, and the band spec was the bare band lookup without the Event join
    or the evidence-provenance filter the Deduper's candidate read plans. A copy that drifts certifies a
    plan production never runs, while the plan it does run goes uncertified. Every one of these is now
    the module constant its own read executes, so an edit moves the read and its audit together.
    """

    audited = {query.name: query.sql for query in query_audit_catalog(now_ms=0).queries}

    assert audited["news_search_identity"] == instrument_storage.SEARCH_IDENTITY_SQL
    assert audited["news_search_event_symbols"] == instrument_storage.SEARCH_EVENT_SYMBOLS_SQL
    assert audited["news_event_members"] == feed_sql.EVENT_MEMBERS_SQL
    assert audited["news_band_lookup"] == events.BAND_CANDIDATES_SQL
    assert audited["news_reaction_due_scan"] == quote_storage.DUE_REACTIONS_SQL
    # Not vacuous about the drift that motivated this: the audited member read returns the whole card.
    assert "i.canonical_url" in audited["news_event_members"]
    assert "news_event_evidence_snapshots" in audited["news_band_lookup"]

    # And the constants are the ones the production methods execute, by AST rather than by text.
    assert "EVENT_MEMBERS_SQL" in _executed_constants(FeedStorage, "event_detail")
    assert _executed_constants(EventStorage, "find_band_candidates") == {"BAND_CANDIDATES_SQL"}
    assert _executed_constants(QuoteStorage, "due_reactions") == {"DUE_REACTIONS_SQL"}


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
    """#570 A1: the sequential scan is large because of what it read, not what the planner expected out.

    This plan used to be judged on `Plan Rows` 50,000 -- a planner *output* estimate -- and on 500 rows
    read. It is now judged on the 50,000 rows the node actually passed through its filter, which is the
    same number the counterexample in #570 had the audit report as zero.
    """

    conn = RecordingJsonPlanConn(
        {
            "Plan": {
                "Node Type": "Sort",
                "Actual Rows": 500,
                "Actual Loops": 1,
                "Temp Read Blocks": 2,
                "Temp Written Blocks": 3,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "events",
                        "Plan Rows": 250,
                        "Actual Rows": 500,
                        "Rows Removed by Filter": 49_500,
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
            ReadQuerySpec(
                name="bounded_read", sql="SELECT 1", max_read_return_amplification=20.0, max_scanned_rows=1_000_000
            )
        ),
    ).run(analyze=True)
    metrics = payload["queries"][0]["metrics"]

    assert payload["ok"] is False
    assert set(payload["queries"][0]["violations"]) == {
        "unexpected_large_table_seq_scan",
        "temp_spill",
        "read_return_amplification_exceeded",
    }
    assert metrics["scanned_rows"] == 50_000
    assert metrics["scan_output_rows"] == 500
    assert metrics["discarded_rows"] == 49_500
    assert metrics["read_return_amplification"] == 100.0
    assert metrics["large_seq_scans"] == [
        {
            "relation": "events",
            "scanned_rows_per_loop": 50_000,
            "scanned_rows": 50_000,
            "returned_rows": 500,
            "discarded_rows": 49_500,
            "loops": 1,
        }
    ]


def test_analyzed_query_audit_multiplies_per_loop_rows_and_filters_by_the_loop_count():
    """A repeated inner scan reads its rows once per loop, and discards them once per loop too.

    The total is work and is reported as work. It is not evidence of a large table: a nested loop that
    scans a 20-row table a thousand times never read a large table, and calling that
    `unexpected_large_table_seq_scan` would fail an ordinary plan. The size claim is measured on one pass.
    """

    conn = RecordingJsonPlanConn(
        {
            "Plan": {
                "Node Type": "Nested Loop",
                "Actual Rows": 12,
                "Actual Loops": 1,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "events",
                        "Actual Rows": 2,
                        "Rows Removed by Filter": 18,
                        "Actual Loops": 1_000,
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
            ReadQuerySpec(
                name="looped_read", sql="SELECT 1", max_read_return_amplification=20.0, max_scanned_rows=1_000_000
            )
        ),
    ).run(analyze=True)
    metrics = payload["queries"][0]["metrics"]

    assert metrics["scan_output_rows"] == 2_000
    assert metrics["scanned_rows"] == 20_000
    assert metrics["discarded_rows"] == 18_000
    assert metrics["large_seq_scans"] == []
    assert payload["queries"][0]["violations"] == ["read_return_amplification_exceeded"]


def test_analyzed_query_audit_counts_bitmap_heap_rows_and_its_rechecks_not_index_candidates():
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
                        "Rows Removed by Index Recheck": 34,
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
            ReadQuerySpec(
                name="bitmap_read", sql="SELECT 1", max_read_return_amplification=20.0, max_scanned_rows=1_000
            )
        ),
    ).run(analyze=True)
    metrics = payload["queries"][0]["metrics"]

    # The index candidates a bitmap never fetches are still not rows read from the table; the heap rows
    # the recheck threw away are.
    assert payload["ok"] is True
    assert metrics["scan_output_rows"] == 116
    assert metrics["scanned_rows"] == 150
    assert metrics["read_return_amplification"] == 3.0


def test_analyzed_query_audit_lets_the_plan_name_the_amplification_denominator():
    """#570 A1: a bounded scalar aggregate is not a query that returned too few rows.

    Asking `count(*)` over 500 bounded rows to return 500 rows is a threshold no correct aggregate can
    meet, and the old catalog answered that with a two-name allow-list. The plan already says which
    statements fold: an aggregate with no `Group Key` returns one row whatever it reads, so its own
    input is its denominator. A grouped aggregate keeps the rows it returns, because that result grows
    with the data it read.
    """

    conn = RecordingJsonPlanConn(_scalar_aggregate_plan(input_rows=500, returned_rows=1))
    grouped = RecordingJsonPlanConn(_grouped_aggregate_plan(input_rows=500, returned_rows=4))
    catalog = _single_query_catalog(
        ReadQuerySpec(
            name="bounded_aggregate", sql="SELECT 1", max_read_return_amplification=20.0, max_scanned_rows=1_000
        )
    )

    folded = PostgresQueryAudit(conn, catalog=catalog).run(analyze=True)["queries"][0]
    by_group = PostgresQueryAudit(grouped, catalog=catalog).run(analyze=True)["queries"][0]

    assert folded["ok"] is True
    assert folded["metrics"]["amplification_basis"] == "aggregate_input"
    assert folded["metrics"]["amplification_basis_rows"] == 500
    assert folded["metrics"]["read_return_amplification"] == 1.0
    assert by_group["ok"] is False
    assert by_group["metrics"]["amplification_basis"] == "returned_rows"
    assert by_group["metrics"]["amplification_basis_rows"] == 4
    assert by_group["metrics"]["read_return_amplification"] == 125.0


def test_analyzed_query_audit_keeps_the_returned_rows_denominator_for_a_paged_read():
    """#570 A1: a page that returns many rows did not fold, whatever its subquery counted.

    `news_market_groups` carries `(SELECT count(*) FROM observations) AS scanned` beside a page of
    collapsed groups, and every review-desk queue read has the same shape. Taking that subquery's input
    as the denominator divides the window by itself and reports a bounded read: this plan scans 500,000
    rows to return 200 and would come back as amplification 1.0004 with no violation. The rows a
    statement returned stay its denominator unless the statement itself folded to one row.
    """

    conn = RecordingJsonPlanConn(_page_with_scalar_subplan(page_rows=200, subplan_rows=500_000))

    payload = PostgresQueryAudit(
        conn,
        catalog=_single_query_catalog(
            ReadQuerySpec(
                name="paged_read",
                sql="SELECT 1",
                max_read_return_amplification=100.0,
                max_scanned_rows=1_000_000,
            )
        ),
    ).run(analyze=True)
    metrics = payload["queries"][0]["metrics"]

    assert metrics["returned_rows"] == 200
    assert metrics["scanned_rows"] == 500_200
    assert metrics["amplification_basis"] == "returned_rows"
    assert metrics["amplification_basis_rows"] == 200
    assert metrics["read_return_amplification"] == 2501.0
    assert payload["queries"][0]["violations"] == ["read_return_amplification_exceeded"]


def test_analyzed_query_audit_keeps_the_returned_rows_denominator_for_an_empty_page():
    """#570 A1: a page that matched nothing folded nothing, and it is the most amplified read there is.

    An ungrouped aggregate emits exactly one row, so a result of none did not come from one. Reading zero
    as "at most one row, so it folded" made the filter that discards a whole window to return nothing --
    `news_market_groups` on a group key no observation carries, which is #570 A3's shape, and
    `news_review_market` through its subquery -- report a bounded read: 25,000 rows discarded beside a
    `count(*)` over 500,000 came back as amplification 1.05 with no violation.
    """

    conn = RecordingJsonPlanConn(_empty_page_with_scalar_subplan(discarded_rows=25_000, subplan_rows=500_000))

    payload = PostgresQueryAudit(
        conn,
        catalog=_single_query_catalog(
            ReadQuerySpec(
                name="empty_page",
                sql="SELECT 1",
                max_read_return_amplification=100.0,
                max_scanned_rows=1_000_000,
            )
        ),
    ).run(analyze=True)
    metrics = payload["queries"][0]["metrics"]

    assert metrics["returned_rows"] == 0
    assert metrics["scan_output_rows"] == 500_000
    assert metrics["scanned_rows"] == 525_000
    assert metrics["amplification_basis"] == "returned_rows"
    assert metrics["amplification_basis_rows"] == 0
    # Nothing came back, so every row read was read for nothing: the denominator floors at one row.
    assert metrics["read_return_amplification"] == 525_000.0
    assert payload["queries"][0]["violations"] == ["read_return_amplification_exceeded"]


def test_analyzed_query_audit_bounds_a_folded_read_by_its_own_scanned_rows_budget():
    """#570 A1: the fold is not an exemption, and the sequential-scan rule does not cover for it.

    A `count(*)` over the whole history returns one row and reads everything, so its amplification is 1
    by construction and nothing about the denominator can catch it. When that read arrives by an index
    path there is no sequential scan either, and before this budget the audit reported no violation at
    all. The rows the spec declared it may touch are what closes it.
    """

    conn = RecordingJsonPlanConn(_index_aggregate_plan(scanned_rows=1_000_000))
    metered = ReadQuerySpec(
        name="unbounded_aggregate",
        sql="SELECT 1",
        max_read_return_amplification=20.0,
        max_scanned_rows=100_000,
    )

    payload = PostgresQueryAudit(conn, catalog=_single_query_catalog(metered)).run(analyze=True)
    audited = payload["queries"][0]

    assert audited["metrics"]["scanned_rows"] == 1_000_000
    assert audited["metrics"]["amplification_basis"] == "aggregate_input"
    assert audited["metrics"]["read_return_amplification"] == 1.0
    # Neither of the other two rules sees it: no sequential scan, and amplification is 1 by construction.
    assert audited["metrics"]["large_seq_scans"] == []
    assert audited["violations"] == ["scanned_rows_budget_exceeded"]


def test_analyzed_query_audit_keeps_a_bounded_one_row_aggregate_inside_its_budget():
    """The same shape within its declared bound stays green, so the budget is a bound and not a ban."""

    conn = RecordingJsonPlanConn(_index_aggregate_plan(scanned_rows=500))

    payload = PostgresQueryAudit(
        conn,
        catalog=_single_query_catalog(
            ReadQuerySpec(
                name="bounded_one_row_aggregate",
                sql="SELECT 1",
                max_read_return_amplification=20.0,
                max_scanned_rows=100_000,
            )
        ),
    ).run(analyze=True)
    metrics = payload["queries"][0]["metrics"]

    assert metrics["returned_rows"] == 1
    assert metrics["amplification_basis"] == "aggregate_input"
    assert metrics["amplification_basis_rows"] == 500
    assert payload["queries"][0]["violations"] == []


def test_each_query_owns_its_amplification_budget():
    conn = RecordingJsonPlanConn(_filtered_scan_plan(scanned_rows=10, returned_rows=1))
    query = ReadQuerySpec(
        name="tight_read",
        sql="SELECT 1",
        max_read_return_amplification=5.0,
        max_scanned_rows=1_000,
    )

    payload = PostgresQueryAudit(conn, catalog=_single_query_catalog(query)).run(analyze=True)

    audited = payload["queries"][0]
    assert audited["budget"] == {"max_read_return_amplification": 5.0, "max_scanned_rows": 1_000}
    assert audited["violations"] == ["read_return_amplification_exceeded"]


def test_catalog_rejects_a_query_without_its_own_amplification_budget():
    with pytest.raises(ValueError, match="amplification budget missing: unbudgeted"):
        _single_query_catalog(ReadQuerySpec(name="unbudgeted", sql="SELECT 1", max_scanned_rows=1_000))


def test_catalog_rejects_a_query_without_its_own_scanned_rows_budget():
    """#570 A1: a read that declares no ceiling on the rows it may touch cannot be audited.

    Amplification alone cannot bound a folding read -- what a `count(*)` scanned divided by what it
    folded is 1 whatever it scanned -- so the fold would be an exemption for any spec allowed to omit
    this. Composition refuses instead.
    """

    with pytest.raises(ValueError, match="scanned-rows budget missing: unmetered"):
        _single_query_catalog(ReadQuerySpec(name="unmetered", sql="SELECT 1", max_read_return_amplification=20.0))


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

    def fetchone(self):
        return None


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


def _scalar_aggregate_plan(*, input_rows: int, returned_rows: int) -> dict:
    return _aggregate_plan(input_rows=input_rows, returned_rows=returned_rows)


def _grouped_aggregate_plan(*, input_rows: int, returned_rows: int) -> dict:
    plan = _aggregate_plan(input_rows=input_rows, returned_rows=returned_rows)
    plan["Plan"]["Group Key"] = ["events.event_kind"]
    return plan


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


def _page_with_scalar_subplan(*, page_rows: int, subplan_rows: int) -> dict:
    """A paged read whose target list carries one scalar aggregate over a much wider relation."""

    return {
        "Plan": {
            "Node Type": "Limit",
            "Actual Rows": page_rows,
            "Actual Loops": 1,
            "Plans": [
                {
                    "Node Type": "Index Only Scan",
                    "Relation Name": "page",
                    "Index Name": "idx_page_id",
                    "Actual Rows": page_rows,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Aggregate",
                    "Parent Relationship": "InitPlan",
                    "Subplan Name": "InitPlan 1",
                    "Actual Rows": 1,
                    "Actual Loops": 1,
                    "Plans": [
                        # An index path, so the denominator is the only rule under test here.
                        {
                            "Node Type": "Index Only Scan",
                            "Relation Name": "observations",
                            "Index Name": "idx_observations_id",
                            "Actual Rows": subplan_rows,
                            "Actual Loops": 1,
                        }
                    ],
                },
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 2.0,
    }


def _empty_page_with_scalar_subplan(*, discarded_rows: int, subplan_rows: int) -> dict:
    """The same page shape, filtered down to nothing: the whole window read and no row returned."""

    plan = _page_with_scalar_subplan(page_rows=0, subplan_rows=subplan_rows)
    plan["Plan"]["Plans"][0] = {
        "Node Type": "Index Scan",
        "Relation Name": "page",
        "Index Name": "idx_page_id",
        "Actual Rows": 0,
        "Rows Removed by Filter": discarded_rows,
        "Actual Loops": 1,
    }
    return plan


def _index_aggregate_plan(*, scanned_rows: int) -> dict:
    """One scalar aggregate reached by an index path: one row out, `scanned_rows` in, no Seq Scan."""

    return {
        "Plan": {
            "Node Type": "Aggregate",
            "Actual Rows": 1,
            "Actual Loops": 1,
            "Plans": [
                {
                    "Node Type": "Index Only Scan",
                    "Relation Name": "events",
                    "Index Name": "idx_events_id",
                    "Actual Rows": scanned_rows,
                    "Actual Loops": 1,
                }
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 2.0,
    }


def _filtered_scan_plan(*, scanned_rows: int, returned_rows: int) -> dict:
    """One indexed scan that reads `scanned_rows` and hands `returned_rows` upward."""

    return {
        "Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "events",
            "Index Name": "idx_events_opened",
            "Actual Rows": returned_rows,
            "Rows Removed by Filter": scanned_rows - returned_rows,
            "Actual Loops": 1,
        },
        "Planning Time": 0.5,
        "Execution Time": 2.0,
    }
