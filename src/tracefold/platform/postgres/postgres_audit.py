from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from tracefold.platform.postgres.postgres_migrations import latest_migration_version
from tracefold.platform.validation import require_nonnegative_int

SEARCH_CUTOFF_AT_MS_PARAM = "search_cutoff_at_ms"
SEARCH_AUDIT_WINDOW_MS = 24 * 60 * 60 * 1000
MAX_READ_RETURN_AMPLIFICATION = 20.0
LARGE_SEQ_SCAN_PLAN_ROWS = 10_000

CORE_TABLES = (
    "raw_frames",
    "events",
    "event_entities",
    "registry_assets",
    "asset_identity_evidence",
    "asset_identity_current",
    "market_ticks",
    "enriched_events",
    "token_evidence",
    "token_intents",
    "token_intent_evidence",
    "token_intent_resolutions",
    "token_radar_current",
)

PROJECTION_TABLES = (
    "token_radar_current",
    "news_projection_summary",
    "news_brief_selection_current",
    "news_brief_current",
)

NEWS_TABLES = (
    "news_sources",
    "news_items",
    "news_stories",
    "news_story_members",
    "news_projection_summary",
    "news_brief_selection_current",
    "news_brief_current",
    "news_push_state",
    "news_push_deliveries",
)

FOREIGN_KEY_CONSTRAINTS = {
    "event_entities_missing_events": ("event_entities", "event_entities_event_id_fkey"),
    "token_evidence_missing_events": ("token_evidence", "token_evidence_event_id_fkey"),
    "token_intents_missing_events": ("token_intents", "token_intents_event_id_fkey"),
    "token_resolutions_missing_intents": (
        "token_intent_resolutions",
        "token_intent_resolutions_intent_id_fkey",
    ),
}
HOT_QUERIES: tuple[dict[str, Any], ...] = (
    {
        "name": "readiness_schema",
        "sql": "SELECT version_num FROM alembic_version LIMIT 1",
        "params": (),
    },
    {
        "name": "recent_all",
        "sql": """
            SELECT event_id
            FROM events
            ORDER BY received_at_ms DESC, event_id DESC
            LIMIT 50
        """,
        "params": (),
    },
    {
        "name": "events_by_ids",
        "sql": """
            SELECT event_id
            FROM events
            WHERE event_id = ANY(%s::text[])
            LIMIT 100
        """,
        "params": (["audit-missing-event"],),
    },
    {
        "name": "search_v2_lexical",
        "sql": """
            WITH query AS (
              SELECT
                websearch_to_tsquery('simple', %(query)s) AS simple_q,
                websearch_to_tsquery('english', %(query)s) AS english_q
            ),
            ranked AS (
              SELECT
                e.*,
                (
                  ts_rank_cd(e.search_tsv, query.simple_q)
                  + ts_rank_cd(e.search_tsv, query.english_q)
                ) AS route_score
              FROM events e, query
              WHERE (
                  e.search_tsv @@ query.simple_q
                  OR e.search_tsv @@ query.english_q
                )
                AND e.received_at_ms >= %(search_cutoff_at_ms)s
            )
            SELECT
              *,
              row_number() OVER (
                ORDER BY received_at_ms DESC, event_id DESC
              ) AS route_rank
            FROM ranked
            ORDER BY received_at_ms DESC, event_id DESC
            LIMIT 50
        """,
        "params": {
            "query": "pepe",
            "search_cutoff_at_ms": None,
        },
    },
    {
        "name": "search_v2_substring",
        "sql": """
            SELECT event_id
            FROM events
            WHERE search_text ILIKE %(substring_pattern)s ESCAPE '\\'
              AND received_at_ms >= %(search_cutoff_at_ms)s
            ORDER BY received_at_ms DESC, event_id DESC
            LIMIT 20
        """,
        "params": {
            "substring_pattern": "%pepe%",
            "search_cutoff_at_ms": None,
        },
    },
    {
        "name": "token_radar_latest",
        "sql": """
            SELECT schema_version, state_fingerprint, latest_attempt_status,
                   latest_error_code, state_changed_at_ms, served_payload
            FROM token_radar_current
            WHERE singleton_key = true
        """,
        "params": (),
    },
    {
        "name": "token_profile_target",
        "sql": """
            SELECT target_type, target_id, payload_hash
            FROM token_profile_current
            WHERE target_type = 'Asset'
              AND target_id = %s
            LIMIT 1
        """,
        "params": ("audit-missing-target",),
    },
    {
        "name": "live_market_current",
        "sql": """
            SELECT current.tick_id
            FROM registry_assets assets
            JOIN market_tick_current current
              ON current.target_type = 'chain_token'
             AND current.target_id = assets.chain_id || ':' || assets.address
            WHERE assets.asset_id = %s
            LIMIT 1
        """,
        "params": ("audit-missing-target",),
    },
    {
        "name": "target_posts_recent",
        "sql": """
            SELECT events.event_id
            FROM token_intent_resolutions tir
            JOIN events ON events.event_id = tir.event_id
            WHERE tir.target_type = %s
              AND tir.target_id = %s
              AND tir.is_current
            ORDER BY events.received_at_ms DESC, events.event_id DESC
            LIMIT 51
        """,
        "params": ("Asset", "audit-missing-target"),
    },
    {
        "name": "news_feed",
        "sql": """
            SELECT stories.story_id
            FROM news_stories stories
            JOIN news_items representative
              ON representative.item_id = stories.representative_item_id
            ORDER BY stories.importance_score DESC,
                     stories.last_published_at_ms DESC,
                     stories.story_id
            LIMIT 101
        """,
        "params": (),
    },
    {
        "name": "news_feed_filtered_facets",
        "amplification_basis": "aggregate_input",
        "sql": """
            WITH current_member_scores AS MATERIALIZED (
              SELECT members.story_id,
                     CASE
                       WHEN jsonb_typeof(items.provider_metadata -> 'score') = 'number'
                         THEN (items.provider_metadata ->> 'score')::numeric
                       ELSE NULL
                     END AS provider_score
              FROM news_story_members members
              JOIN news_items items ON items.item_id = members.item_id
              OFFSET 0
            ),
            filtered_stories AS MATERIALIZED (
              SELECT stories.story_id, stories.facet_facts
              FROM news_stories stories
              WHERE EXISTS (
                SELECT 1
                FROM current_member_scores scores
                WHERE scores.story_id = stories.story_id
                  AND scores.provider_score > 70
              )
            ),
            facet_rows AS (
              SELECT filtered_stories.story_id,
                     'source'::text AS facet_kind,
                     source_value.value AS facet_value,
                     sources.name AS facet_label
              FROM filtered_stories
              CROSS JOIN LATERAL jsonb_array_elements_text(
                filtered_stories.facet_facts -> 'source_ids'
              ) AS source_value(value)
              JOIN news_sources sources ON sources.source_id = source_value.value

              UNION ALL

              SELECT filtered_stories.story_id,
                     'reporting_origin'::text AS facet_kind,
                     lower(btrim(origin_value.value)) AS facet_value,
                     btrim(origin_value.value) AS facet_label
              FROM filtered_stories
              CROSS JOIN LATERAL jsonb_array_elements_text(
                filtered_stories.facet_facts -> 'reporting_origins'
              ) AS origin_value(value)
            )
            SELECT facet_kind,
                   facet_value,
                   min(facet_label) AS facet_label,
                   count(DISTINCT story_id)::integer AS story_count
            FROM facet_rows
            WHERE nullif(btrim(facet_value), '') IS NOT NULL
            GROUP BY facet_kind, facet_value
            ORDER BY facet_kind, story_count DESC, facet_value
            LIMIT 101
        """,
        "params": (),
    },
    {
        "name": "news_story",
        "sql": """
            SELECT stories.story_id
            FROM news_stories stories
            JOIN news_sources sources
              ON sources.source_id = stories.representative_source_id
            WHERE stories.story_id = %s
            LIMIT 1
        """,
        "params": ("audit-missing-story",),
    },
    {
        "name": "news_story_members",
        "sql": """
            SELECT members.item_id
            FROM news_story_members members
            JOIN news_items items ON items.item_id = members.item_id
            JOIN news_sources sources ON sources.source_id = items.source_id
            WHERE members.story_id = %s
            ORDER BY items.published_at_ms DESC,
                     items.item_id
            LIMIT 101
        """,
        "params": ("audit-missing-story",),
    },
    {
        "name": "news_brief",
        "sql": """
            SELECT current.slot_at_ms, current.slot_status,
                   current.next_due_at_ms, current.model_outcome,
                   current.pointer_action, current.served_payload,
                   current.updated_at_ms
              FROM news_brief_current current
             WHERE current.singleton_key
             LIMIT 1
        """,
        "params": (),
    },
    {
        "name": "news_sources",
        "sql": """
            SELECT source_id, source_kind, tier, name,
                   live_connected, last_recovery_at_ms,
                   next_fetch_at_ms, claim_lease_expires_at_ms,
                   last_outcome, last_error
            FROM news_sources
            WHERE enabled
            ORDER BY tier, name, source_id
            LIMIT 101
        """,
        "params": (),
    },
    {
        "name": "news_status",
        "sql": """
            SELECT active_story_count AS active_count,
                   newest_story_at_ms
            FROM news_projection_summary
            WHERE singleton_key = 'current'
        """,
        "params": (),
    },
    {
        "name": "macro_modules_current",
        "sql": """
            SELECT module_id, payload_hash
            FROM macro_module_current
            ORDER BY module_id
            LIMIT 6
        """,
        "params": (),
    },
    {
        "name": "macro_module_current",
        "sql": """
            SELECT module_id, payload_hash
            FROM macro_module_current
            WHERE module_id = %s
            LIMIT 1
        """,
        "params": ("rates_fed",),
    },
    {
        "name": "workers_runtime",
        "sql": """
            SELECT runtime_id, lifecycle_state, heartbeat_at_ms
            FROM workers_runtime
            WHERE singleton_key
        """,
        "params": (),
    },
    {
        "name": "provider_gmgn_freshness",
        "sql": """
            SELECT received_at_ms
            FROM raw_frames
            WHERE source = 'gmgn'
            ORDER BY received_at_ms DESC
            LIMIT 1
        """,
        "params": (),
    },
    {
        "name": "provider_circuits",
        "sql": """
            SELECT provider, status, consecutive_failures, next_probe_at_ms
            FROM provider_circuit_state
            WHERE provider = ANY(%s::text[])
            ORDER BY provider
        """,
        "params": (
            [
                "gmgn_direct_ws",
                "gmgn_dex_profile",
                "binance_web3_profile",
                "okx_dex_search",
            ],
        ),
    },
    {
        "name": "provider_backlogs",
        "sql": """
            WITH providers(provider) AS (
              SELECT unnest(%s::text[])
            )
            SELECT
              provider,
              (
                SELECT profile_queue.provider
                FROM asset_profile_refresh_targets profile_queue
                WHERE profile_queue.provider = providers.provider
                  AND profile_queue.terminal_reason IS NULL
                ORDER BY
                  profile_queue.provider,
                  profile_queue.priority,
                  profile_queue.due_at_ms,
                  profile_queue.updated_at_ms,
                  profile_queue.target_type,
                  profile_queue.target_id
                LIMIT 1
              ) IS NOT NULL OR (
                SELECT discovery_queue.provider
                FROM token_discovery_dirty_lookup_keys discovery_queue
                WHERE discovery_queue.provider = providers.provider
                ORDER BY
                  discovery_queue.provider,
                  discovery_queue.refresh_priority,
                  discovery_queue.due_at_ms,
                  discovery_queue.latest_seen_ms DESC,
                  discovery_queue.updated_at_ms,
                  discovery_queue.lookup_key
                LIMIT 1
              ) IS NOT NULL AS has_backlog
            FROM providers
            ORDER BY provider
        """,
        "params": (
            [
                "gmgn_direct_ws",
                "gmgn_dex_profile",
                "binance_web3_profile",
                "okx_dex_search",
            ],
        ),
    },
    {
        "name": "persisted_live_after_cursor",
        "sql": """
            SELECT cursor, payload_json
            FROM persisted_live_events
            WHERE cursor > %s
            ORDER BY cursor
            LIMIT 500
        """,
        "params": (0,),
    },
)


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
        "news_feed",
        "news_feed_filtered_facets",
    ),
    "/api/news/stories/{story_id}": ("news_story", "news_story_members"),
    "/api/news/brief": ("news_brief",),
    "/api/news/sources": ("news_sources",),
    "/api/news/status": ("news_status", "news_brief"),
    "/api/macro/overview": ("macro_modules_current",),
    "/api/macro/rates-fed": (
        "macro_module_current",
        "macro_modules_current",
    ),
    "/api/macro/economy-inflation": (
        "macro_module_current",
        "macro_modules_current",
    ),
    "/api/macro/liquidity-funding": (
        "macro_module_current",
        "macro_modules_current",
    ),
    "/api/macro/credit": (
        "macro_module_current",
        "macro_modules_current",
    ),
    "/api/macro/volatility": (
        "macro_module_current",
        "macro_modules_current",
    ),
    "/api/macro/cross-asset": (
        "macro_module_current",
        "macro_modules_current",
    ),
}

