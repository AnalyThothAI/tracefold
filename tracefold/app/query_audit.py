from __future__ import annotations

import time
from typing import Protocol

from tracefold.platform.postgres.audit import (
    BOUNDED_WINDOW_SCAN_BUDGET,
    INDEXED_ROW_SCAN_BUDGET,
    PostgresQueryAudit,
    QueryAuditCatalog,
    ReadQuerySpec,
    postgres_query_specs,
)
from tracefold.trading.storage.analysis import (
    ACTIONS_BY_CASE_SQL,
    ANALYSIS_CASE_SQL,
    ANALYSIS_CASES_FOR_SOURCE_SQL,
    ANALYSIS_CASES_SQL,
    ANALYSIS_RUNTIME_SQL,
    ASSESSMENTS_BY_CASE_SQL,
    PAPER_BY_CASE_SQL,
)
from tracefold.trading.storage.executor import (
    EXECUTION_FILLS_SQL,
    EXECUTION_ORDERS_SQL,
    EXECUTION_PLANS_SQL,
    EXECUTION_REFUSALS_SQL,
    FILL_LEDGER_SQL,
    OPERATOR_INTENTS_SQL,
    REALIZED_TOTALS_SQL,
    SIGNAL_LEDGER_SQL,
)
from tracefold.trading.storage.history import LATEST_CASE_CREATED_AT_SQL
from tracefold.trading.storage.scoreboard import (
    SCOREBOARD_ACTIONS_SQL,
    SCOREBOARD_ASSESSMENTS_SQL,
    SCOREBOARD_CASES_SQL,
    SCOREBOARD_DISPOSITIONS_SQL,
    SCOREBOARD_EXECUTIONS_SQL,
    SCOREBOARD_LEGS_SQL,
    SCOREBOARD_TRIGGER_COUNT_SQL,
)

from .workers.runtime import workers_runtime_read_query


class NewsQuerySpecsProvider(Protocol):
    def __call__(self, *, now_ms: int) -> tuple[ReadQuerySpec, ...]: ...


PUBLIC_ROUTE_QUERY_COVERAGE: dict[str, tuple[str, ...]] = {
    "/readyz": ("readiness_schema",),
    "/api/status": ("readiness_schema", "workers_runtime"),
    "/api/news/feed": (
        "news_feed_events",
        "news_feed_filtered",
        "news_feed_filtered_counts",
        "news_search_identity",
        "news_search_event_symbols",
        "news_feed_asset_search",
        "news_feed_asset_search_counts",
        "news_feed_asset_search_cursor",
        "news_feed_text_search",
        "news_feed_text_search_counts",
        "news_feed_text_search_cursor",
        "news_event_asset_projection",
    ),
    "/api/news/quotes": ("news_quote_snapshot_read",),
    # #553. Three statements per list request -- the collapsed page, the per-kind intake summary and
    # the per-kind receipt summary beside it -- and four per detail request, because the card that
    # spoke for an observation and the observations it covered are each their own bounded read.
    "/api/news/market": (
        "news_market_groups",
        "news_market_oi_ranked",
        "news_market_sources",
        "news_market_delivery_summary",
    ),
    "/api/news/market/{item_id}": (
        "news_market_item",
        "news_market_item_delivery",
        "news_market_item_covered",
        "news_market_group_timeline",
    ),
    "/api/news/wallets": ("news_wallet_roster", "news_wallet_tape_state", "news_wallet_notification_funnel"),
    "/api/news/wallets/events": ("news_wallet_events", "news_wallet_event_totals"),
    "/api/news/wallets/events/{episode_id}": ("news_wallet_event", "news_wallet_event_fills"),
    # Three reads per request, and all three are named: `is_tradeable` runs its own statement and a
    # manifest that omitted it would let `db query-audit --analyze` report full coverage of a public route
    # while never planning one of its queries.
    "/api/news/symbols/{base}": ("news_symbol_contracts", "news_symbol_tradeable", "news_symbol_aliases"),
    "/api/news/events/{event_id}": (
        "news_event_detail",
        "news_event_members",
        "news_event_deliveries",
        "news_event_delivery_queue",
        "news_event_semantic_work",
        "news_event_update_head",
        "news_event_update_revisions",
        "news_event_update_previous_claims",
        "news_event_semantic_observations",
        "news_event_notification_work",
        "news_event_asset_projection",
    ),
    "/api/news/items/{item_id}/events": (
        "news_item_related_count",
        "news_item_related_keys",
        "news_item_related_events",
    ),
    # #570 A2. The eleven statements `FeedStorage.status_snapshot` executes, each named as the constant
    # the production read executes: nine of its own, plus the open-incident and recovery-backlog reads it
    # calls `OperationsStorage` for, which own those two statements. `workers_runtime` is the route's,
    # folded in beside the snapshot. Two of these names used to stand for the whole page and neither was
    # a statement the route runs: a route name is not SQL coverage. The instrument, asset-usage and price
    # reads the route composes after the snapshot are still unregistered; #570 A2 names them and they are
    # not in this change.
    "/api/news/status": (
        "workers_runtime",
        "news_status_ingest",
        "news_status_incidents_open",
        "news_status_recovery_backlog",
        "news_status_pipeline",
        "news_status_source_contracts",
        "news_status_delivery",
        "news_status_funnel_decisions",
        "news_status_funnel_totals",
    ),
    "/api/trading/status": ("trading_status_latest_case", "trading_analysis_runtime"),
    "/api/trading/cases": (
        "trading_analysis_cases",
        "trading_analysis_cases_for_source",
        "trading_analysis_case_by_id",
        "trading_assessments_by_case",
        "trading_actions_by_case",
        "trading_paper_by_case",
    ),
    "/api/trading/scoreboard": (
        "trading_scoreboard_cases",
        "trading_scoreboard_triggers",
        "trading_scoreboard_assessments",
        "trading_scoreboard_actions",
        "trading_scoreboard_legs",
        "trading_scoreboard_executions",
        "trading_scoreboard_dispositions",
    ),
    # #528 PR-1, #604 T3. The desk table plans three statements: its own per-entry fold, the
    # unfiltered window of the Command ledger it renders beside it, and the realized totals that are
    # the only numbers on the page not bounded by that window.
    "/api/trading/executions": (
        "trading_execution_plans",
        "trading_execution_refusals",
        "trading_execution_orders",
        "trading_execution_fills",
        "trading_realized_totals",
    ),
}

