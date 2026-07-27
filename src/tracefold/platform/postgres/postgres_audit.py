from __future__ import annotations

from typing import Any

from tracefold.platform.postgres.postgres_migrations import latest_migration_version
from tracefold.platform.validation import require_nonnegative_int

TOKEN_RADAR_PROJECTION_VERSION_PARAM = "token_radar_projection_version"

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
            SELECT row_id
            FROM token_radar_current_rows
            WHERE projection_version = %(token_radar_projection_version)s
              AND "window" = '5m'
              AND venue = 'all'
            ORDER BY computed_at_ms DESC, lane DESC, rank ASC
            LIMIT 50
        """,
        "params": {TOKEN_RADAR_PROJECTION_VERSION_PARAM: None},
    },
    {
        "name": "target_posts_recent",
        "sql": """
            WITH latest_target AS (
              SELECT target_type, target_id
              FROM token_intent_resolutions
              WHERE is_current = true
                AND target_type IS NOT NULL
                AND target_id IS NOT NULL
              ORDER BY decision_time_ms DESC, resolution_id DESC
              LIMIT 1
            )
            SELECT tir.event_id
            FROM token_intent_resolutions tir
            JOIN latest_target
              ON latest_target.target_type = tir.target_type
             AND latest_target.target_id = tir.target_id
            WHERE tir.is_current = true
            ORDER BY tir.decision_time_ms DESC, tir.event_id DESC
            LIMIT 50
        """,
        "params": (),
    },
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
        return {
            "ok": all(item["ok"] for item in queries),
            "engine": "postgresql",
            "analyze": bool(analyze),
            "queries": queries,
        }

    def _explain(self, item: dict[str, Any], *, analyze: bool) -> dict[str, Any]:
        prefix = "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)" if analyze else "EXPLAIN (FORMAT TEXT)"
        try:
            rows = self.conn.execute(f"{prefix} {item['sql']}", self._params(item["params"])).fetchall()
            return {
                "ok": True,
                "name": item["name"],
                "plan": [_plan_line(row) for row in rows],
            }
        except Exception as exc:
            return {
                "ok": False,
                "name": item["name"],
                "error": type(exc).__name__,
                "detail": str(exc),
                "plan": [],
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


def _plan_line(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("QUERY PLAN") or row.get("?column?") or next(iter(row.values()), ""))
    return str(row[0])