PUBLIC_NO_SQL_ROUTES = frozenset(
    {
        "/healthz",
        "/metrics",
        "/api/bootstrap",
    }
)


class PostgresOperationalAudit:
    def __init__(self, conn: Any, *, expected_migration_version: str | None = None):
        self.conn = conn
        self.expected_migration_version = expected_migration_version or latest_migration_version()

    def run(self) -> dict[str, Any]:
        counts = self._counts(CORE_TABLES)
        projection_schema = self._table_presence(PROJECTION_TABLES)
        actual_news_tables = self._news_tables()
        news_schema = {
            "expected_tables": list(NEWS_TABLES),
            "actual_tables": sorted(actual_news_tables),
            "exact": actual_news_tables == set(NEWS_TABLES),
        }
        foreign_key_checks = self._foreign_key_checks()
        migration_version = self._migration_version()
        migration_ready = migration_version == self.expected_migration_version
        orphan_count = sum(int(value) for value in foreign_key_checks.values())
        return {
            "ok": (
                migration_ready and orphan_count == 0 and all(projection_schema.values()) and bool(news_schema["exact"])
            ),
            "engine": "postgresql",
            "migration_version": migration_version,
            "expected_migration_version": self.expected_migration_version,
            "migration_status": "ready" if migration_ready else "stale",
            "counts": counts,
            "projection_schema": projection_schema,
            "news_schema": news_schema,
            "foreign_key_checks": foreign_key_checks,
        }

    def _counts(self, table_names: tuple[str, ...]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table_name in table_names:
            if not self._table_exists(table_name):
                counts[table_name] = -1
                continue
            row = self.conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
            counts[table_name] = int(row["count"] if row else 0)
        return counts

    def _table_presence(self, table_names: tuple[str, ...]) -> dict[str, bool]:
        return {table_name: self._table_exists(table_name) for table_name in table_names}

    def _table_exists(self, table_name: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 AS ok
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            (table_name,),
        ).fetchone()
        return row is not None

    def _news_tables(self) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
              AND left(table_name, 5) = 'news_'
            """
        ).fetchall()
        return {str(row["table_name"]) for row in rows}

    def _foreign_key_checks(self) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT child.relname AS child_table,
                   constraints.conname,
                   constraints.convalidated
            FROM pg_constraint AS constraints
            JOIN pg_class AS child ON child.oid = constraints.conrelid
            JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace
            WHERE constraints.contype = 'f'
              AND namespace.nspname = 'public'
              AND child.relname = ANY(%s)
              AND constraints.conname = ANY(%s)
            """,
            (
                [table_name for table_name, _constraint_name in FOREIGN_KEY_CONSTRAINTS.values()],
                [constraint_name for _table_name, constraint_name in FOREIGN_KEY_CONSTRAINTS.values()],
            ),
        ).fetchall()
        validated = {(str(row["child_table"]), str(row["conname"])): bool(row["convalidated"]) for row in rows}
        checks = {
            name: int(not validated.get(constraint_identity, False))
            for name, constraint_identity in FOREIGN_KEY_CONSTRAINTS.items()
        }
        return checks

    def _migration_version(self) -> str | None:
        row = self.conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        return str(row["version_num"]) if row else None


