from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from psycopg import sql

BOOTSTRAP_ROLE = "tracefold_app"
MIGRATION_ROLE = "tracefold_owner"
STEADY_RUNTIME_ROLES = (
    "tracefold_serve",
    "tracefold_workers",
    "tracefold_nautilus",
)
RUNTIME_LOGIN_ROLES = (MIGRATION_ROLE, *STEADY_RUNTIME_ROLES)


def provision_runtime_role_passwords(
    conn: Any,
    *,
    password_files: Mapping[str, Path],
) -> None:
    """Set direct-login role passwords without placing secret values in output."""

    if set(password_files) != set(RUNTIME_LOGIN_ROLES):
        raise ValueError("runtime_role_password_file_set_invalid")
    for role in RUNTIME_LOGIN_ROLES:
        password = _read_password(password_files[role])
        conn.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(password),
            )
        )


def runtime_role_contract(conn: Any) -> dict[str, Any]:
    """Return only the business-bearing production readiness checks."""

    rows = conn.execute(
        """
        SELECT role.rolname, role.rolcanlogin, role.rolinherit, role.rolsuper,
               role.rolcreatedb, role.rolcreaterole, role.rolreplication,
               role.rolbypassrls,
               COALESCE(
                 (
                   SELECT setting
                   FROM unnest(role.rolconfig) setting
                   WHERE setting LIKE 'default_transaction_read_only=%%'
                   LIMIT 1
                 ),
                 ''
               ) AS read_only_setting
        FROM pg_roles role
        WHERE role.rolname = ANY(%s)
        ORDER BY role.rolname
        """,
        ([BOOTSTRAP_ROLE, MIGRATION_ROLE, *STEADY_RUNTIME_ROLES],),
    ).fetchall()
    by_name = {str(row["rolname"]): dict(row) for row in rows}
    bootstrap = by_name.get(BOOTSTRAP_ROLE)
    owner = by_name.get(MIGRATION_ROLE)
    serve = by_name.get("tracefold_serve")
    workers = by_name.get("tracefold_workers")
    nautilus = by_name.get("tracefold_nautilus")
    schema_owner = conn.execute(
        """
        SELECT owner.rolname AS owner
        FROM pg_namespace namespace
        JOIN pg_roles owner ON owner.oid = namespace.nspowner
        WHERE namespace.nspname = 'public'
        """
    ).fetchone()
    unexpected_owner_count = int(
        conn.execute(
            """
            SELECT (
              SELECT count(*)
              FROM pg_class object
              JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
              JOIN pg_roles owner ON owner.oid = object.relowner
              WHERE namespace.nspname = 'public'
                AND object.relkind IN ('r', 'p', 'S', 'v', 'm')
                AND owner.rolname <> 'tracefold_owner'
                AND NOT EXISTS (
                  SELECT 1 FROM pg_depend dependency
                  WHERE dependency.classid = 'pg_class'::regclass
                    AND dependency.objid = object.oid
                    AND dependency.deptype = 'e'
                )
            ) + (
              SELECT count(*)
              FROM pg_proc object
              JOIN pg_namespace namespace ON namespace.oid = object.pronamespace
              JOIN pg_roles owner ON owner.oid = object.proowner
              WHERE namespace.nspname = 'public'
                AND owner.rolname <> 'tracefold_owner'
                AND NOT EXISTS (
                  SELECT 1 FROM pg_depend dependency
                  WHERE dependency.classid = 'pg_proc'::regclass
                    AND dependency.objid = object.oid
                    AND dependency.deptype = 'e'
                )
            ) AS count
            """
        ).fetchone()["count"]
    )
    owner_default_acl_count = int(
        conn.execute(
            """
            SELECT count(*) AS count
            FROM pg_default_acl defaults
            JOIN pg_roles owner ON owner.oid = defaults.defaclrole
            WHERE owner.rolname = 'tracefold_owner'
              AND defaults.defaclnamespace = 'public'::regnamespace
              AND defaults.defaclobjtype IN ('r', 'S')
            """
        ).fetchone()["count"]
    )
    default_acl_count = int(conn.execute("SELECT count(*) AS count FROM pg_default_acl").fetchone()["count"])
    default_acl_mismatch_count = int(
        conn.execute(
            """
            WITH actual(object_type, grantor_name, grantee_name, privilege_type, is_grantable) AS (
              SELECT defaults.defaclobjtype::text,
                     grantor.rolname::text,
                     COALESCE(grantee.rolname::text, 'PUBLIC'),
                     privilege.privilege_type,
                     privilege.is_grantable
              FROM pg_default_acl defaults
              CROSS JOIN LATERAL aclexplode(defaults.defaclacl) privilege
              LEFT JOIN pg_roles grantor ON grantor.oid = privilege.grantor
              LEFT JOIN pg_roles grantee ON grantee.oid = privilege.grantee
            ),
            expected(object_type, grantor_name, grantee_name, privilege_type, is_grantable) AS (
              VALUES
                ('r', 'tracefold_owner', 'tracefold_serve', 'SELECT', false),
                ('r', 'tracefold_owner', 'tracefold_workers', 'DELETE', false),
                ('r', 'tracefold_owner', 'tracefold_workers', 'INSERT', false),
                ('r', 'tracefold_owner', 'tracefold_workers', 'SELECT', false),
                ('r', 'tracefold_owner', 'tracefold_workers', 'UPDATE', false),
                ('S', 'tracefold_owner', 'tracefold_workers', 'SELECT', false),
                ('S', 'tracefold_owner', 'tracefold_workers', 'USAGE', false)
            )
            SELECT count(*) AS count
            FROM (
              (SELECT * FROM actual EXCEPT SELECT * FROM expected)
              UNION ALL
              (SELECT * FROM expected EXCEPT SELECT * FROM actual)
            ) mismatch
            """
        ).fetchone()["count"]
    )
    privileges = dict(
        conn.execute(
            """
            SELECT
              has_table_privilege('tracefold_serve', 'public.news_events', 'SELECT') AS serve_select,
              has_table_privilege('tracefold_serve', 'public.news_events', 'INSERT') AS serve_core_insert,
              has_table_privilege('tracefold_serve', 'public.news_reviews', 'INSERT')
                AS serve_review_insert,
              has_schema_privilege('tracefold_workers', 'public', 'CREATE') AS workers_create,
              has_table_privilege(
                'tracefold_workers', 'public.news_event_evidence_snapshots', 'INSERT'
              ) AS workers_evidence_insert,
              has_table_privilege(
                'tracefold_workers', 'public.news_event_evidence_snapshots', 'UPDATE, DELETE'
              ) AS workers_evidence_rewrite,
              has_column_privilege(
                'tracefold_workers', 'public.trading_intents', 'execution_state', 'UPDATE'
              ) AS workers_execution_update,
              has_table_privilege(
                'tracefold_workers', 'public.trading_nautilus_runtime_starts', 'INSERT, UPDATE, DELETE'
              ) AS workers_nautilus_start_write,
              has_table_privilege('tracefold_nautilus', 'public.trading_intents', 'SELECT')
                AS nautilus_intents_select,
              has_column_privilege(
                'tracefold_nautilus', 'public.trading_intents', 'execution_state', 'UPDATE'
              ) AS nautilus_execution_update,
              has_column_privilege(
                'tracefold_nautilus', 'public.trading_intents', 'case_id', 'UPDATE'
              ) AS nautilus_identity_update,
              has_table_privilege('tracefold_nautilus', 'public.trading_cases', 'UPDATE')
                AS nautilus_cases_update,
              has_table_privilege(
                'tracefold_serve', to_regclass('public.trading_trade_signals'), 'SELECT'
              )
                AS serve_execution_stream_select,
              has_table_privilege(
                'tracefold_serve', to_regclass('public.trading_trade_signals'), 'INSERT'
              )
                AS serve_execution_stream_insert,
              has_table_privilege(
                'tracefold_workers', to_regclass('public.trading_trade_signals'), 'INSERT'
              )
                AS workers_signal_insert,
              has_table_privilege(
                'tracefold_workers', to_regclass('public.trading_execution_observations'), 'INSERT'
              )
                AS workers_observation_insert,
              has_table_privilege(
                'tracefold_nautilus', to_regclass('public.trading_trade_signals'), 'SELECT'
              )
                AS nautilus_signal_select,
              has_table_privilege(
                'tracefold_nautilus', to_regclass('public.trading_execution_observations'), 'INSERT'
              )
                AS nautilus_observation_insert,
              has_table_privilege(
                'tracefold_nautilus', to_regclass('public.trading_execution_profile_activations'), 'INSERT'
              ) AS nautilus_activation_insert
            """
        ).fetchone()
    )
    checks = {
        "bootstrap_no_login_superuser": bootstrap is not None
        and not bool(bootstrap["rolcanlogin"])
        and bool(bootstrap["rolsuper"]),
        "owner_direct_login": _ordinary_login(owner),
        "serve_read_boundary": serve is not None
        and _ordinary_login(serve)
        and str(serve["read_only_setting"]).endswith("=on")
        and bool(privileges["serve_select"])
        and bool(privileges["serve_review_insert"])
        and not bool(privileges["serve_core_insert"]),
        "workers_business_boundary": _ordinary_login(workers)
        and not bool(privileges["workers_create"])
        and bool(privileges["workers_evidence_insert"])
        and not bool(privileges["workers_evidence_rewrite"])
        and not bool(privileges["workers_execution_update"])
        and not bool(privileges["workers_nautilus_start_write"]),
        "nautilus_projection_boundary": _ordinary_login(nautilus)
        and bool(privileges["nautilus_intents_select"])
        and bool(privileges["nautilus_execution_update"])
        and not bool(privileges["nautilus_identity_update"])
        and not bool(privileges["nautilus_cases_update"]),
        "execution_stream_boundary": bool(privileges["serve_execution_stream_select"])
        and not bool(privileges["serve_execution_stream_insert"])
        and bool(privileges["workers_signal_insert"])
        and not bool(privileges["workers_observation_insert"])
        and bool(privileges["nautilus_signal_select"])
        and bool(privileges["nautilus_observation_insert"])
        and not bool(privileges["nautilus_activation_insert"]),
        "schema_owner": bool(schema_owner) and str(schema_owner["owner"]) == MIGRATION_ROLE,
        "application_object_ownership": unexpected_owner_count == 0,
        "owner_default_privileges": (
            default_acl_count == owner_default_acl_count == 2 and default_acl_mismatch_count == 0
        ),
    }
    failures = [name for name, passed in checks.items() if not passed]
    return {"ok": not failures, "failures": failures, "checks": checks}


def _ordinary_login(role: dict[str, Any] | None) -> bool:
    return role is not None and all(
        (
            bool(role["rolcanlogin"]),
            bool(role["rolinherit"]),
            not bool(role["rolsuper"]),
            not bool(role["rolcreatedb"]),
            not bool(role["rolcreaterole"]),
            not bool(role["rolreplication"]),
            not bool(role["rolbypassrls"]),
        )
    )


def _read_password(path: Path) -> str:
    resolved = Path(path).expanduser()
    password = resolved.read_text(encoding="utf-8").strip()
    if not password:
        raise ValueError(f"runtime_role_password_empty:{resolved.name}")
    if len(password) > 1_024:
        raise ValueError(f"runtime_role_password_oversized:{resolved.name}")
    return password


__all__ = [
    "BOOTSTRAP_ROLE",
    "MIGRATION_ROLE",
    "RUNTIME_LOGIN_ROLES",
    "STEADY_RUNTIME_ROLES",
    "provision_runtime_role_passwords",
    "runtime_role_contract",
]
