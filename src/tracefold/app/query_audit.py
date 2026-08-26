from __future__ import annotations

import time
from typing import Protocol

from tracefold.platform.postgres.audit import (
    PostgresQueryAudit,
    QueryAuditCatalog,
    ReadQuerySpec,
    postgres_query_specs,
)

from .workers.runtime import workers_runtime_read_query


class NewsQuerySpecsProvider(Protocol):
    def __call__(self, *, now_ms: int) -> tuple[ReadQuerySpec, ...]: ...


PUBLIC_ROUTE_QUERY_COVERAGE: dict[str, tuple[str, ...]] = {
    "/readyz": ("readiness_schema",),
    "/api/status": ("readiness_schema", "workers_runtime"),
    "/api/news/feed": (
        "news_feed_events",
        "news_feed_symbol_filter",
        "news_feed_search",
        "news_reaction_attach",
    ),
    "/api/news/quotes": ("news_quote_snapshot_read",),
    # Three reads per request, and all three are named: `is_tradeable` runs its own statement and a
    # manifest that omitted it would let `db query-audit --analyze` report full coverage of a public route
    # while never planning one of its queries.
    "/api/news/symbols/{base}": ("news_symbol_contracts", "news_symbol_tradeable", "news_symbol_aliases"),
    "/api/news/events/{event_id}": (
        "news_event_detail",
        "news_event_members",
        "news_event_verdicts",
        "news_reaction_attach",
    ),
    "/api/news/status": (
        "workers_runtime",
        "news_status_ingest",
        "news_status_incidents_open",
        "news_status_pipeline_24h",
        "news_status_delivery_1h",
        "news_status_learning_retention",
    ),
    # #207 PR-W4. `trading_runtime_state` is a single row and needs no spec of its own; the two that scan
    # do, and both are bounded by a 24 h window plus a hard limit.
    "/api/trading/status": ("trading_status_counts",),
    "/api/trading/orders": ("trading_console_orders", "trading_console_cases"),
    "/api/trading/events/{event_id}": ("trading_case_for_source_key",),
}

PUBLIC_NO_SQL_ROUTES = frozenset(
    {
        "/healthz",
        "/metrics",
        "/api/bootstrap",
    }
)

# The console is read-only and the ReviewDesk moved to the CLI (#256), so the public HTTP surface has no
# write route at all. The audit still asks for the set rather than assuming it stays empty.
PUBLIC_WRITE_ROUTES: frozenset[str] = frozenset()


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
        *_trading_query_specs(now_ms=int(now_ms)),
    )
    aggregate_input_queries = [query.name for query in queries if query.amplification_basis == "aggregate_input"]
    if aggregate_input_queries:
        raise ValueError("only bounded aggregate reads may use aggregate-input amplification")
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
    from tracefold.news.query_specs import news_query_specs

    return news_query_specs(now_ms=now_ms)


def _trading_query_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
    """The capital lane's two console reads, with the predicates they actually run (#207 PR-W4).

    Declared here rather than in `tracefold.trading`: the specs describe an *app* surface — which HTTP route
    runs which statement — and the package that owns the tables does not know an HTTP layer exists.
    """

    since_ms = int(now_ms) - 24 * 3_600_000
    return (
        ReadQuerySpec(
            name="trading_status_counts",
            sql="SELECT state, count(*) AS n FROM trading_orders WHERE created_at_ms >= %s GROUP BY state",
            params=(since_ms,),
        ),
        ReadQuerySpec(
            name="trading_console_orders",
            sql="""
                SELECT o.order_id, o.state, c.trigger_kind, c.strategy_id
                  FROM trading_orders o
                  JOIN trading_cases c ON c.case_id = o.case_id
                 WHERE o.created_at_ms >= %s
                 ORDER BY o.created_at_ms DESC
                 LIMIT 100
            """,
            params=(since_ms,),
        ),
        ReadQuerySpec(
            name="trading_case_for_source_key",
            sql="""
                SELECT c.case_id, o.order_id
                  FROM trading_cases c
                  LEFT JOIN trading_orders o ON o.case_id = c.case_id
                 WHERE c.primary_source_key = %s
            """,
            params=("oi:not-a-real-event:oi_signal_v1",),
        ),
        ReadQuerySpec(
            name="trading_console_cases",
            sql="""
                SELECT c.case_id, c.state
                  FROM trading_cases c
                 WHERE c.created_at_ms >= %s
                   AND NOT EXISTS (SELECT 1 FROM trading_orders o WHERE o.case_id = c.case_id)
                 ORDER BY c.created_at_ms DESC
                 LIMIT 100
            """,
            params=(since_ms,),
        ),
    )


__all__ = [
    "PUBLIC_NO_SQL_ROUTES",
    "PUBLIC_ROUTE_QUERY_COVERAGE",
    "PUBLIC_WRITE_ROUTES",
    "NewsQuerySpecsProvider",
    "query_audit_catalog",
    "query_audit_for_connection",
]
