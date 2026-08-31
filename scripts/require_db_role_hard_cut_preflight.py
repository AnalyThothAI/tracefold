from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg import conninfo
from psycopg.rows import dict_row

from tracefold.platform.config.loader import load_settings
from tracefold.platform.postgres.restore_drill import POSTGRES_PRODUCTION_IMAGE, _set_restore_function_search_path

_EXPECTED_KEYS = {
    "schema",
    "migration_head",
    "backup_path",
    "backup_created_at_ms",
    "backup_sha256",
    "restore_verified_at_ms",
    "restore_image_identity",
    "capital_control",
    "pending_cases",
    "nonterminal_intents",
    "active_legacy_orders",
    "public_schema_owner",
    "application_object_owner_violations",
    "default_acl_count",
    "owner_default_acl_count",
    "default_acl_privilege_mismatches",
}
_EXPECTED_VALUES = {
    "schema": "tracefold_db_role_hard_cut_preflight_v1",
    "migration_head": "20260830_0337",
    "restore_image_identity": POSTGRES_PRODUCTION_IMAGE,
    "capital_control": "PAUSED",
    "pending_cases": 0,
    "nonterminal_intents": 0,
    "active_legacy_orders": 0,
    "public_schema_owner": "tracefold_owner",
    "application_object_owner_violations": 0,
    "default_acl_count": 2,
    "owner_default_acl_count": 2,
    "default_acl_privilege_mismatches": 0,
}
_INTEGER_EVIDENCE = {
    "pending_cases",
    "nonterminal_intents",
    "active_legacy_orders",
    "application_object_owner_violations",
    "default_acl_count",
    "owner_default_acl_count",
    "default_acl_privilege_mismatches",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_preflight(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != _EXPECTED_KEYS:
        raise ValueError("db_role_hard_cut_preflight_shape_invalid")
    _require_exact_evidence(payload)

    backup_value = payload["backup_path"]
    if type(backup_value) is not str:
        raise ValueError("db_role_hard_cut_preflight_backup_path_invalid")
    backup_path = Path(backup_value)
    if not backup_path.is_absolute() or not backup_path.is_file():
        raise ValueError("db_role_hard_cut_preflight_backup_path_invalid")
    backup_created_at_ms = payload["backup_created_at_ms"]
    restore_verified_at_ms = payload["restore_verified_at_ms"]
    if type(backup_created_at_ms) is not int or backup_created_at_ms <= 0:
        raise ValueError("db_role_hard_cut_preflight_backup_created_at_ms_invalid")
    if backup_created_at_ms != backup_path.stat().st_mtime_ns // 1_000_000:
        raise ValueError("db_role_hard_cut_preflight_backup_created_at_ms_mismatch")
    if type(restore_verified_at_ms) is not int or restore_verified_at_ms < backup_created_at_ms:
        raise ValueError("db_role_hard_cut_preflight_restore_verified_at_ms_invalid")
    backup_sha256 = payload["backup_sha256"]
    if type(backup_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", backup_sha256):
        raise ValueError("db_role_hard_cut_preflight_backup_sha256_invalid")
    if _sha256(backup_path) != backup_sha256:
        raise ValueError("db_role_hard_cut_preflight_backup_sha256_mismatch")

    settings = load_settings(require_ws_token=False)
    migrate_dsn = conninfo.conninfo_to_dict(settings.postgres_dsn("migrate"))
    if migrate_dsn.get("user") != "tracefold_owner":
        raise ValueError("db_role_hard_cut_config_owner_dsn_required")
    if migrate_dsn.get("password"):
        raise ValueError("db_role_hard_cut_config_embedded_password_forbidden")
    password_file = settings.postgres_password_file("migrate")
    if password_file is None or password_file.name != "postgres_migrate_password":
        raise ValueError("db_role_hard_cut_migrate_password_file_required")


def _require_exact_evidence(payload: dict[str, Any]) -> None:
    for name, value in _EXPECTED_VALUES.items():
        if name in _INTEGER_EVIDENCE and type(payload[name]) is not int:
            raise ValueError(f"db_role_hard_cut_preflight_{name}_invalid")
        if payload[name] != value:
            raise ValueError(f"db_role_hard_cut_preflight_{name}_invalid")


def record_preflight(backup_path: Path, output_path: Path) -> None:
    backup_path = backup_path.resolve(strict=True)
    if not backup_path.is_file():
        raise ValueError("db_role_hard_cut_backup_path_invalid")
    if not output_path.is_absolute():
        raise ValueError("db_role_hard_cut_preflight_output_path_invalid")
    docker = _run(("docker", "info"), check=False)
    if docker.returncode != 0:
        raise RuntimeError("db_role_hard_cut_docker_unavailable")

    container = f"tracefold-role-backup-verify-{uuid.uuid4().hex[:12]}"
    admin_password = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="tracefold-role-backup-") as directory:
        password_path = Path(directory) / "postgres_password"
        password_path.write_text(admin_password + "\n", encoding="utf-8")
        password_path.chmod(0o600)
        try:
            _run(
                (
                    "docker",
                    "run",
                    "--name",
                    container,
                    "--detach",
                    "--publish",
                    "127.0.0.1::5432",
                    "--env",
                    "POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password",
                    "--volume",
                    f"{password_path}:/run/secrets/postgres_password:ro",
                    "--volume",
                    f"{backup_path}:/restore/tracefold.dump:ro",
                    POSTGRES_PRODUCTION_IMAGE,
                )
            )
            _wait_ready(container)
            port = _published_port(container)
            admin_dsn = f"postgresql://postgres:{admin_password}@127.0.0.1:{port}/postgres"
            restored_dsn = f"postgresql://postgres:{admin_password}@127.0.0.1:{port}/tracefold_restore"
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                conn.execute(_OLD_ROLE_BOOTSTRAP_SQL)
                conn.execute("CREATE DATABASE tracefold_restore OWNER tracefold_owner")
            _restore_section(container, "pre-data")
            _set_restore_function_search_path(restored_dsn, enabled=True)
            try:
                _restore_section(container, "data")
            finally:
                _set_restore_function_search_path(restored_dsn, enabled=False)
            _restore_section(container, "post-data")
            evidence = _restored_evidence(restored_dsn)
        finally:
            _run(("docker", "rm", "--force", container), check=False)

    payload = {
        "schema": "tracefold_db_role_hard_cut_preflight_v1",
        "migration_head": evidence["migration_head"],
        "backup_path": str(backup_path),
        "backup_created_at_ms": backup_path.stat().st_mtime_ns // 1_000_000,
        "backup_sha256": _sha256(backup_path),
        "restore_verified_at_ms": int(time.time() * 1000),
        "restore_image_identity": POSTGRES_PRODUCTION_IMAGE,
        "capital_control": evidence["capital_control"],
        "pending_cases": evidence["pending_cases"],
        "nonterminal_intents": evidence["nonterminal_intents"],
        "active_legacy_orders": evidence["active_legacy_orders"],
        "public_schema_owner": evidence["public_schema_owner"],
        "application_object_owner_violations": evidence["application_object_owner_violations"],
        "default_acl_count": evidence["default_acl_count"],
        "owner_default_acl_count": evidence["owner_default_acl_count"],
        "default_acl_privilege_mismatches": evidence["default_acl_privilege_mismatches"],
    }
    _require_exact_evidence(payload)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run(arguments: tuple[str, ...], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=True, check=check, text=True, timeout=240)


def _wait_ready(container: str) -> None:
    for _ in range(120):
        process = _run(("docker", "exec", container, "cat", "/proc/1/comm"), check=False)
        if process.stdout.strip() != "postgres":
            time.sleep(0.25)
            continue
        result = _run(("docker", "exec", container, "pg_isready", "-U", "postgres"), check=False)
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError("db_role_hard_cut_restore_postgres_not_ready")


def _published_port(container: str) -> int:
    output = _run(("docker", "port", container, "5432/tcp")).stdout.strip().splitlines()[0]
    return int(output.rsplit(":", 1)[1])


def _restore_section(container: str, section: str) -> None:
    _run(
        (
            "docker",
            "exec",
            container,
            "pg_restore",
            "--exit-on-error",
            f"--section={section}",
            "--username=postgres",
            "--dbname=tracefold_restore",
            "/restore/tracefold.dump",
        )
    )


def _restored_evidence(dsn: str) -> dict[str, Any]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(_RESTORE_EVIDENCE_SQL).fetchone()
    if row is None:
        raise RuntimeError("db_role_hard_cut_restore_evidence_missing")
    return dict(row)


_OLD_ROLE_BOOTSTRAP_SQL = """
CREATE ROLE tracefold_app NOLOGIN INHERIT SUPERUSER CREATEDB CREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE tracefold_owner NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE tracefold_serve LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE tracefold_serve SET default_transaction_read_only = on;
CREATE ROLE tracefold_workers LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE tracefold_nautilus LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE tracefold_migrate LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT tracefold_owner TO tracefold_migrate WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
"""

_RESTORE_EVIDENCE_SQL = """
SELECT (SELECT version_num FROM alembic_version) AS migration_head,
       (SELECT control FROM trading_runtime_state WHERE id = 1) AS capital_control,
       (SELECT count(*)::integer FROM trading_cases WHERE state IN ('PENDING', 'RUNNING')) AS pending_cases,
       (SELECT count(*)::integer FROM trading_intents
         WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW'))
         AS nonterminal_intents,
       (SELECT count(*)::integer FROM trading_orders
         WHERE state IN (
           'PREPARED', 'AWAITING_APPROVAL', 'APPROVED', 'SUBMITTING', 'AMBIGUOUS',
           'RECONCILING', 'MANUAL_REVIEW_REQUIRED', 'ACKNOWLEDGED', 'PARTIAL',
           'OPEN', 'UNPROTECTED', 'SAFETY_CLOSING'
         )) AS active_legacy_orders,
       (SELECT owner.rolname FROM pg_namespace namespace
         JOIN pg_roles owner ON owner.oid = namespace.nspowner
        WHERE namespace.nspname = 'public') AS public_schema_owner,
       (
         (SELECT count(*) FROM pg_class object
           JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
           JOIN pg_roles owner ON owner.oid = object.relowner
          WHERE namespace.nspname = 'public'
            AND object.relkind IN ('r', 'p', 'S', 'v', 'm')
            AND owner.rolname <> 'tracefold_owner'
            AND NOT EXISTS (
              SELECT 1 FROM pg_depend dependency
               WHERE dependency.classid = 'pg_class'::regclass
                 AND dependency.objid = object.oid AND dependency.deptype = 'e'
            ))
         +
         (SELECT count(*) FROM pg_proc object
           JOIN pg_namespace namespace ON namespace.oid = object.pronamespace
           JOIN pg_roles owner ON owner.oid = object.proowner
          WHERE namespace.nspname = 'public'
            AND owner.rolname <> 'tracefold_owner'
            AND NOT EXISTS (
              SELECT 1 FROM pg_depend dependency
               WHERE dependency.classid = 'pg_proc'::regclass
                 AND dependency.objid = object.oid AND dependency.deptype = 'e'
            ))
       )::integer AS application_object_owner_violations,
       (SELECT count(*)::integer FROM pg_default_acl) AS default_acl_count,
       (SELECT count(*)::integer FROM pg_default_acl defaults
         JOIN pg_roles owner ON owner.oid = defaults.defaclrole
        WHERE owner.rolname = 'tracefold_owner'
          AND defaults.defaclnamespace = 'public'::regnamespace
          AND defaults.defaclobjtype IN ('r', 'S')) AS owner_default_acl_count,
       (
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
         SELECT count(*)::integer
         FROM (
           (SELECT * FROM actual EXCEPT SELECT * FROM expected)
           UNION ALL
           (SELECT * FROM expected EXCEPT SELECT * FROM actual)
         ) mismatch
       ) AS default_acl_privilege_mismatches
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("preflight", nargs="?", type=Path)
    parser.add_argument("--record", nargs=2, metavar=("BACKUP", "OUTPUT"), type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.record is not None:
            if arguments.preflight is not None:
                parser.error("preflight and --record are mutually exclusive")
            record_preflight(*arguments.record)
            print(f"PostgreSQL role hard-cut backup restore verified; receipt: {arguments.record[1]}")
            return 0
        if arguments.preflight is None:
            parser.error("preflight is required")
        require_preflight(arguments.preflight)
    except (OSError, json.JSONDecodeError, psycopg.Error, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        print(f"PostgreSQL role hard-cut preflight refused: {exc}", file=sys.stderr)
        return 1
    print("PostgreSQL role hard-cut preflight verified: backup restored; Capital PAUSED; old head 20260830_0337.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
