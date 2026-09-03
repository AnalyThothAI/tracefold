from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from psycopg import sql

from tracefold.platform.postgres.migrations import latest_migration_version
from tracefold.platform.validation import require_nonnegative_int

LARGE_SEQ_SCAN_PLAN_ROWS = 10_000

APPLICATION_ROLE = "tracefold"
BOOTSTRAP_ROLE = "tracefold_app"
RETIRED_APPLICATION_ROLES = (
    "tracefold_owner",
    "tracefold_serve",
    "tracefold_workers",
    "tracefold_nautilus",
)

AmplificationBasis = Literal["returned_rows", "aggregate_input"]


@dataclass(frozen=True, slots=True)
class ReadQuerySpec:
    """One already-bound read statement owned by a runtime query module."""

    name: str
    sql: str
    params: Any = ()
    amplification_basis: AmplificationBasis = "returned_rows"
    max_read_return_amplification: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("query audit name must not be empty")
        if not self.sql.strip():
            raise ValueError(f"query audit SQL must not be empty: {self.name}")
        if self.max_read_return_amplification is not None and self.max_read_return_amplification <= 0:
            raise ValueError(f"query audit amplification budget must be positive: {self.name}")


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
        missing_budgets = [query.name for query in self.queries if query.max_read_return_amplification is None]
        if missing_budgets:
            raise ValueError(f"query audit amplification budget missing: {', '.join(missing_budgets)}")


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
    "news_reviews",
    "news_external_miss_snapshots",
    "news_market_instruments",
    "news_market_instrument_listing_events",
    "news_symbol_aliases",
    "news_quote_snapshots",
    "news_event_reactions",
    "news_oi_signals",
    "news_market_liquidations",
    "news_event_evidence_snapshots",
    "news_learning_epochs",
    "news_learning_artifacts",
    "news_learning_cases",
    "news_model_recordings",
    "news_canary_activations",
    "news_agent_assignments",
    "news_agent_runtime_manifests",
    "news_learning_retention_state",
)

# #104: the Trading bounded context's own registry. Kept beside `NEWS_TABLES` rather than merged into
# it, because "exactly these tables" is a per-capability claim: a trading table appearing under the
# News heading would make the News schema audit pass for the wrong reason.
TRADING_TABLES = (
    "trading_candidate_gate_decisions",
    "trading_cases",
    "trading_trade_signals",
    "trading_operator_intents",
    "trading_execution_observations",
    "trading_execution_runtime_control_state",
    "trading_execution_runtime_state",
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
        ReadQuerySpec(
            name=str(template["name"]),
            sql=str(template["sql"]),
            params=template["params"],
            max_read_return_amplification=4.0,
        )
        for template in _POSTGRES_QUERY_TEMPLATES
    )


