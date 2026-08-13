from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from tracefold.platform.postgres.postgres_migrations import latest_migration_version
from tracefold.platform.validation import require_nonnegative_int

SEARCH_CUTOFF_AT_MS_PARAM = "search_cutoff_at_ms"
SEARCH_AUDIT_WINDOW_MS = 24 * 60 * 60 * 1000
MAX_READ_RETURN_AMPLIFICATION = 20.0
LARGE_SEQ_SCAN_PLAN_ROWS = 10_000

AmplificationBasis = Literal["returned_rows", "aggregate_input"]


@dataclass(frozen=True, slots=True)
class ReadQuerySpec:
    """One already-bound read statement owned by a runtime query module."""

    name: str
    sql: str
    params: Any = ()
    amplification_basis: AmplificationBasis = "returned_rows"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("query audit name must not be empty")
        if not self.sql.strip():
            raise ValueError(f"query audit SQL must not be empty: {self.name}")


@dataclass(frozen=True, slots=True)
class QueryAuditCatalog:
    """The complete query and public-route manifest supplied by app composition."""

    queries: tuple[ReadQuerySpec, ...]
    query_routes: dict[str, tuple[str, ...]]
    no_sql_routes: frozenset[str]

    def __post_init__(self) -> None:
        names = [query.name for query in self.queries]
        if len(names) != len(set(names)):
            raise ValueError("query audit names must be unique")


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
    "news_opennews_incidents",
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
_POSTGRES_QUERY_TEMPLATES: tuple[dict[str, Any], ...] = (
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


def postgres_query_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
    """Return platform-owned reads with all clock parameters already bound."""

    search_cutoff_at_ms = int(now_ms) - SEARCH_AUDIT_WINDOW_MS
    specs: list[ReadQuerySpec] = []
    for template in _POSTGRES_QUERY_TEMPLATES:
        params = template["params"]
        if isinstance(params, dict):
            params = dict(params)
            if SEARCH_CUTOFF_AT_MS_PARAM in params:
                params[SEARCH_CUTOFF_AT_MS_PARAM] = search_cutoff_at_ms
        specs.append(
            ReadQuerySpec(
                name=str(template["name"]),
                sql=str(template["sql"]),
                params=params,
            )
        )
    return tuple(specs)


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
        catalog: QueryAuditCatalog,
    ):
        self.conn = conn
        self.catalog = catalog

    def run(self, *, analyze: bool = False) -> dict[str, Any]:
        queries = [self._explain(query, analyze=analyze) for query in self.catalog.queries]
        audited_names = {query.name for query in self.catalog.queries}
        missing_query_names = sorted(
            {
                query_name
                for query_names in self.catalog.query_routes.values()
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
                "query_routes": self.catalog.query_routes,
                "no_sql_routes": sorted(self.catalog.no_sql_routes),
                "missing_query_names": missing_query_names,
            },
            "queries": queries,
        }

    def _explain(self, query: ReadQuerySpec, *, analyze: bool) -> dict[str, Any]:
        prefix = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)" if analyze else "EXPLAIN (FORMAT JSON)"
        try:
            rows = self.conn.execute(f"{prefix} {query.sql}", query.params).fetchall()
            plan = _json_plan(rows)
            metrics = (
                _plan_metrics(
                    plan,
                    amplification_basis=query.amplification_basis,
                )
                if analyze
                else None
            )
            violations = _plan_violations(metrics) if metrics is not None else []
            return {
                "ok": not violations,
                "name": query.name,
                "plan": plan,
                "metrics": metrics,
                "violations": violations,
            }
        except Exception as exc:
            return {
                "ok": False,
                "name": query.name,
                "error": type(exc).__name__,
                "detail": str(exc),
                "plan": [],
                "metrics": None,
                "violations": ["explain_failed"],
            }


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