PUBLIC_NO_SQL_ROUTES = frozenset(
    {
        "/healthz",
        "/metrics",
        "/api/bootstrap",
    }
)

# #624: Serve exposes only reads; a future mutation must change this explicit authority inventory.
PUBLIC_WRITE_ROUTES: frozenset[str] = frozenset()


def query_audit_catalog(
    *,
    now_ms: int,
    news_query_specs: NewsQuerySpecsProvider | None = None,
) -> QueryAuditCatalog:
    provider = news_query_specs or _default_news_query_specs
    queries = (
        *postgres_query_specs(),
        workers_runtime_read_query(),
        *provider(now_ms=int(now_ms)),
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
    """Audit the exact Analysis, scoreboard, execution, and CLI SQL statements."""
    since_ms = int(now_ms) - 24 * 3_600_000
    since_ns = since_ms * 1_000_000
    day_start_ns = (int(now_ms) - int(now_ms) % 86_400_000) * 1_000_000
    day_end_ns = day_start_ns + 86_400_000 * 1_000_000
    case_id = "0" * 64
    ids = [case_id]
    specs = (
        ("trading_status_latest_case", LATEST_CASE_CREATED_AT_SQL, ()),
        ("trading_analysis_runtime", ANALYSIS_RUNTIME_SQL, ("binance_usdm_primary",)),
        ("trading_analysis_cases", ANALYSIS_CASES_SQL, (since_ms, None, None, 25)),
        ("trading_analysis_cases_for_source", ANALYSIS_CASES_FOR_SOURCE_SQL, ("source-item", None, None, 25)),
        ("trading_analysis_case_by_id", ANALYSIS_CASE_SQL, (case_id,)),
        ("trading_assessments_by_case", ASSESSMENTS_BY_CASE_SQL, (case_id,)),
        ("trading_actions_by_case", ACTIONS_BY_CASE_SQL, (case_id,)),
        ("trading_paper_by_case", PAPER_BY_CASE_SQL, (case_id,)),
        ("trading_scoreboard_cases", SCOREBOARD_CASES_SQL, (since_ms, now_ms)),
        ("trading_scoreboard_triggers", SCOREBOARD_TRIGGER_COUNT_SQL, (since_ms, now_ms)),
        ("trading_scoreboard_assessments", SCOREBOARD_ASSESSMENTS_SQL, (ids, None, None)),
        ("trading_scoreboard_actions", SCOREBOARD_ACTIONS_SQL, (ids, None, None)),
        ("trading_scoreboard_legs", SCOREBOARD_LEGS_SQL, (ids,)),
        ("trading_scoreboard_executions", SCOREBOARD_EXECUTIONS_SQL, (ids,)),
        ("trading_scoreboard_dispositions", SCOREBOARD_DISPOSITIONS_SQL, (ids,)),
        ("trading_execution_plans", EXECUTION_PLANS_SQL, (since_ns, None, None, 101)),
        ("trading_execution_refusals", EXECUTION_REFUSALS_SQL, (since_ns, None, None, 101)),
        ("trading_execution_orders", EXECUTION_ORDERS_SQL, (ids,)),
        ("trading_execution_fills", EXECUTION_FILLS_SQL, (ids,)),
        (
            "trading_realized_totals",
            REALIZED_TOTALS_SQL,
            (
                day_start_ns,
                day_end_ns,
                day_start_ns,
                day_end_ns,
                day_start_ns,
                day_end_ns,
                day_start_ns,
                day_end_ns,
                "binance_usdm_primary",
            ),
        ),
        ("trading_console_commands", OPERATOR_INTENTS_SQL, (since_ns, None, None, 101)),
        ("trading_signal_ledger", SIGNAL_LEDGER_SQL, (since_ns, 101)),
        ("trading_fill_ledger", FILL_LEDGER_SQL, (since_ns, 101)),
    )
    return tuple(
        ReadQuerySpec(
            name=name,
            sql=sql,
            params=params,
            max_read_return_amplification=200.0,
            max_scanned_rows=BOUNDED_WINDOW_SCAN_BUDGET
            if "scoreboard" in name or "cases" in name
            else INDEXED_ROW_SCAN_BUDGET,
        )
        for name, sql, params in specs
    )


__all__ = [
    "PUBLIC_NO_SQL_ROUTES",
    "PUBLIC_ROUTE_QUERY_COVERAGE",
    "PUBLIC_WRITE_ROUTES",
    "NewsQuerySpecsProvider",
    "query_audit_catalog",
    "query_audit_for_connection",
]
