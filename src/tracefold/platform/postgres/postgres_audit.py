from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from tracefold.platform.postgres.postgres_migrations import latest_migration_version
from tracefold.platform.validation import require_nonnegative_int

TOKEN_RADAR_PROJECTION_VERSION_PARAM = "token_radar_projection_version"
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
    "token_radar_current_rows",
    "token_radar_publication_state",
    "token_radar_target_first_seen",
)

PROJECTION_TABLES = (
    "token_radar_current_rows",
    "token_radar_publication_state",
    "stock_attention_target_features",
    "stocks_radar_current_rows",
    "stocks_radar_publication_state",
    "news_projection_summary",
    "news_story_facet_counts",
    "news_source_facet_counts",
    "news_brief_selection_current",
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
TOKEN_RADAR_ORPHAN_CHECK = """
    SELECT COUNT(*) AS count
    FROM token_radar_current_rows child
    LEFT JOIN token_intents parent ON parent.intent_id = child.intent_id
    WHERE child.venue = 'all'
      AND parent.intent_id IS NULL
"""


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
            WITH ranked AS (
              SELECT current_rows.row_id, current_rows.lane, current_rows.rank,
                     row_number() OVER (
                       PARTITION BY current_rows.lane
                       ORDER BY current_rows.rank
                     ) AS lane_rank
              FROM token_radar_current_rows current_rows
              JOIN token_radar_publication_state state
                ON state.projection_version = current_rows.projection_version
               AND state."window" = current_rows."window"
               AND state.venue = current_rows.venue
              WHERE current_rows.projection_version = %(token_radar_projection_version)s
                AND current_rows."window" = '5m'
                AND current_rows.venue = 'all'
                AND state.current_generation_id IS NOT NULL
            )
            SELECT row_id
            FROM ranked
            WHERE lane_rank <= 50
            ORDER BY lane DESC, rank
            LIMIT 100
        """,
        "params": {TOKEN_RADAR_PROJECTION_VERSION_PARAM: None},
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
        "name": "stocks_radar_recent",
        "sql": """
            SELECT target_id, mentions, latest_seen_ms
            FROM stocks_radar_current_rows
            WHERE window_key = %s
            ORDER BY rank
            LIMIT 100
        """,
        "params": ("1h",),
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
        "name": "news_feed_story_facets",
        "sql": """
            SELECT facet_type, facet_value, story_count
            FROM news_story_facet_counts
            ORDER BY facet_type, story_count DESC, facet_value
            LIMIT 101
        """,
        "params": (),
    },
    {
        "name": "news_feed_source_facets",
        "sql": """
            SELECT sources.source_id, facets.story_count
            FROM news_source_facet_counts facets
            JOIN news_sources sources ON sources.source_id = facets.source_id
            ORDER BY facets.story_count DESC, sources.name, sources.source_id
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
            SELECT current.publication_id, selection.story_id
            FROM news_brief_current current
            LEFT JOIN news_brief_selection_current selection ON true
            WHERE current.singleton_key
            ORDER BY selection.rank
            LIMIT 8
        """,
        "params": (),
    },
    {
        "name": "news_sources",
        "sql": """
            SELECT source_id, live_connected, last_recovery_at_ms,
                   gap_unclosed, last_error
            FROM news_sources
            WHERE enabled AND source_kind = 'opennews'
            ORDER BY source_id
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
    "/api/token-radar": ("token_radar_latest", "token_profile_target"),
    "/api/stocks-radar": ("stocks_radar_recent",),
    "/api/live-market": ("live_market_current",),
    "/api/search": ("search_v2_lexical", "search_v2_substring"),
    "/api/search/inspect": (
        "search_v2_lexical",
        "search_v2_substring",
        "token_profile_target",
    ),
    "/api/token-case": (
        "token_profile_target",
        "token_radar_latest",
        "target_posts_recent",
    ),
    "/api/target-posts": ("target_posts_recent",),
    "/api/target-social-timeline": ("target_posts_recent",),
    "/api/news/feed": (
        "news_feed",
        "news_feed_story_facets",
        "news_feed_source_facets",
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
        foreign_key_checks = self._foreign_key_checks()
        migration_version = self._migration_version()
        migration_ready = migration_version == self.expected_migration_version
        orphan_count = sum(int(value) for value in foreign_key_checks.values())
        return {
            "ok": migration_ready and orphan_count == 0 and all(projection_schema.values()),
            "engine": "postgresql",
            "migration_version": migration_version,
            "expected_migration_version": self.expected_migration_version,
            "migration_status": "ready" if migration_ready else "stale",
            "counts": counts,
            "projection_schema": projection_schema,
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
        row = self.conn.execute(TOKEN_RADAR_ORPHAN_CHECK).fetchone()
        checks["token_radar_current_rows_missing_intents"] = int(row["count"] if row else 0)
        return checks

    def _migration_version(self) -> str | None:
        row = self.conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        return str(row["version_num"]) if row else None


class PostgresQueryAudit:
    def __init__(
        self,
        conn: Any,
        *,
        token_radar_projection_version: str | None = None,
        now_ms: int | None = None,
    ):
        self.conn = conn
        self.token_radar_projection_version = token_radar_projection_version
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
            metrics = _plan_metrics(plan) if analyze else None
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
        if TOKEN_RADAR_PROJECTION_VERSION_PARAM in bound:
            bound[TOKEN_RADAR_PROJECTION_VERSION_PARAM] = self.token_radar_projection_version
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
              SELECT row_id, intent_id, target_type, target_id
              FROM token_radar_current_rows
              WHERE venue = 'all'
              ORDER BY computed_at_ms DESC, rank ASC
              LIMIT %s
            ),
            reference_counts AS (
              SELECT
                COUNT(*) AS checked_count,
                COUNT(*) FILTER (WHERE intents.intent_id IS NULL) AS missing_intent_count,
                COUNT(*) FILTER (
                  WHERE sampled_radar_rows.target_type = 'Asset'
                    AND sampled_radar_rows.target_id IS NOT NULL
                    AND sampled_radar_rows.target_id <> ''
                    AND assets.asset_id IS NULL
                ) AS missing_asset_count
              FROM sampled_radar_rows
              LEFT JOIN token_intents AS intents
                ON intents.intent_id = sampled_radar_rows.intent_id
              LEFT JOIN registry_assets AS assets
                ON sampled_radar_rows.target_type = 'Asset'
               AND assets.asset_id = sampled_radar_rows.target_id
            ),
            latest_radar AS (
              SELECT MAX(computed_at_ms) AS computed_at_ms
              FROM token_radar_current_rows
              WHERE venue = 'all'
            )
            SELECT
              latest_radar.computed_at_ms,
              COALESCE(reference_counts.checked_count, 0) AS checked_count,
              (
                COALESCE(reference_counts.missing_intent_count, 0)
                + COALESCE(reference_counts.missing_asset_count, 0)
              ) AS mismatch_count
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
            WITH stock_expected_ranked AS (
              SELECT
                window_key,
                target_id,
                row_number() OVER (
                  PARTITION BY window_key
                  ORDER BY mentions DESC, latest_seen_ms DESC,
                           symbol, target_id
                )::integer AS expected_rank,
                state_fingerprint
              FROM stock_attention_target_features
            ),
            stock_expected AS (
              SELECT *
              FROM stock_expected_ranked
              WHERE expected_rank <= 100
            ),
            stock_mismatch AS (
              SELECT count(*)::integer AS count
              FROM stock_expected expected
              FULL OUTER JOIN stocks_radar_current_rows current
                ON current.window_key = expected.window_key
               AND current.target_id = expected.target_id
              WHERE expected.target_id IS NULL
                 OR current.target_id IS NULL
                 OR current.rank IS DISTINCT FROM expected.expected_rank
                 OR current.state_fingerprint
                      IS DISTINCT FROM expected.state_fingerprint
            ),
            story_facet_expected AS (
              SELECT 'category'::text AS facet_type,
                     category AS facet_value,
                     count(*)::integer AS story_count
              FROM news_stories
              GROUP BY category
              UNION ALL
              SELECT 'level'::text AS facet_type,
                     level AS facet_value,
                     count(*)::integer AS story_count
              FROM news_stories
              GROUP BY level
            ),
            story_facet_mismatch AS (
              SELECT count(*)::integer AS count
              FROM story_facet_expected expected
              FULL OUTER JOIN news_story_facet_counts current
                ON current.facet_type = expected.facet_type
               AND current.facet_value = expected.facet_value
              WHERE expected.facet_value IS NULL
                 OR current.facet_value IS NULL
                 OR current.story_count IS DISTINCT FROM expected.story_count
            ),
            source_facet_expected AS (
              SELECT item.source_id,
                     count(DISTINCT member.story_id)::integer AS story_count
              FROM news_story_members member
              JOIN news_stories story
                ON story.story_id = member.story_id
              JOIN news_items item
                ON item.item_id = member.item_id
              GROUP BY item.source_id
            ),
            source_facet_mismatch AS (
              SELECT count(*)::integer AS count
              FROM source_facet_expected expected
              FULL OUTER JOIN news_source_facet_counts current
                ON current.source_id = expected.source_id
              WHERE expected.source_id IS NULL
                 OR current.source_id IS NULL
                 OR current.story_count IS DISTINCT FROM expected.story_count
            ),
            brief_ranked_by_origin AS (
              SELECT story.story_id,
                     story.importance_score,
                     story.last_published_at_ms,
                     row_number() OVER (
                       PARTITION BY item.reporting_origin
                       ORDER BY story.importance_score DESC,
                                story.last_published_at_ms DESC,
                                story.story_id
                     ) AS origin_rank
                FROM news_stories story
                JOIN news_items item
                  ON item.item_id = story.representative_item_id
               WHERE NOT item.brief_excluded
            ),
            brief_candidates AS (
              SELECT story_id, importance_score, last_published_at_ms
                FROM brief_ranked_by_origin
               WHERE origin_rank <= 3
               ORDER BY importance_score DESC,
                        last_published_at_ms DESC,
                        story_id
               LIMIT 8
            ),
            brief_expected AS (
              SELECT row_number() OVER (
                       ORDER BY importance_score DESC,
                                last_published_at_ms DESC,
                                story_id
                     )::smallint AS rank,
                     story_id
              FROM brief_candidates
            ),
            brief_mismatch AS (
              SELECT count(*)::integer AS count
              FROM brief_expected expected
              FULL OUTER JOIN news_brief_selection_current current
                USING (rank)
              WHERE expected.story_id IS NULL
                 OR current.story_id IS NULL
                 OR current.story_id IS DISTINCT FROM expected.story_id
            )
            SELECT
              (SELECT count FROM stock_mismatch)
                AS stocks_radar_current_mismatch,
              (SELECT count FROM story_facet_mismatch)
                AS news_story_facet_mismatch,
              (SELECT count FROM source_facet_mismatch)
                AS news_source_facet_mismatch,
              (SELECT count FROM brief_mismatch)
                AS news_brief_selection_mismatch
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
                "token_radar_current_rows_missing_refs": missing_refs,
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


def _plan_metrics(plan_payload: Any) -> dict[str, Any]:
    statement = _plan_statement(plan_payload)
    root = statement.get("Plan")
    if not isinstance(root, dict):
        return {
            "plan_json_valid": False,
            "execution_time_ms": None,
            "planning_time_ms": None,
            "returned_rows": 0,
            "read_rows": 0,
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
    denominator = max(1, returned_rows)
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
        "read_return_amplification": round(amplification, 6),
        "temp_read_blocks": int(root.get("Temp Read Blocks") or 0),
        "temp_written_blocks": int(root.get("Temp Written Blocks") or 0),
        "large_seq_scans": large_seq_scans,
    }


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
