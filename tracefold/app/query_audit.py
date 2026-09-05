from __future__ import annotations

import time
from typing import Protocol

from tracefold.platform.postgres.audit import (
    PostgresQueryAudit,
    QueryAuditCatalog,
    ReadQuerySpec,
    postgres_query_specs,
)
from tracefold.trading.storage.execution_stream import execution_stream_query_specs
from tracefold.trading.storage.gate import (
    GATE_DECISION_COUNTS_SQL,
    GATE_DECISION_FOR_SOURCE_KEY_SQL,
    GATE_DECISIONS_SINCE_SQL,
)
from tracefold.trading.storage.lane import LATEST_CASE_CREATED_AT_SQL
from tracefold.trading.storage.queries import (
    TRADING_CASE_COUNTS_SQL,
    TRADING_CASE_REASON_COUNTS_SQL,
    console_cases_statement,
    console_executions_statement,
    console_operator_intents_statement,
    observation_ledger_statement,
    signal_ledger_statement,
)

from .workers.runtime import workers_runtime_read_query


class NewsQuerySpecsProvider(Protocol):
    def __call__(self, *, now_ms: int) -> tuple[ReadQuerySpec, ...]: ...


PUBLIC_ROUTE_QUERY_COVERAGE: dict[str, tuple[str, ...]] = {
    "/readyz": ("readiness_schema",),
    "/api/status": ("readiness_schema", "workers_runtime"),
    "/api/news/feed": (
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
    ),
    "/api/news/quotes": ("news_quote_snapshot_read",),
    # #553. Three statements per list request -- the collapsed page, the per-kind intake summary and
    # the per-kind receipt summary beside it -- and four per detail request, because the card that
    # spoke for an observation and the observations it covered are each their own bounded read.
    "/api/news/market": ("news_market_groups", "news_market_sources", "news_market_delivery_summary"),
    "/api/news/market/{item_id}": (
        "news_market_item",
        "news_market_item_delivery",
        "news_market_item_covered",
        "news_market_group_timeline",
    ),
    # Three reads per request, and all three are named: `is_tradeable` runs its own statement and a
    # manifest that omitted it would let `db query-audit --analyze` report full coverage of a public route
    # while never planning one of its queries.
    "/api/news/symbols/{base}": ("news_symbol_contracts", "news_symbol_tradeable", "news_symbol_aliases"),
    "/api/news/events/{event_id}": (
        "news_event_detail",
        "news_event_members",
        "news_event_verdicts",
        "news_event_asset_projection",
        "news_reaction_attach",
    ),
    # #570 A2. The eleven statements `FeedStorage.status_snapshot` executes, each named as the constant
    # the production read executes, plus the Workers row the route folds in beside them. Two of these
    # names used to stand for the whole page and neither was a statement the route runs: a route name is
    # not SQL coverage. The instrument, asset-usage and price reads the route composes after the snapshot
    # are still unregistered; #570 A2 names them and they are not in this change.
    "/api/news/status": (
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
    ),
    # One statement over `trading_cases`, where the two 24 h `count(*)` scans this route also ran on
    # every 15 s poll were rendered nowhere the desk still has (#537 PR-5).
    "/api/trading/status": ("trading_status_latest_case",),
    # The Case read is registered twice, because the route plans two statements: the first page with
    # no filter, and the filtered page a reader gets once they narrow. Certifying only one of them
    # certifies a plan the route does not always execute.
    "/api/trading/cases": (
        "trading_console_cases",
        "trading_console_cases_filtered",
        "trading_case_counts",
        "trading_case_reason_counts",
    ),
    # #528 PR-1. The desk table plans two statements: its own per-entry fold, and the unfiltered
    # window of the Command ledger it renders beside it.
    "/api/trading/executions": (
        "trading_console_executions",
        "trading_console_commands",
    ),
    "/api/trading/gate/{event_id}": ("trading_gate_decision_for_source_key",),
    # #269. The same admission ledger the event endpoint reads one row of, for a whole window — bounded
    # by 24 h and a hard row limit — plus the one grouped pass that answers both 24 h distributions.
    "/api/trading/gate": ("trading_gate_decisions_since", "trading_gate_decision_counts"),
}

PUBLIC_NO_SQL_ROUTES = frozenset(
    {
        "/healthz",
        "/metrics",
        "/api/bootstrap",
    }
)

# #475 PR-E adds exactly one bounded append to the existing Command aggregate. The path is also a GET;
# recording it in both sets keeps the read plan and the write authority independently explicit.
PUBLIC_WRITE_ROUTES: frozenset[str] = frozenset({"/api/trading/execution/commands"})


def query_audit_catalog(
    *,
    now_ms: int,
    news_query_specs: NewsQuerySpecsProvider | None = None,
) -> QueryAuditCatalog:
    provider = news_query_specs or _default_news_query_specs
    queries = (
        *postgres_query_specs(now_ms=int(now_ms)),
        workers_runtime_read_query(),
        *provider(now_ms=int(now_ms)),
        *execution_stream_query_specs(),
        *_trading_query_specs(now_ms=int(now_ms)),
    )
    return QueryAuditCatalog(
        queries=queries,
        query_routes=dict(PUBLIC_ROUTE_QUERY_COVERAGE),
        no_sql_routes=PUBLIC_NO_SQL_ROUTES,
        write_routes=PUBLIC_WRITE_ROUTES,
    )