class PostgresQueryAudit:
    def __init__(
        self,
        conn: Any,
        *,
        now_ms: int | None = None,
    ):
        self.conn = conn
        resolved_now_ms = int(now_ms if now_ms is not None else time.time() * 1_000)
        self.search_cutoff_at_ms = resolved_now_ms - SEARCH_AUDIT_WINDOW_MS

    def run(self, *, analyze: bool = False) -> dict[str, Any]:
        queries = [self._explain(item, analyze=analyze) for item in HOT_QUERIES]
        audited_names = {str(item["name"]) for item in HOT_QUERIES}
        missing_query_names = sorted(
            {
                query_name
                for query_names in PUBLIC_ROUTE_QUERY_COVERAGE.values()
                for query_name in query_names
                if query_name not in audited_names
            }
        )
        return {
            "ok": not missing_query_names and all(item["ok"] for item in queries),
            "engine": "postgresql",
            "analyze": bool(analyze),
            "thresholds": {
                "large_seq_scan_plan_rows": LARGE_SEQ_SCAN_PLAN_ROWS,
                "max_read_return_amplification": MAX_READ_RETURN_AMPLIFICATION,
                "temp_blocks": 0,
            },
            "route_coverage": {
                "query_routes": PUBLIC_ROUTE_QUERY_COVERAGE,
                "no_sql_routes": sorted(PUBLIC_NO_SQL_ROUTES),
                "missing_query_names": missing_query_names,
            },
            "queries": queries,
        }

    def _explain(self, item: dict[str, Any], *, analyze: bool) -> dict[str, Any]:
        prefix = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)" if analyze else "EXPLAIN (FORMAT JSON)"
        try:
            rows = self.conn.execute(f"{prefix} {item['sql']}", self._params(item["params"])).fetchall()
            plan = _json_plan(rows)
            metrics = (
                _plan_metrics(
                    plan,
                    amplification_basis=str(item.get("amplification_basis") or "returned_rows"),
                )
                if analyze
                else None
            )
            violations = _plan_violations(metrics) if metrics is not None else []
            return {
                "ok": not violations,
                "name": item["name"],
                "plan": plan,
                "metrics": metrics,
                "violations": violations,
            }
        except Exception as exc:
            return {
                "ok": False,
                "name": item["name"],
                "error": type(exc).__name__,
                "detail": str(exc),
                "plan": [],
                "metrics": None,
                "violations": ["explain_failed"],
            }

    def _params(self, params: Any) -> Any:
        if not isinstance(params, dict):
            return params
        bound = dict(params)
        if SEARCH_CUTOFF_AT_MS_PARAM in bound:
            bound[SEARCH_CUTOFF_AT_MS_PARAM] = self.search_cutoff_at_ms
        return bound


