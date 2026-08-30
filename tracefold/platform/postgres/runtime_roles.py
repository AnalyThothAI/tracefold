from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from psycopg import sql

RUNTIME_LOGIN_ROLES = (
    "tracefold_serve",
    "tracefold_workers",
    "tracefold_migrate",
    "tracefold_nautilus",
    "tracefold_onchain",
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
    migrate = by_name.get("tracefold_migrate")
    nautilus = by_name.get("tracefold_nautilus")
    onchain = by_name.get("tracefold_onchain")
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
                'SELECT'
              ) AS workers_select,
              has_table_privilege(
                'tracefold_workers',
                'public.news_events',
                'INSERT'
              ) AS workers_insert,
              has_table_privilege(
                'tracefold_workers',
                'public.news_events',
                'UPDATE'
              ) AS workers_update,
              has_table_privilege(
                'tracefold_workers',
                'public.news_events',
                'DELETE'
              ) AS workers_delete,
              has_table_privilege(
                'tracefold_workers',
                'public.news_event_evidence_snapshots',
                'SELECT'
              ) AS workers_evidence_select,
              has_table_privilege(
                'tracefold_workers',
                'public.news_event_evidence_snapshots',
                'INSERT'
              ) AS workers_evidence_insert,
              has_table_privilege(
                'tracefold_workers',
                'public.news_event_evidence_snapshots',
                'UPDATE'
              ) AS workers_evidence_update,
              has_table_privilege(
                'tracefold_workers',
                'public.news_event_evidence_snapshots',
                'DELETE'
              ) AS workers_evidence_delete,
              has_schema_privilege(
                'tracefold_workers',
                'public',
                'CREATE'
              ) AS workers_create,
              has_table_privilege(
                'tracefold_serve',
                'public.news_reviews',
                'INSERT'
              ) AS serve_review_insert,
              has_table_privilege(
                'tracefold_serve',
                'public.news_external_miss_snapshots',
                'INSERT'
              ) AS serve_external_miss_insert,
              has_table_privilege(
                'tracefold_workers',
                'public.trading_intents',
                'SELECT'
              ) AS workers_intents_select,
              has_column_privilege(
                'tracefold_workers',
                'public.trading_intents',
                'case_id',
                'INSERT'
              ) AS workers_intents_identity_insert,
              has_column_privilege(
                'tracefold_workers',
                'public.trading_intents',
                'execution_state',
                'INSERT'
              ) AS workers_intents_execution_insert,
              has_column_privilege(
                'tracefold_workers',
                'public.trading_intents',
                'execution_state',
                'UPDATE'
              ) AS workers_intents_execution_update,
              has_table_privilege(
                'tracefold_workers',
                'public.trading_orders',
                'SELECT'
              ) AS workers_legacy_orders_select,
              has_table_privilege(
                'tracefold_workers',
                'public.trading_orders',
                'INSERT, UPDATE, DELETE'
              ) AS workers_legacy_orders_write,
              has_table_privilege(
                'tracefold_workers',
                'public.trading_order_observations',
                'SELECT'
              ) AS workers_legacy_observations_select,
              has_table_privilege(
                'tracefold_workers',
                'public.trading_order_observations',
                'INSERT, UPDATE, DELETE'
              ) AS workers_legacy_observations_write,
              has_column_privilege(
                'tracefold_workers',
                'public.trading_runtime_state',
                'control',
                'UPDATE'
              ) AS workers_runtime_control_update,
              has_column_privilege(
                'tracefold_workers',
                'public.trading_runtime_state',
                'orders_today',
                'UPDATE'
              ) AS workers_runtime_legacy_counter_update,
              has_table_privilege(
                'tracefold_serve',
                'public.trading_intents',
                'SELECT'
              ) AS serve_intents_select,
              has_table_privilege(
                'tracefold_serve',
                'public.trading_intents',
                'INSERT'
              ) AS serve_intents_insert,
              has_table_privilege(
                'tracefold_nautilus',
                'public.trading_intents',
                'SELECT'
              ) AS nautilus_intents_select,
              has_table_privilege(
                'tracefold_nautilus',
                'public.trading_intents',
                'INSERT'
              ) AS nautilus_intents_insert,
              has_column_privilege(
                'tracefold_nautilus',
                'public.trading_intents',
                'execution_state',
                'UPDATE'
              ) AS nautilus_execution_update,
              has_column_privilege(
                'tracefold_nautilus',
                'public.trading_intents',
                'case_id',
                'UPDATE'
              ) AS nautilus_identity_update,
              has_table_privilege(
                'tracefold_nautilus',
                'public.trading_cases',
                'UPDATE'
              ) AS nautilus_cases_update,
              has_column_privilege(
                'tracefold_nautilus',
                'public.trading_runtime_state',
                'id',
                'SELECT'
              ) AS nautilus_runtime_id_select,
              has_column_privilege(
                'tracefold_nautilus',
                'public.trading_runtime_state',
                'control',
                'SELECT'
              ) AS nautilus_runtime_control_select,
              has_column_privilege(
                'tracefold_nautilus',
                'public.trading_runtime_state',
                'orders_today',
                'SELECT'
              ) AS nautilus_runtime_counter_select,
              has_table_privilege(
                'tracefold_nautilus',
                'public.trading_onchain_execution_intents',
                'SELECT'
              ) AS nautilus_onchain_execution_select,
              has_table_privilege(
                'tracefold_onchain',
                'public.trading_onchain_execution_intents',
                'SELECT'
              ) AS onchain_execution_select,
              has_column_privilege(
                'tracefold_onchain',
                'public.trading_onchain_execution_intents',
                'state',
                'UPDATE'
              ) AS onchain_execution_state_update,
              has_table_privilege(
                'tracefold_onchain',
                'public.trading_onchain_signed_transactions',
                'SELECT'
              ) AS onchain_signed_select,
              has_column_privilege(
                'tracefold_onchain',
                'public.trading_onchain_signed_transactions',
                'signed_transaction',
                'INSERT'
              ) AS onchain_signed_insert,
              has_column_privilege(
                'tracefold_workers',
                'public.trading_onchain_signed_transactions',
                'transaction_hash',
                'SELECT'
              ) AS workers_onchain_hash_select,
              has_column_privilege(
                'tracefold_workers',
                'public.trading_onchain_signed_transactions',
                'signed_transaction',
                'SELECT'
              ) AS workers_onchain_raw_select,
              has_table_privilege(
                'tracefold_workers',
                'public.trading_onchain_executor_runtime',
                'SELECT'
              ) AS workers_onchain_executor_runtime_select,
              has_table_privilege(
                'tracefold_nautilus',
                'public.trading_onchain_executor_runtime',
                'SELECT'
              ) AS nautilus_onchain_executor_runtime_select,
              has_table_privilege(
                'tracefold_onchain',
                'public.trading_onchain_executor_runtime',
                'SELECT'
              ) AS onchain_executor_runtime_select,
              has_column_privilege(
                'tracefold_onchain',
                'public.trading_onchain_executor_runtime',
                'id',
                'INSERT'
              ) AS onchain_executor_runtime_insert,
              has_column_privilege(
                'tracefold_onchain',
                'public.trading_onchain_executor_runtime',
                'heartbeat_at_ms',
                'UPDATE'
              ) AS onchain_executor_runtime_update,
              has_column_privilege(
                'tracefold_onchain',
                'public.trading_onchain_executor_runtime',
                'wallet_fingerprint',
                'UPDATE'
              ) AS onchain_executor_wallet_rotation_update,
              pg_has_role(
                'tracefold_migrate',
                'tracefold_owner',
                'MEMBER'
              ) AS migrate_owner_member
            """
        ).fetchone()
    )
    if expect_legacy_revoked:
        legacy_login_state = legacy is None or not bool(legacy["rolcanlogin"])
    else:
        legacy_login_state = legacy is not None and bool(legacy["rolcanlogin"])
    checks = {
        "owner_no_login": owner is not None and not bool(owner["rolcanlogin"]),
        "serve_login": serve is not None and bool(serve["rolcanlogin"]),
        "serve_read_only": serve is not None and str(serve["read_only_setting"]).endswith("=on"),
        "workers_login": workers is not None and bool(workers["rolcanlogin"]),
        "migrate_login_noinherit": (
            migrate is not None and bool(migrate["rolcanlogin"]) and not bool(migrate["rolinherit"])
        ),
        "nautilus_login": nautilus is not None and bool(nautilus["rolcanlogin"]),
        "onchain_login": onchain is not None and bool(onchain["rolcanlogin"]),
        "legacy_login_state": legacy_login_state,
        "schema_owner": bool(schema_owner_row) and str(schema_owner_row["owner"]) == "tracefold_owner",
        "serve_select": bool(privileges["serve_select"]),
        "serve_insert_denied": not bool(privileges["serve_insert"]),
        "workers_dml": all(
            bool(privileges[name]) for name in ("workers_select", "workers_insert", "workers_update", "workers_delete")
        ),
        "workers_evidence_append": bool(privileges["workers_evidence_select"])
        and bool(privileges["workers_evidence_insert"]),
        "workers_evidence_rewrite_denied": not bool(privileges["workers_evidence_update"])
        and not bool(privileges["workers_evidence_delete"]),
        "workers_create_denied": not bool(privileges["workers_create"]),
        "serve_review_append": bool(privileges["serve_review_insert"])
        and bool(privileges["serve_external_miss_insert"]),
        "workers_intents_append": bool(privileges["workers_intents_select"])
        and bool(privileges["workers_intents_identity_insert"])
        and not bool(privileges["workers_intents_execution_insert"])
        and not bool(privileges["workers_intents_execution_update"]),
        "workers_legacy_execution_read_only": bool(privileges["workers_legacy_orders_select"])
        and bool(privileges["workers_legacy_observations_select"])
        and not bool(privileges["workers_legacy_orders_write"])
        and not bool(privileges["workers_legacy_observations_write"]),
        "workers_runtime_current_columns_only": bool(privileges["workers_runtime_control_update"])
        and not bool(privileges["workers_runtime_legacy_counter_update"]),
        "serve_intents_read_only": bool(privileges["serve_intents_select"])
        and not bool(privileges["serve_intents_insert"]),
        "nautilus_intents_projection_only": bool(privileges["nautilus_intents_select"])
        and not bool(privileges["nautilus_intents_insert"])
        and bool(privileges["nautilus_execution_update"])
        and not bool(privileges["nautilus_identity_update"])
        and not bool(privileges["nautilus_cases_update"])
        and bool(privileges["nautilus_runtime_id_select"])
        and bool(privileges["nautilus_runtime_control_select"])
        and not bool(privileges["nautilus_runtime_counter_select"]),
        "onchain_execution_isolated": not bool(privileges["nautilus_onchain_execution_select"])
        and bool(privileges["onchain_execution_select"])
        and bool(privileges["onchain_execution_state_update"])
        and bool(privileges["onchain_signed_select"])
        and bool(privileges["onchain_signed_insert"])
        and bool(privileges["workers_onchain_hash_select"])
        and not bool(privileges["workers_onchain_raw_select"])
        and bool(privileges["workers_onchain_executor_runtime_select"])
        and not bool(privileges["nautilus_onchain_executor_runtime_select"])
        and bool(privileges["onchain_executor_runtime_select"])
        and bool(privileges["onchain_executor_runtime_insert"])
        and bool(privileges["onchain_executor_runtime_update"])
        and bool(privileges["onchain_executor_wallet_rotation_update"]),
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