def query_audit_for_connection(
    conn: object,
    *,
    now_ms: int | None = None,
    news_query_specs: NewsQuerySpecsProvider | None = None,
) -> PostgresQueryAudit:
    resolved_now_ms = int(now_ms if now_ms is not None else time.time() * 1_000)
    return PostgresQueryAudit(
        conn,
        catalog=query_audit_catalog(
            now_ms=resolved_now_ms,
            news_query_specs=news_query_specs,
        ),
    )


def _default_news_query_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
    from tracefold.news.storage.query_specs import news_query_specs

    return news_query_specs(now_ms=now_ms)


def _trading_query_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
    """The bounded Signal reads, built by the production statement builders.

    Every console spec below calls the same builder `QueryStorage` runs, once with no optional
    predicate and once with all of them, so both plans the route can execute are certified and neither
    can drift away from an audited copy.

    The two ledger reads at the end belong to no public route: `tracefold trading signals` and
    `tracefold trading observations` are their only callers since #537 PR-5 deleted the `GET` routes
    that were. They stay audited because they still run against production data.
    """

    since_ms = int(now_ms) - 24 * 3_600_000
    since_ns = since_ms * 1_000_000
    executions_sql, executions_params = console_executions_statement(since_ns=since_ns, limit=101)
    return (
        ReadQuerySpec(
            # The Decision Plane's liveness: one index-only probe of the newest Case, and the whole
            # of what `GET /api/trading/status` still asks `trading_cases` (#537 PR-5).
            name="trading_status_latest_case",
            sql=LATEST_CASE_CREATED_AT_SQL,
            params=(),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="trading_gate_decision_for_source_key",
            sql=GATE_DECISION_FOR_SOURCE_KEY_SQL,
            params=("oi:not-a-real-event:oi_signal_v1",),
            max_read_return_amplification=4.0,
        ),
        ReadQuerySpec(
            # #269. One admission answer per source in the window, newest frame first. One row per
            # frame is the table's own primary key now (#537 PR-3), so the shipped read is the index
            # scan this certifies rather than a materialised `DISTINCT ON` set re-sorted by the outer
            # query — the table a reader scrolls and the distribution above it cannot disagree.
            name="trading_gate_decisions_since",
            sql=GATE_DECISIONS_SINCE_SQL,
            params=("oi", since_ms, 401),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            # Both 24 h distributions from one grouped pass over the same window (#537 PR-5).
            name="trading_gate_decision_counts",
            sql=GATE_DECISION_COUNTS_SQL,
            params=("oi", since_ms),
            max_read_return_amplification=20.0,
        ),
        *_console_specs(
            name="trading_console_cases",
            unfiltered=console_cases_statement(since_ms=since_ms, limit=101),
            filtered=console_cases_statement(
                since_ms=since_ms,
                underlying_key="crypto:BTC",
                states=("SIGNAL_EMITTED", "NO_TRADE"),
                limit=101,
            ),
        ),
        ReadQuerySpec(
            name="trading_case_counts",
            sql=TRADING_CASE_COUNTS_SQL,
            params=(since_ms,),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="trading_case_reason_counts",
            sql=TRADING_CASE_REASON_COUNTS_SQL,
            params=(since_ms,),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            # #528 PR-1. One plan, not two: the desk table takes no filter. The fold reads every
            # observation of the Signals in its own window, so its input is the join rather than the
            # row per Signal it returns.
            name="trading_console_executions",
            sql=executions_sql,
            params=executions_params,
            max_read_return_amplification=20.0,
        ),
        *_console_specs(
            name="trading_console_commands",
            unfiltered=console_operator_intents_statement(since_ns=since_ns, limit=101),
            filtered=console_operator_intents_statement(since_ns=since_ns, action="flatten", limit=101),
        ),
        *(
            ReadQuerySpec(name=name, sql=sql, params=params, max_read_return_amplification=20.0)
            for name, (sql, params) in (
                ("trading_signal_ledger", signal_ledger_statement(since_ns=since_ns, limit=101)),
                ("trading_observation_ledger", observation_ledger_statement(since_ns=since_ns, limit=101)),
            )
        ),
    )


def _console_specs(
    *,
    name: str,
    unfiltered: tuple[str, dict[str, object]],
    filtered: tuple[str, dict[str, object]],
) -> tuple[ReadQuerySpec, ...]:
    """One console read's two plans: the first page, and the narrowed page after it."""

    return tuple(
        ReadQuerySpec(name=spec_name, sql=sql, params=params, max_read_return_amplification=20.0)
        for spec_name, (sql, params) in ((name, unfiltered), (f"{name}_filtered", filtered))
    )


__all__ = [
    "PUBLIC_NO_SQL_ROUTES",
    "PUBLIC_ROUTE_QUERY_COVERAGE",
    "PUBLIC_WRITE_ROUTES",
    "NewsQuerySpecsProvider",
    "query_audit_catalog",
    "query_audit_for_connection",
]