class ProjectionValidationAudit:
    def __init__(self, conn: Any):
        self.conn = conn

    def run(self, *, sample: int) -> dict[str, Any]:
        sample_size = require_nonnegative_int(
            sample,
            error_code="projection_validation_sample_required",
        )
        row = self.conn.execute(
            """
            WITH sampled_radar_rows AS (
              SELECT item
              FROM token_radar_current current
              CROSS JOIN LATERAL jsonb_array_elements(current.served_payload -> 'items') item
              WHERE current.singleton_key = true
              LIMIT %s
            ),
            reference_counts AS (
              SELECT
                COUNT(*) AS checked_count,
                COUNT(*) FILTER (
                  WHERE NULLIF(item ->> 'trigger_event_id', '') IS NULL
                     OR NULLIF(item #>> '{target,target_id}', '') IS NULL
                     OR item #>> '{target,target_type}' NOT IN ('Asset', 'CexToken')
                     OR event.event_id IS NULL
                     OR (
                       item #>> '{target,target_type}' = 'Asset'
                       AND asset.asset_id IS NULL
                     )
                     OR (
                       item #>> '{target,target_type}' = 'CexToken'
                       AND cex.cex_token_id IS NULL
                     )
                ) AS mismatch_count
              FROM sampled_radar_rows
              LEFT JOIN events event
                ON event.event_id = sampled_radar_rows.item ->> 'trigger_event_id'
              LEFT JOIN registry_assets asset
                ON sampled_radar_rows.item #>> '{target,target_type}' = 'Asset'
               AND asset.asset_id = sampled_radar_rows.item #>> '{target,target_id}'
              LEFT JOIN cex_tokens cex
                ON sampled_radar_rows.item #>> '{target,target_type}' = 'CexToken'
               AND cex.cex_token_id = sampled_radar_rows.item #>> '{target,target_id}'
            ),
            latest_radar AS (
              SELECT evaluation_at_ms AS computed_at_ms
              FROM token_radar_current
              WHERE singleton_key = true
            )
            SELECT
              latest_radar.computed_at_ms,
              COALESCE(reference_counts.checked_count, 0) AS checked_count,
              COALESCE(reference_counts.mismatch_count, 0) AS mismatch_count
            FROM reference_counts
            CROSS JOIN latest_radar
            """,
            (sample_size,),
        ).fetchone()
        checked_count = int(row["checked_count"] if row else 0)
        missing_refs = int(row["mismatch_count"] if row else 0)
        latest_computed_at_ms = row["computed_at_ms"] if row else None
        bounded_models = self.conn.execute(
            """
            WITH radar_mismatch AS (
              SELECT CASE
                       WHEN count(*) <> 1 THEN 1
                       ELSE count(*) FILTER (
                         WHERE NOT singleton_key
                            OR schema_version <> 'token_radar_snapshot_v4'
                            OR (
                              latest_attempt_status = 'ready'
                              AND state_fingerprint IS NULL
                            )
                            OR (
                              state_fingerprint IS NOT NULL
                              AND (
                                NULLIF(btrim(ruleset_version), '') IS NULL
                                OR ruleset_fingerprint !~ '^sha256:[0-9a-f]{64}$'
                              )
                            )
                            OR served_payload ->> 'schema_version'
                                 <> 'token_radar_snapshot_v4'
                            OR jsonb_typeof(served_payload -> 'items') <> 'array'
                            OR jsonb_array_length(served_payload -> 'items') > 50
                            OR COALESCE((served_payload ->> 'eligible_total')::bigint, -1)
                                 < jsonb_array_length(served_payload -> 'items')
                       )::integer
                     END AS count
              FROM token_radar_current
            ),
            brief_mismatch AS (
              SELECT count(*)::integer AS count
              FROM news_brief_selection_current current
              WHERE NOT current.singleton_key
                 OR current.selection_fingerprint !~ '^[0-9a-f]{64}$'
                 OR jsonb_typeof(current.top_stories) <> 'array'
                 OR jsonb_array_length(current.top_stories) > 8
                 OR jsonb_typeof(current.selection_stats) <> 'object'
            ),
            brief_current_mismatch AS (
              SELECT CASE
                       WHEN count(*) <> 1 THEN 1
                       ELSE count(*) FILTER (
                         WHERE NOT singleton_key
                            OR slot_status NOT IN ('due', 'running', 'completed')
                            OR (
                              active_selection IS NOT NULL
                              AND jsonb_typeof(active_selection) <> 'object'
                            )
                            OR (
                              served_payload IS NOT NULL
                              AND jsonb_typeof(served_payload) <> 'object'
                            )
                       )::integer
                     END AS count
                FROM news_brief_current
            )
            SELECT
              (SELECT count FROM radar_mismatch)
                AS token_radar_current_mismatch,
              (SELECT count FROM brief_mismatch)
                AS news_brief_selection_snapshot_mismatch,
              (SELECT count FROM brief_current_mismatch)
                AS news_brief_current_mismatch
            """
        ).fetchone()
        bounded_checks = {str(name): int(value or 0) for name, value in dict(bounded_models or {}).items()}
        mismatch_count = missing_refs + sum(bounded_checks.values())
        status = "ready" if latest_computed_at_ms is not None else "projection_missing"
        return {
            "ok": mismatch_count == 0,
            "status": status,
            "sample": sample_size,
            "checked_count": checked_count,
            "mismatch_count": mismatch_count,
            "checks": {
                "token_radar_current_missing_refs": missing_refs,
                **bounded_checks,
            },
        }


