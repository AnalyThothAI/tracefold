from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from tracefold.platform.postgres.postgres_migrations import latest_migration_version
from tracefold.platform.postgres.runtime_roles import runtime_role_contract
from tracefold.platform.validation import require_nonnegative_int

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
    write_routes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        names = [query.name for query in self.queries]
        if len(names) != len(set(names)):
            raise ValueError("query audit names must be unique")


NEWS_TABLES = (
    "news_ingest_state",
    "news_opennews_incidents",
    "news_items",
    "news_events",
    "news_event_members",
    "news_event_bands",
    "news_event_assets",
    "news_verdicts",
    "news_deliveries",
    "news_control_state",
    "news_reviews",
    "news_external_miss_snapshots",
    "news_market_instruments",
    "news_symbol_aliases",
    "news_quote_snapshots",
    "news_event_reactions",
    "news_event_evidence_snapshots",
    "news_learning_artifacts",
    "news_learning_cases",
    "news_model_recordings",
    "news_canary_activations",
    "news_agent_assignments",
    "news_agent_runtime_manifests",
    "news_learning_retention_state",
)

_POSTGRES_QUERY_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "name": "readiness_schema",
        "sql": "SELECT version_num FROM alembic_version LIMIT 1",
        "params": (),
    },
)


def postgres_query_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
    """Return platform-owned reads (no clock parameters remain, ``now_ms`` keeps the catalog contract)."""

    del now_ms
    return tuple(
        ReadQuerySpec(name=str(template["name"]), sql=str(template["sql"]), params=template["params"])
        for template in _POSTGRES_QUERY_TEMPLATES
    )


class PostgresOperationalAudit:
    def __init__(self, conn: Any, *, expected_migration_version: str | None = None):
        self.conn = conn
        self.expected_migration_version = expected_migration_version or latest_migration_version()

    def run(self) -> dict[str, Any]:
        counts = self._counts(NEWS_TABLES)
        actual_news_tables = self._news_tables()
        news_schema = {
            "expected_tables": list(NEWS_TABLES),
            "actual_tables": sorted(actual_news_tables),
            "exact": actual_news_tables == set(NEWS_TABLES),
        }
        migration_version = self._migration_version()
        migration_ready = migration_version == self.expected_migration_version
        runtime_roles = runtime_role_contract(self.conn)
        return {
            "ok": (
                migration_ready
                and all(count >= 0 for count in counts.values())
                and bool(news_schema["exact"])
                and bool(runtime_roles["ok"])
            ),
            "engine": "postgresql",
            "migration_version": migration_version,
            "expected_migration_version": self.expected_migration_version,
            "migration_status": "ready" if migration_ready else "stale",
            "counts": counts,
            "news_schema": news_schema,
            "runtime_roles": runtime_roles,
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
                "write_routes": sorted(self.catalog.write_routes),
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
        bounded_models = self.conn.execute(
            """
            WITH ingest_mismatch AS (
              SELECT CASE
                       WHEN count(*) <> 1 THEN 1
                       ELSE count(*) FILTER (
                         WHERE singleton_key <> 'opennews'
                            OR (
                              provider_enabled_strategy_ids IS NOT NULL
                              AND jsonb_typeof(provider_enabled_strategy_ids) <> 'array'
                            )
                       )::integer
                     END AS count
                FROM news_ingest_state
            ),
            control_mismatch AS (
              SELECT CASE
                       WHEN count(*) <> 1 THEN 1
                       ELSE count(*) FILTER (
                         WHERE singleton_key <> 'current'
                            OR jsonb_typeof(mutes) <> 'array'
                       )::integer
                     END AS count
                FROM news_control_state
            ),
            delivery_mismatch AS (
              SELECT count(*)::integer AS count
              FROM news_deliveries
              WHERE (state = 'sending' AND settled_at_ms IS NOT NULL)
                 OR (state IN ('sent', 'terminal') AND settled_at_ms IS NULL)
                 OR (state = 'sent' AND error_code IS NOT NULL)
                 OR jsonb_typeof(card) <> 'object'
            )
            SELECT
              (SELECT count FROM ingest_mismatch) AS news_ingest_state_mismatch,
              (SELECT count FROM control_mismatch) AS news_control_state_mismatch,
              (SELECT count FROM delivery_mismatch) AS news_delivery_state_mismatch
            """
        ).fetchone()
        bounded_checks = {str(name): int(value or 0) for name, value in dict(bounded_models or {}).items()}
        mismatch_count = sum(bounded_checks.values())
        return {
            "ok": mismatch_count == 0,
            "status": "ready",
            "sample": sample_size,
            "checked_count": len(bounded_checks),
            "mismatch_count": mismatch_count,
            "checks": bounded_checks,
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
