from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from tracefold.platform.postgres.postgres_migrations import latest_migration_version
from tracefold.platform.validation import require_nonnegative_int

TOKEN_RADAR_PROJECTION_VERSION_PARAM = "token_radar_projection_version"
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
)

FOREIGN_KEY_CHECKS = {
    "event_entities_missing_events": """
        SELECT COUNT(*) AS count
        FROM event_entities child
        LEFT JOIN events parent ON parent.event_id = child.event_id
        WHERE parent.event_id IS NULL
    """,
    "token_evidence_missing_events": """
        SELECT COUNT(*) AS count
        FROM token_evidence child
        LEFT JOIN events parent ON parent.event_id = child.event_id
        WHERE parent.event_id IS NULL
    """,
    "token_intents_missing_events": """
        SELECT COUNT(*) AS count
        FROM token_intents child
        LEFT JOIN events parent ON parent.event_id = child.event_id
        WHERE parent.event_id IS NULL
    """,
    "token_resolutions_missing_intents": """
        SELECT COUNT(*) AS count
        FROM token_intent_resolutions child
        LEFT JOIN token_intents parent ON parent.intent_id = child.intent_id
        WHERE parent.intent_id IS NULL
    """,
    "token_radar_current_rows_missing_intents": """
        SELECT COUNT(*) AS count
        FROM token_radar_current_rows child
        LEFT JOIN token_intents parent ON parent.intent_id = child.intent_id
        WHERE child.venue = 'all'
          AND parent.intent_id IS NULL
    """,
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
                websearch_to_tsquery('simple', %s) AS simple_q,
                websearch_to_tsquery('english', %s) AS english_q
            )
            SELECT e.event_id
            FROM events e, query
            WHERE e.search_tsv @@ query.simple_q
               OR e.search_tsv @@ query.english_q
            ORDER BY
              (
                ts_rank_cd(e.search_tsv, query.simple_q)
                + ts_rank_cd(e.search_tsv, query.english_q)
              ) DESC,
              e.received_at_ms DESC,
              e.event_id DESC
            LIMIT 20
        """,
        "params": ("pepe", "pepe"),
    },
    {
        "name": "search_v2_trigram",
        "sql": """
            SELECT event_id
            FROM events
            WHERE search_text %% %s
            ORDER BY similarity(search_text, %s) DESC, received_at_ms DESC, event_id DESC
            LIMIT 20
        """,
        "params": ("pepe", "pepe"),
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
            SELECT tir.target_id, count(*) AS mentions,
                   max(events.received_at_ms) AS latest_seen_ms
            FROM events
            JOIN token_intents intents ON intents.event_id = events.event_id
            JOIN token_intent_resolutions tir
              ON tir.intent_id = intents.intent_id
             AND tir.is_current
             AND tir.target_type = 'MarketInstrument'
            WHERE events.received_at_ms >= %s
              AND events.received_at_ms <= %s
            GROUP BY tir.target_id
            ORDER BY mentions DESC, latest_seen_ms DESC, tir.target_id
            LIMIT 100
        """,
        "params": (0, 9_999_999_999_999),
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
            JOIN news_sources sources
              ON sources.source_id = stories.representative_source_id
            WHERE stories.active
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
            SELECT category, level, count(*) AS story_count
            FROM news_stories
            WHERE active
            GROUP BY GROUPING SETS ((category), (level))
            ORDER BY story_count DESC, category, level
            LIMIT 101
        """,
        "params": (),
    },
    {
        "name": "news_feed_source_facets",
        "sql": """
            SELECT sources.source_id, count(DISTINCT members.story_id) AS story_count
            FROM news_story_members members
            JOIN news_stories stories ON stories.story_id = members.story_id
            JOIN news_items items ON items.item_id = members.item_id
            JOIN news_sources sources ON sources.source_id = items.source_id
            WHERE stories.active
              AND members.current
            GROUP BY sources.source_id, sources.name
            ORDER BY story_count DESC, sources.name, sources.source_id
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
            ORDER BY members.current DESC,
                     items.published_at_ms DESC,
                     items.item_id
            LIMIT 101
        """,
        "params": ("audit-missing-story",),
    },
    {
        "name": "news_brief",
        "sql": """
            WITH candidates AS (
              SELECT stories.story_id, stories.importance_score,
                     stories.last_published_at_ms,
                     row_number() OVER (
                       PARTITION BY stories.representative_source_id
                       ORDER BY stories.importance_score DESC,
                                stories.last_published_at_ms DESC,
                                stories.story_id
                     ) AS source_rank
              FROM news_stories stories
              JOIN news_sources sources
                ON sources.source_id = stories.representative_source_id
              JOIN news_items items
                ON items.item_id = stories.representative_item_id
              WHERE stories.active
                AND sources.enabled
                AND NOT items.brief_excluded
            ),
            selected AS (
              SELECT story_id
              FROM candidates
              WHERE source_rank <= 3
              ORDER BY importance_score DESC, last_published_at_ms DESC, story_id
              LIMIT 8
            )
            SELECT current.publication_id, count(selected.story_id) AS candidate_count
            FROM news_brief_current current
            LEFT JOIN selected ON true
            WHERE current.singleton_key
            GROUP BY current.publication_id
        """,
        "params": (),
    },
    {
        "name": "news_sources",
        "sql": """
            SELECT sources.source_id, fetches.fetch_id
            FROM news_sources sources
            LEFT JOIN LATERAL (
              SELECT fetch_id
              FROM news_source_fetches
              WHERE source_id = sources.source_id
              ORDER BY finished_at_ms DESC, fetch_id DESC
              LIMIT 1
            ) fetches ON true
            WHERE sources.enabled
            ORDER BY sources.tier, sources.name, sources.source_id
            LIMIT 101
        """,
        "params": (),
    },
    {
        "name": "news_status",
        "sql": """
            SELECT count(*) FILTER (WHERE active) AS active_count,
                   max(last_published_at_ms) FILTER (WHERE active) AS newest_story_at_ms
            FROM news_stories
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
        "name": "macro_thesis_state",
        "sql": """
            SELECT runs.session_date, runs.status, publications.publication_id
            FROM macro_thesis_runs runs
            LEFT JOIN macro_thesis_publications publications USING (session_date)
            ORDER BY runs.session_date DESC
            LIMIT 1
        """,
        "params": (),
    },
    {
        "name": "macro_thesis_history",
        "sql": """
            SELECT publication_id, session_date
            FROM macro_thesis_publications
            ORDER BY session_date DESC
            LIMIT 30
        """,
        "params": (),
    },
    {
        "name": "worker_runtime_status",
        "sql": """
            SELECT unit_name, heartbeat_at_ms
            FROM worker_runtime_status
            ORDER BY unit_name
            LIMIT 15
        """,
        "params": (),
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
    "/api/status": ("worker_runtime_status",),
    "/api/recent": ("recent_all", "events_by_ids"),
    "/api/events/by-ids": ("events_by_ids",),
    "/api/token-radar": ("token_radar_latest", "token_profile_target"),
    "/api/stocks-radar": ("stocks_radar_recent",),
    "/api/live-market": ("live_market_current",),
    "/api/search": ("search_v2_lexical", "search_v2_trigram"),
    "/api/search/inspect": (
        "search_v2_lexical",
        "search_v2_trigram",
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
    "/api/macro/overview": (
        "macro_modules_current",
        "macro_thesis_state",
    ),
    "/api/macro/rates-fed": (
        "macro_module_current",
        "macro_modules_current",
        "macro_thesis_state",
    ),
    "/api/macro/economy-inflation": (
        "macro_module_current",
        "macro_modules_current",
        "macro_thesis_state",
    ),
    "/api/macro/liquidity-funding": (
        "macro_module_current",
        "macro_modules_current",
        "macro_thesis_state",
    ),
    "/api/macro/credit": (
        "macro_module_current",
        "macro_modules_current",
        "macro_thesis_state",
    ),
    "/api/macro/volatility": (
        "macro_module_current",
        "macro_modules_current",
        "macro_thesis_state",
    ),
    "/api/macro/cross-asset": (
        "macro_module_current",
        "macro_modules_current",
        "macro_thesis_state",
    ),
    "/api/macro/research": (
        "macro_modules_current",
        "macro_thesis_state",
        "macro_thesis_history",
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
        checks: dict[str, int] = {}
        for name, sql in FOREIGN_KEY_CHECKS.items():
            row = self.conn.execute(sql).fetchone()
            checks[name] = int(row["count"] if row else 0)
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
    ):
        self.conn = conn
        self.token_radar_projection_version = token_radar_projection_version

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
        status = "ready" if latest_computed_at_ms is not None else "projection_missing"
        return {
            "ok": missing_refs == 0,
            "status": status,
            "sample": sample_size,
            "checked_count": checked_count,
            "mismatch_count": missing_refs,
            "checks": {
                "token_radar_current_rows_missing_refs": missing_refs,
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
    leaf_nodes = [node for node in nodes if not node.get("Plans")]
    read_rows = sum(_executed_rows(node) for node in leaf_nodes)
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