def _json_plan(rows: list[Any]) -> Any:
    if not rows:
        return []
    row = rows[0]
    if isinstance(row, dict):
        value: Any = row.get("QUERY PLAN") or row.get("?column?") or next(iter(row.values()), [])
    else:
        value = row[0]
    if isinstance(value, str):
        return [{"Plan": {"Node Type": value}}]
    return value


def _plan_metrics(
    plan_payload: Any,
    *,
    amplification_basis: str = "returned_rows",
) -> dict[str, Any]:
    statement = _plan_statement(plan_payload)
    root = statement.get("Plan")
    if not isinstance(root, dict):
        return {
            "plan_json_valid": False,
            "execution_time_ms": None,
            "planning_time_ms": None,
            "returned_rows": 0,
            "read_rows": 0,
            "amplification_basis": amplification_basis,
            "amplification_basis_rows": 0,
            "read_return_amplification": 0.0,
            "temp_read_blocks": 0,
            "temp_written_blocks": 0,
            "large_seq_scans": [],
        }
    nodes = list(_walk_plan_nodes(root))
    returned_rows = _executed_rows(root)
    relation_scans = [
        node for node in nodes if node.get("Relation Name") and "Scan" in str(node.get("Node Type") or "")
    ]
    read_rows = sum(_executed_rows(node) for node in relation_scans)
    basis_rows = _amplification_basis_rows(
        root,
        nodes,
        amplification_basis=amplification_basis,
    )
    denominator = max(1, basis_rows)
    amplification = read_rows / denominator
    large_seq_scans = [
        {
            "relation": str(node.get("Relation Name") or ""),
            "plan_rows": int(node.get("Plan Rows") or 0),
            "actual_rows": _executed_rows(node),
        }
        for node in nodes
        if str(node.get("Node Type") or "") == "Seq Scan"
        and int(node.get("Plan Rows") or 0) >= LARGE_SEQ_SCAN_PLAN_ROWS
    ]
    return {
        "plan_json_valid": True,
        "execution_time_ms": _optional_float(statement.get("Execution Time")),
        "planning_time_ms": _optional_float(statement.get("Planning Time")),
        "returned_rows": returned_rows,
        "read_rows": read_rows,
        "amplification_basis": amplification_basis,
        "amplification_basis_rows": basis_rows,
        "read_return_amplification": round(amplification, 6),
        "temp_read_blocks": int(root.get("Temp Read Blocks") or 0),
        "temp_written_blocks": int(root.get("Temp Written Blocks") or 0),
        "large_seq_scans": large_seq_scans,
    }


