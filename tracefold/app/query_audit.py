from __future__ import annotations

import time
from typing import Protocol

from tracefold.platform.postgres.audit import (
    PostgresQueryAudit,
    QueryAuditCatalog,
    ReadQuerySpec,
    postgres_query_specs,
)
from tracefold.trading import (
    BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
    PROTECTION_CONTRACT_SHA256,
    QUOTE_CONTRACT_SHA256,
)
from tracefold.trading.contracts import BINDING_RUNTIME_HEARTBEAT_STALE_AFTER_MS
from tracefold.trading.storage.query_sql import (
    AUTHORITY_PROJECTION_SQL,
    BINDING_RUNTIME_ROWS_SQL,
    CAPITAL_AUTHORITY_SNAPSHOT_SQL,
    DEFAULT_CONSOLE_INTENT_STATES,
    EXECUTION_CAPABILITY_SNAPSHOT_SQL,
    GATE_DECISION_FOR_SOURCE_KEY_SQL,
    TRADING_STATUS_COUNTS_SQL,
    console_capital_evidence_sql,
    console_cases_sql,
    console_intents_sql,
    gate_decisions_since_sql,
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
    "/api/news/status": (
        "workers_runtime",
        "news_status_ingest",
        "news_status_incidents_open",
        "news_status_recovery_backlog",
        "news_status_pipeline_24h",
        "news_status_delivery_1h",
        "news_status_learning_retention",
    ),
    # #207 PR-W4. `trading_runtime_state` is a single row and needs no spec of its own; the two that scan
    # do, and both are bounded by a 24 h window plus a hard limit.
    "/api/trading/status": ("trading_status_counts",),
    "/api/trading/intents": ("trading_console_intents",),
    # One route per durable aggregate (#331): Cases no longer ride along on the Intent read.
    "/api/trading/cases": ("trading_console_cases",),
    "/api/trading/capabilities": ("trading_capability_bindings", "trading_capability_snapshot"),
    "/api/trading/evidence": ("trading_authority_projection", "trading_console_capital_evidence"),
    "/api/trading/gate/{event_id}": ("trading_gate_decision_for_source_key",),
    # #269. The same admission ledger the event endpoint reads one row of, for a whole window — bounded
    # by 24 h and a hard row limit, like the two beside it.
    "/api/trading/gate": ("trading_gate_decisions_since",),
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
    allowed_aggregate_input_queries = {
        # The production first-page feed aggregates are time-bounded and scan exactly the predicate whose
        # page they describe. Their one returned row is not the right amplification denominator; the bounded
        # input is. Both SQL statements are built by FeedStorage's own production statement builder.
        "news_feed_asset_search_counts",
        "news_feed_text_search_counts",
    }
    aggregate_input_queries = {query.name for query in queries if query.amplification_basis == "aggregate_input"}
    if aggregate_input_queries - allowed_aggregate_input_queries:
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
    from tracefold.news.storage.query_specs import news_query_specs

    return news_query_specs(now_ms=now_ms)


def _trading_query_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
    """The capital lane's console reads, with the predicates they actually run (#207 PR-W4/#331).

    Declared here rather than in `tracefold.trading`: the specs describe an *app* surface — which HTTP route
    runs which statement — and the package that owns the tables does not know an HTTP layer exists.
    """

    since_ms = int(now_ms) - 24 * 3_600_000
    return (
        ReadQuerySpec(
            name="trading_status_counts",
            sql=TRADING_STATUS_COUNTS_SQL,
            params=(since_ms,),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="trading_console_intents",
            sql=console_intents_sql(),
            params=(list(DEFAULT_CONSOLE_INTENT_STATES), since_ms, 101),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="trading_gate_decision_for_source_key",
            sql=GATE_DECISION_FOR_SOURCE_KEY_SQL,
            params=("oi:not-a-real-event:oi_signal_v1",),
            max_read_return_amplification=4.0,
        ),
        ReadQuerySpec(
            # #269. One admission answer per source in the window, newest frame first. `DISTINCT ON`
            # over the source key is what keeps the batch to one row per frame, matching the
            # distributions the same page prints above it — a frame two configurations have looked at
            # must not appear twice in a table whose total is a frame count.
            #
            # The subquery and the outer sort are both here on purpose: the dedup has to order by
            # `source_key` and the table has to arrive in frame order, so the shipped read materialises
            # the whole 24 h dedup set and re-sorts it. Flattening that into one `DISTINCT ON` with the
            # limit inside would certify a plan that can stop early — a plan the route never runs.
            name="trading_gate_decisions_since",
            sql=gate_decisions_since_sql(),
            params=("oi", since_ms, 401),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            # The Case aggregate on its own axis. The Intent link is one nullable id, never a joined
            # lifecycle: `NOT EXISTS (... trading_intents ...)` used to make "no Intent" a property of
            # the Case read, which is how one contract came to answer two different questions.
            name="trading_console_cases",
            sql=console_cases_sql(),
            params=(since_ms, 101),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="trading_capability_bindings",
            sql=BINDING_RUNTIME_ROWS_SQL,
            params={
                "now": int(now_ms),
                "heartbeat_floor": int(now_ms) - BINDING_RUNTIME_HEARTBEAT_STALE_AFTER_MS,
                "adapter_contract": BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
                "quote_contract": QUOTE_CONTRACT_SHA256,
                "protection_contract": PROTECTION_CONTRACT_SHA256,
            },
            max_read_return_amplification=4.0,
        ),
        ReadQuerySpec(
            name="trading_capability_snapshot",
            sql=EXECUTION_CAPABILITY_SNAPSHOT_SQL,
            params=("0" * 64,),
            max_read_return_amplification=4.0,
        ),
        ReadQuerySpec(
            name="trading_authority_projection",
            sql=AUTHORITY_PROJECTION_SQL,
            max_read_return_amplification=8.0,
        ),
        ReadQuerySpec(
            name="trading_console_capital_evidence",
            sql=console_capital_evidence_sql(),
            params=(101,),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="trading_capital_authority_snapshot",
            sql=CAPITAL_AUTHORITY_SNAPSHOT_SQL,
            params={
                "active_states": list(DEFAULT_CONSOLE_INTENT_STATES),
                "bindings": ["BINANCE_USDM", "HYPERLIQUID_PERP"],
                "since_ms": since_ms,
                "now_ms": int(now_ms),
            },
            max_read_return_amplification=20.0,
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
