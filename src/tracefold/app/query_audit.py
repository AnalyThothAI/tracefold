from __future__ import annotations

import time
from typing import Protocol

from tracefold.platform.postgres.postgres_audit import (
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
    "/api/news/review": ("news_review_window",),
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
        "news_control_state",
    ),
}

PUBLIC_NO_SQL_ROUTES = frozenset(
    {
        "/healthz",
        "/metrics",
        "/api/bootstrap",
    }
)


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
    )
    aggregate_input_queries = [query.name for query in queries if query.amplification_basis == "aggregate_input"]
    if aggregate_input_queries:
        raise ValueError("only bounded aggregate reads may use aggregate-input amplification")
    return QueryAuditCatalog(
        queries=queries,
        query_routes=dict(PUBLIC_ROUTE_QUERY_COVERAGE),
        no_sql_routes=PUBLIC_NO_SQL_ROUTES,
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


__all__ = [
    "PUBLIC_NO_SQL_ROUTES",
    "PUBLIC_ROUTE_QUERY_COVERAGE",
    "NewsQuerySpecsProvider",
    "query_audit_catalog",
    "query_audit_for_connection",
]