def _amplification_basis_rows(
    root: dict[str, Any],
    nodes: list[dict[str, Any]],
    *,
    amplification_basis: str,
) -> int:
    returned_rows = _executed_rows(root)
    if amplification_basis == "returned_rows":
        return returned_rows
    if amplification_basis != "aggregate_input":
        raise ValueError(f"unsupported amplification basis: {amplification_basis}")
    aggregate_input_rows = max(
        (
            sum(_executed_rows(child) for child in node.get("Plans") or () if isinstance(child, dict))
            for node in nodes
            if "Aggregate" in str(node.get("Node Type") or "")
        ),
        default=0,
    )
    return aggregate_input_rows or returned_rows


def _plan_violations(metrics: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    if not bool(metrics["plan_json_valid"]):
        violations.append("plan_json_missing")
    if metrics["large_seq_scans"]:
        violations.append("unexpected_large_table_seq_scan")
    if int(metrics["temp_read_blocks"]) or int(metrics["temp_written_blocks"]):
        violations.append("temp_spill")
    if float(metrics["read_return_amplification"]) > MAX_READ_RETURN_AMPLIFICATION:
        violations.append("read_return_amplification_exceeded")
    return violations


def _plan_statement(plan_payload: Any) -> dict[str, Any]:
    if isinstance(plan_payload, list) and plan_payload and isinstance(plan_payload[0], dict):
        return plan_payload[0]
    if isinstance(plan_payload, dict):
        return plan_payload
    return {}


def _walk_plan_nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans") or ():
        if isinstance(child, dict):
            yield from _walk_plan_nodes(child)


def _executed_rows(node: dict[str, Any]) -> int:
    return int(node.get("Actual Rows") or 0) * int(node.get("Actual Loops") or 0)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
