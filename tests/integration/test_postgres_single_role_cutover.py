"""One-time old-role catalog cutover without a permanent production repair path."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from psycopg import sql

from tracefold.platform.postgres.client import connect_postgres
from tracefold.platform.postgres.migrations import latest_migration_version

pytestmark = [pytest.mark.integration, pytest.mark.migration]

OLD_RUNTIME_ROLES = ("tracefold_serve", "tracefold_workers", "tracefold_nautilus")


def _schema_fingerprint(conn: Any) -> str:
    rows = conn.execute(
        """
        WITH objects(kind, identity, definition) AS (
          SELECT 'column', table_name || '.' || column_name,
                 data_type || '|' || is_nullable || '|' || coalesce(column_default, '')
            FROM information_schema.columns
           WHERE table_schema = 'public'
          UNION ALL
          SELECT 'constraint', constraint_object.relname || '.' || item.conname,
                 pg_get_constraintdef(item.oid, true)
            FROM pg_constraint item
            JOIN pg_class constraint_object ON constraint_object.oid = item.conrelid
            JOIN pg_namespace namespace ON namespace.oid = constraint_object.relnamespace
           WHERE namespace.nspname = 'public'
          UNION ALL
          SELECT 'index', index_class.relname, pg_get_indexdef(index_class.oid)
            FROM pg_index index
            JOIN pg_class index_class ON index_class.oid = index.indexrelid
            JOIN pg_namespace namespace ON namespace.oid = index_class.relnamespace
           WHERE namespace.nspname = 'public'
          UNION ALL
          SELECT 'function', procedure.oid::regprocedure::text, pg_get_functiondef(procedure.oid)
            FROM pg_proc procedure
            JOIN pg_namespace namespace ON namespace.oid = procedure.pronamespace
           WHERE namespace.nspname = 'public'
          UNION ALL
          SELECT 'trigger', table_class.relname || '.' || trigger.tgname, pg_get_triggerdef(trigger.oid, true)
            FROM pg_trigger trigger
            JOIN pg_class table_class ON table_class.oid = trigger.tgrelid
            JOIN pg_namespace namespace ON namespace.oid = table_class.relnamespace
           WHERE namespace.nspname = 'public' AND NOT trigger.tgisinternal
        )
        SELECT kind, identity, definition FROM objects ORDER BY kind, identity
        """
    ).fetchall()
    document = json.dumps([dict(row) for row in rows], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(document.encode()).hexdigest()


def _row_counts(conn: Any) -> dict[str, int]:
    names = [
        str(row["tablename"])
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        ).fetchall()
    ]
    return {
        name: int(conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(name))).fetchone()["n"])
        for name in names
    }


def _identity_aggregates(conn: Any) -> dict[str, str]:
    primary_keys = conn.execute(
        """
        SELECT table_class.relname AS table_name,
               array_agg(attribute.attname ORDER BY key.ordinality) AS columns
          FROM pg_index item
          JOIN pg_class table_class ON table_class.oid = item.indrelid
          JOIN pg_namespace namespace ON namespace.oid = table_class.relnamespace
          CROSS JOIN LATERAL unnest(item.indkey) WITH ORDINALITY AS key(attnum, ordinality)
          JOIN pg_attribute attribute
            ON attribute.attrelid = table_class.oid AND attribute.attnum = key.attnum
         WHERE namespace.nspname = 'public' AND item.indisprimary
         GROUP BY table_class.relname
         ORDER BY table_class.relname
        """
    ).fetchall()
    table_names = {
        str(row["tablename"])
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        ).fetchall()
    }
    keyed_names = {str(row["table_name"]) for row in primary_keys}
    assert keyed_names == table_names

    aggregates: dict[str, str] = {}
    for row in primary_keys:
        table_name = str(row["table_name"])
        columns = [str(column) for column in row["columns"]]
        identifiers = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
        identities = conn.execute(
            sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                identifiers,
                sql.Identifier(table_name),
                identifiers,
            )
        ).fetchall()
        document = json.dumps([dict(identity) for identity in identities], sort_keys=True, separators=(",", ":"))
        aggregates[table_name] = hashlib.sha256(document.encode()).hexdigest()
    return aggregates


def _reconstruct_exact_pre_cut_terminal_role_catalog(conn: Any) -> None:
    """Give the byte-equivalent terminal schema its retired owner/login catalog."""

    conn.execute("ALTER ROLE tracefold RENAME TO tracefold_owner")
    for role in OLD_RUNTIME_ROLES:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS").format(
                sql.Identifier(role)
            )
        )
    conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO tracefold_serve")
    conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tracefold_workers")
    # `tracefold_nautilus` also held `SELECT, UPDATE ON trading_binding_runtime`. That grant is not
    # reconstructable at head — `20260901_0347` dropped the table — and it is not what this test is
    # about: the cutover claim is that removing the four retired *roles* moves no row and no revision.
    conn.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner IN SCHEMA public GRANT SELECT ON TABLES TO tracefold_serve"
    )


def _require_cutover_preconditions(conn: Any, *, expected_fingerprint: str) -> None:
    revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
    roles = {
        str(row["rolname"])
        for row in conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = 'tracefold_owner' OR rolname = ANY(%s)",
            (list(OLD_RUNTIME_ROLES),),
        ).fetchall()
    }
    if revision != latest_migration_version() or roles != {"tracefold_owner", *OLD_RUNTIME_ROLES}:
        raise RuntimeError("single_role_cutover_old_contract_mismatch")
    if _schema_fingerprint(conn) != expected_fingerprint:
        raise RuntimeError("single_role_cutover_schema_fingerprint_mismatch")


def test_exact_terminal_role_cutover_preserves_schema_rows_and_revision(postgres_clone_dsn: str) -> None:
    with connect_postgres(postgres_clone_dsn) as conn:
        before_fingerprint = _schema_fingerprint(conn)
        before_counts = _row_counts(conn)
        before_identities = _identity_aggregates(conn)
        _reconstruct_exact_pre_cut_terminal_role_catalog(conn)

        _require_cutover_preconditions(conn, expected_fingerprint=before_fingerprint)
        with pytest.raises(RuntimeError, match="schema_fingerprint_mismatch"):
            _require_cutover_preconditions(conn, expected_fingerprint="0" * 64)

        conn.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE tracefold_owner IN SCHEMA public "
            "REVOKE SELECT ON TABLES FROM tracefold_serve"
        )
        for role in OLD_RUNTIME_ROLES:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        conn.execute("ALTER ROLE tracefold_owner RENAME TO tracefold")
        for role in OLD_RUNTIME_ROLES:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))

        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == (
            latest_migration_version()
        )
        assert _schema_fingerprint(conn) == before_fingerprint
        assert _row_counts(conn) == before_counts
        assert _identity_aggregates(conn) == before_identities
        assert {
            str(row["rolname"])
            for row in conn.execute(
                "SELECT rolname FROM pg_roles WHERE rolname IN "
                "('tracefold', 'tracefold_owner', 'tracefold_serve', 'tracefold_workers', 'tracefold_nautilus')"
            ).fetchall()
        } == {"tracefold"}