class PostgresOperationalAudit:
    def __init__(self, conn: Any, *, expected_migration_version: str | None = None):
        self.conn = conn
        self.expected_migration_version = expected_migration_version or latest_migration_version()

    def run(self, *, deep: bool = False) -> dict[str, Any]:
        table_names = NEWS_TABLES + TRADING_TABLES
        row_estimates = self._row_estimates(table_names)
        actual_news_tables = self._tables_with_prefix("news_")
        news_schema = {
            "expected_tables": list(NEWS_TABLES),
            "actual_tables": sorted(actual_news_tables),
            "exact": actual_news_tables == set(NEWS_TABLES),
        }
        actual_trading_tables = self._tables_with_prefix("trading_")
        trading_schema = {
            "expected_tables": list(TRADING_TABLES),
            "actual_tables": sorted(actual_trading_tables),
            "exact": actual_trading_tables == set(TRADING_TABLES),
        }
        migration_version = self._migration_version()
        migration_ready = migration_version == self.expected_migration_version
        database_identity = self._database_identity()
        result = {
            "ok": (
                migration_ready
                and bool(database_identity["ok"])
                and all(count >= 0 for count in row_estimates.values())
                and bool(news_schema["exact"])
                and bool(trading_schema["exact"])
            ),
            "engine": "postgresql",
            "mode": "deep" if deep else "fast",
            "database_identity": database_identity,
            "migration_version": migration_version,
            "expected_migration_version": self.expected_migration_version,
            "migration_status": "ready" if migration_ready else "stale",
            "row_estimates": row_estimates,
            "news_schema": news_schema,
            "trading_schema": trading_schema,
        }
        if deep:
            counts = self._counts(table_names)
            result["counts"] = counts
            result["ok"] = bool(result["ok"]) and all(count >= 0 for count in counts.values())
        return result

    def _database_identity(self) -> dict[str, Any]:
        settings = self.conn.execute(
            """
            SELECT current_setting('server_version_num') AS server_version_num,
                   current_user AS current_user,
                   current_setting('transaction_isolation') AS transaction_isolation,
                   current_setting('statement_timeout') AS statement_timeout,
                   current_setting('lock_timeout') AS lock_timeout,
                   current_setting('idle_in_transaction_session_timeout') AS idle_in_transaction_session_timeout,
                   current_setting('jit') AS jit
            """
        ).fetchone()
        extensions = self.conn.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname").fetchall()
        role_names = (APPLICATION_ROLE, BOOTSTRAP_ROLE, *RETIRED_APPLICATION_ROLES)
        role_rows = self.conn.execute(
            """
            SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin,
                   rolreplication, rolbypassrls
              FROM pg_roles
             WHERE rolname = ANY(%s)
             ORDER BY rolname
            """,
            (list(role_names),),
        ).fetchall()
        roles = {
            str(row["rolname"]): {
                "superuser": bool(row["rolsuper"]),
                "create_database": bool(row["rolcreatedb"]),
                "create_role": bool(row["rolcreaterole"]),
                "login": bool(row["rolcanlogin"]),
                "replication": bool(row["rolreplication"]),
                "bypass_rls": bool(row["rolbypassrls"]),
            }
            for row in role_rows
        }
        application_role = roles.get(APPLICATION_ROLE)
        bootstrap_role = roles.get(BOOTSTRAP_ROLE)
        retired_roles_present = sorted(set(roles) & set(RETIRED_APPLICATION_ROLES))
        schema_owner_row = self.conn.execute(
            """
            SELECT pg_get_userbyid(nspowner) AS owner
              FROM pg_namespace
             WHERE nspname = 'public'
            """
        ).fetchone()
        public_schema_owner = str(schema_owner_row["owner"]) if schema_owner_row else None
        unexpected_owner_rows = self.conn.execute(
            """
            WITH application_objects(kind, identity, owner) AS (
              SELECT 'relation', relation.relname, pg_get_userbyid(relation.relowner)
                FROM pg_class relation
                JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
               WHERE namespace.nspname = 'public'
                 AND relation.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
                 AND NOT EXISTS (
                   SELECT 1
                     FROM pg_depend dependency
                    WHERE dependency.classid = 'pg_class'::regclass
                      AND dependency.objid = relation.oid
                      AND dependency.refclassid = 'pg_extension'::regclass
                      AND dependency.deptype = 'e'
                 )
              UNION ALL
              SELECT 'routine', procedure.oid::regprocedure::text, pg_get_userbyid(procedure.proowner)
                FROM pg_proc procedure
                JOIN pg_namespace namespace ON namespace.oid = procedure.pronamespace
               WHERE namespace.nspname = 'public'
                 AND NOT EXISTS (
                   SELECT 1
                     FROM pg_depend dependency
                    WHERE dependency.classid = 'pg_proc'::regclass
                      AND dependency.objid = procedure.oid
                      AND dependency.refclassid = 'pg_extension'::regclass
                      AND dependency.deptype = 'e'
                 )
            )
            SELECT kind, identity, owner
              FROM application_objects
             WHERE owner <> %s
             ORDER BY kind, identity
            """,
            (APPLICATION_ROLE,),
        ).fetchall()
        unexpected_application_object_owners = [dict(row) for row in unexpected_owner_rows]
        server_version_num = int(settings["server_version_num"])
        extension_versions = {str(row["extname"]): str(row["extversion"]) for row in extensions}
        setting_names = {
            "transaction_isolation",
            "statement_timeout",
            "lock_timeout",
            "idle_in_transaction_session_timeout",
            "jit",
        }
        checks = {
            "production_major": server_version_num // 10_000 == 18,
            "plpgsql_available": "plpgsql" in extension_versions,
            "session_settings_reported": all(settings[name] is not None for name in setting_names),
            "application_session": settings["current_user"] == APPLICATION_ROLE,
            "application_role_exact": application_role
            == {
                "superuser": False,
                "create_database": False,
                "create_role": False,
                "login": True,
                "replication": False,
                "bypass_rls": False,
            },
            "bootstrap_role_no_login_superuser": bool(
                bootstrap_role and bootstrap_role["superuser"] and not bootstrap_role["login"]
            ),
            "retired_roles_absent": not retired_roles_present,
            "public_schema_owned_by_application": public_schema_owner == APPLICATION_ROLE,
            "application_objects_owned_by_application": not unexpected_application_object_owners,
        }
        return {
            "ok": all(checks.values()),
            "checks": checks,
            "server_version_num": server_version_num,
            "declared_image_identity": os.environ.get("TRACEFOLD_POSTGRES_IMAGE", "unreported"),
            "image_identity_source": "TRACEFOLD_POSTGRES_IMAGE",
            "extensions": extension_versions,
            "settings": {key: str(settings[key]) for key in setting_names},
            "current_user": str(settings["current_user"]),
            "role_catalog": {
                "roles": roles,
                "retired_roles_present": retired_roles_present,
            },
            "ownership": {
                "public_schema_owner": public_schema_owner,
                "unexpected_application_object_owners": unexpected_application_object_owners,
            },
        }

    def _row_estimates(self, table_names: tuple[str, ...]) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT relname, greatest(n_live_tup, 0)::bigint AS row_estimate
              FROM pg_stat_user_tables
             WHERE schemaname = 'public' AND relname = ANY(%s)
            """,
            (list(table_names),),
        ).fetchall()
        estimates = {str(row["relname"]): int(row["row_estimate"]) for row in rows}
        return {table_name: estimates.get(table_name, -1) for table_name in table_names}

    def _counts(self, table_names: tuple[str, ...]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table_name in table_names:
            if not self._table_exists(table_name):
                counts[table_name] = -1
                continue
            row = self.conn.execute(
                sql.SQL("SELECT COUNT(*) AS count FROM {}").format(sql.Identifier(table_name))
            ).fetchone()
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

    def _tables_with_prefix(self, prefix: str) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
              AND left(table_name, %s) = %s
            """,
            (len(prefix), prefix),
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
            budget = query.max_read_return_amplification
            if budget is None:  # pragma: no cover - QueryAuditCatalog rejects this at composition
                raise RuntimeError("query_audit_amplification_budget_missing")
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
            violations = (
                _plan_violations(
                    metrics,
                    max_read_return_amplification=budget,
                )
                if metrics is not None
                else []
            )
            return {
                "ok": not violations,
                "name": query.name,
                "plan": plan,
                "metrics": metrics,
                "budget": {
                    "max_read_return_amplification": budget,
                },
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
                       )::integer
                     END AS count
                FROM news_ingest_state
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


def _plan_violations(
    metrics: dict[str, Any],
    *,
    max_read_return_amplification: float,
) -> list[str]:
    violations: list[str] = []
    if not bool(metrics["plan_json_valid"]):
        violations.append("plan_json_missing")
    if metrics["large_seq_scans"]:
        violations.append("unexpected_large_table_seq_scan")
    if int(metrics["temp_read_blocks"]) or int(metrics["temp_written_blocks"]):
        violations.append("temp_spill")
    if float(metrics["read_return_amplification"]) > max_read_return_amplification:
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
