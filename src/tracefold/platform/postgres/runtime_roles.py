from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from psycopg import sql

RUNTIME_LOGIN_ROLES = (
    "tracefold_serve",
    "tracefold_workers",
    "tracefold_review",
    "tracefold_migrate",
)
LEGACY_RUNTIME_ROLE = "tracefold_app"


def provision_runtime_role_passwords(
    conn: Any,
    *,
    password_files: Mapping[str, Path],
) -> None:
    """Set runtime passwords without placing secret values in output."""

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


def runtime_role_contract(
    conn: Any,
    *,
    expect_legacy_revoked: bool = True,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT role.rolname, role.rolcanlogin, role.rolinherit,
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
        (
            [
                "tracefold_owner",
                *RUNTIME_LOGIN_ROLES,
                LEGACY_RUNTIME_ROLE,
            ],
        ),
    ).fetchall()
    by_name = {str(row["rolname"]): dict(row) for row in rows}
    owner = by_name.get("tracefold_owner")
    serve = by_name.get("tracefold_serve")
    workers = by_name.get("tracefold_workers")
    review = by_name.get("tracefold_review")
    migrate = by_name.get("tracefold_migrate")
    legacy = by_name.get(LEGACY_RUNTIME_ROLE)
    schema_owner_row = conn.execute(
        """
        SELECT owner.rolname AS owner
        FROM pg_namespace namespace
        JOIN pg_roles owner ON owner.oid = namespace.nspowner
        WHERE namespace.nspname = 'public'
        """
    ).fetchone()
    privileges = dict(
        conn.execute(
            """
            SELECT
              has_table_privilege(
                'tracefold_serve',
                'public.news_events',
                'SELECT'
              ) AS serve_select,
              has_table_privilege(
                'tracefold_serve',
                'public.news_events',
                'INSERT'
              ) AS serve_insert,
              has_table_privilege(
                'tracefold_workers',
                'public.news_events',
                'SELECT,INSERT,UPDATE,DELETE'
              ) AS workers_dml,
              has_schema_privilege(
                'tracefold_workers',
                'public',
                'CREATE'
              ) AS workers_create,
              has_table_privilege(
                'tracefold_review',
                'public.news_events',
                'SELECT'
              ) AS review_events_select,
              pg_has_role(
                'tracefold_migrate',
                'tracefold_owner',
                'MEMBER'
              ) AS migrate_owner_member
            """
        ).fetchone()
    )
    checks = {
        "owner_no_login": owner is not None and not bool(owner["rolcanlogin"]),
        "serve_login": serve is not None and bool(serve["rolcanlogin"]),
        "serve_read_only": serve is not None and str(serve["read_only_setting"]).endswith("=on"),
        "workers_login": workers is not None and bool(workers["rolcanlogin"]),
        "review_login": review is not None and bool(review["rolcanlogin"]),
        "migrate_login_noinherit": (
            migrate is not None and bool(migrate["rolcanlogin"]) and not bool(migrate["rolinherit"])
        ),
        "legacy_login_state": (legacy is not None and bool(legacy["rolcanlogin"]) == (not expect_legacy_revoked)),
        "schema_owner": bool(schema_owner_row) and str(schema_owner_row["owner"]) == "tracefold_owner",
        "serve_select": bool(privileges["serve_select"]),
        "serve_insert_denied": not bool(privileges["serve_insert"]),
        "workers_dml": bool(privileges["workers_dml"]),
        "workers_create_denied": not bool(privileges["workers_create"]),
        "review_base_select_denied": not bool(privileges["review_events_select"]),
        "migrate_owner_member": bool(privileges["migrate_owner_member"]),
    }
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "ok": not failures,
        "failures": failures,
        "checks": checks,
    }


def revoke_legacy_runtime_login(conn: Any) -> None:
    conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(LEGACY_RUNTIME_ROLE)))


def _read_password(path: Path) -> str:
    resolved = Path(path).expanduser()
    password = resolved.read_text(encoding="utf-8").strip()
    if not password:
        raise ValueError(f"runtime_role_password_empty:{resolved.name}")
    if len(password) > 1_024:
        raise ValueError(f"runtime_role_password_oversized:{resolved.name}")
    return password


__all__ = [
    "LEGACY_RUNTIME_ROLE",
    "RUNTIME_LOGIN_ROLES",
    "provision_runtime_role_passwords",
    "revoke_legacy_runtime_login",
    "runtime_role_contract",
]
