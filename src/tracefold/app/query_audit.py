from __future__ import annotations

import time
from typing import Protocol

from tracefold.platform.postgres.postgres_audit import (
    PostgresQueryAudit,
    QueryAuditCatalog,
    ReadQuerySpec,
    postgres_query_specs,
)

from .workers_runtime import workers_runtime_read_query


class NewsQuerySpecsProvider(Protocol):
    def __call__(self, *, now_ms: int) -> tuple[ReadQuerySpec, ...]: ...


PUBLIC_ROUTE_QUERY_COVERAGE: dict[str, tuple[str, ...]] = {
    "/readyz": ("readiness_schema",),
    "/ws": ("persisted_live_after_cursor",),
    "/api/status": (
        "readiness_schema",
        "workers_runtime",
        "provider_gmgn_freshness",
        "provider_circuits",
        "provider_backlogs",
    ),
    "/api/recent": ("recent_all", "events_by_ids"),
    "/api/events/by-ids": ("events_by_ids",),
    "/api/token-radar": ("token_radar_latest",),
    "/api/live-market": ("live_market_current",),
    "/api/search": ("search_v2_lexical", "search_v2_substring"),
    "/api/search/inspect": (
        "search_v2_lexical",
        "search_v2_substring",
        "token_profile_target",
    ),
    "/api/token-case": (
        "token_profile_target",
        "target_posts_recent",
    ),
    "/api/target-posts": ("target_posts_recent",),
    "/api/target-social-timeline": ("target_posts_recent",),
    "/api/news/feed": (
        "news_feed_focus_rows",
        "news_feed_focus_facets",
        "news_feed_push_contexts",
    ),
    "/api/news/stories/{story_id}": (
        "news_story",
        "news_story_members",
        "news_feed_push_contexts",
    ),
    "/api/news/brief": ("news_brief",),
    "/api/news/sources": ("news_sources",),
    "/api/news/status": (
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
    ),
    "/api/macro/overview": ("macro_modules_current",),
    "/api/macro/rates-fed": ("macro_module_current", "macro_modules_current"),
    "/api/macro/economy-inflation": ("macro_module_current", "macro_modules_current"),
    "/api/macro/liquidity-funding": ("macro_module_current", "macro_modules_current"),
    "/api/macro/credit": ("macro_module_current", "macro_modules_current"),
    "/api/macro/volatility": ("macro_module_current", "macro_modules_current"),
    "/api/macro/cross-asset": ("macro_module_current", "macro_modules_current"),
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
    if aggregate_input_queries != ["news_feed_focus_facets"]:
        raise ValueError("only news_feed_focus_facets may use aggregate-input amplification")
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
