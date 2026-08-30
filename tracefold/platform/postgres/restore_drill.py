"""Isolated PostgreSQL dump/restore evidence using the production client image."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from .audit import PostgresOperationalAudit
from .migrations import upgrade_head

POSTGRES_PRODUCTION_IMAGE = (
    "postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296"
)


def run_restore_drill(
    admin_dsn: str,
    *,
    seed_and_summarize: Callable[[str], dict[str, Any]],
    summarize: Callable[[Any], dict[str, Any]],
    smoke: Callable[[Any], dict[str, bool]],
) -> dict[str, Any]:
    """Create two disposable databases; never read or write the database named by ``admin_dsn``."""

    suffix = uuid.uuid4().hex[:12]
    source_name = f"tracefold_restore_source_{suffix}"
    restored_name = f"tracefold_restore_target_{suffix}"
    source_dsn = _database_dsn(admin_dsn, source_name)
    restored_dsn = _database_dsn(admin_dsn, restored_name)
    started = time.perf_counter()
    _create_database(admin_dsn, source_name)
    _create_database(admin_dsn, restored_name)
    try:
        upgrade_head(source_dsn)
        source_summary = seed_and_summarize(source_dsn)
        with tempfile.TemporaryDirectory(prefix="tracefold-restore-") as directory:
            workspace = Path(directory)
            pgpass = workspace / "pgpass"
            _write_pgpass(pgpass, admin_dsn)
            dump_started = time.perf_counter()
            _run_client(admin_dsn, database=source_name, workspace=workspace, pgpass=pgpass, action="dump")
            dump_seconds = time.perf_counter() - dump_started
            restore_started = time.perf_counter()
            _run_client(admin_dsn, database=restored_name, workspace=workspace, pgpass=pgpass, action="restore")
            restore_seconds = time.perf_counter() - restore_started

        upgrade_head(restored_dsn)
        with psycopg.connect(restored_dsn, row_factory=dict_row) as conn:
            audit = PostgresOperationalAudit(conn).run(deep=True)
            restored_summary = summarize(conn)
            smoke_results = smoke(conn)
        if not audit["ok"] or source_summary != restored_summary or not all(smoke_results.values()):
            evidence = {
                "audit_ok": audit["ok"],
                "news_schema": audit["news_schema"],
                "trading_schema": audit["trading_schema"],
                "runtime_roles": audit["runtime_roles"],
                "missing_estimates": [name for name, count in audit["row_estimates"].items() if count < 0],
                "source_summary": source_summary,
                "restored_summary": restored_summary,
                "smoke": smoke_results,
            }
            raise RuntimeError(f"postgres_restore_drill_evidence_mismatch:{json.dumps(evidence, sort_keys=True)}")
        return {
            "ok": True,
            "image_identity": POSTGRES_PRODUCTION_IMAGE,
            "source_head": source_summary["migration_head"],
            "restored_head": restored_summary["migration_head"],
            "duration_seconds": {
                "dump": round(dump_seconds, 6),
                "restore": round(restore_seconds, 6),
                "total": round(time.perf_counter() - started, 6),
            },
            "identity_summary": restored_summary,
            "smoke": smoke_results,
            "audit": {
                "mode": audit["mode"],
                "migration_status": audit["migration_status"],
                "news_schema_exact": audit["news_schema"]["exact"],
                "trading_schema_exact": audit["trading_schema"]["exact"],
                "runtime_roles_ok": audit["runtime_roles"]["ok"],
            },
        }
    finally:
        _drop_database(admin_dsn, restored_name)
        _drop_database(admin_dsn, source_name)


def _create_database(admin_dsn: str, name: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _database_dsn(dsn: str, database: str) -> str:
    if "://" in dsn:
        parsed = urlsplit(dsn)
        return urlunsplit((parsed.scheme, parsed.netloc, f"/{quote(database, safe='')}", parsed.query, parsed.fragment))
    return make_conninfo(dsn, dbname=database)


def _drop_database(admin_dsn: str, name: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _write_pgpass(path: Path, dsn: str) -> None:
    values = conninfo_to_dict(dsn)
    host = _client_host(str(values.get("host") or "127.0.0.1"))
    fields = (
        host,
        str(values.get("port") or "5432"),
        "*",
        str(values.get("user") or "postgres"),
        str(values.get("password") or ""),
    )
    path.write_text(":".join(_pgpass_escape(value) for value in fields) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _run_client(
    dsn: str,
    *,
    database: str,
    workspace: Path,
    pgpass: Path,
    action: str,
) -> None:
    values = conninfo_to_dict(dsn)
    host = _client_host(str(values.get("host") or "127.0.0.1"))
    connection = [
        "-h",
        host,
        "-p",
        str(values.get("port") or "5432"),
        "-U",
        str(values.get("user") or "postgres"),
        "-d",
        database,
    ]
    if action == "dump":
        client = ["pg_dump", "--format=custom", "--file=/work/tracefold.dump", *connection]
    elif action == "restore":
        client = ["pg_restore", "--exit-on-error", *connection, "/work/tracefold.dump"]
    else:  # pragma: no cover - internal closed choice
        raise ValueError(f"postgres_restore_drill_action_invalid:{action}")
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("postgres_restore_drill_docker_missing")
    # All arguments are a closed client command plus parsed connection fields; shell execution is disabled.
    subprocess.run(  # noqa: S603
        [
            docker,
            "run",
            "--rm",
            "--add-host",
            "host.docker.internal:host-gateway",
            "--volume",
            f"{workspace}:/work",
            "--volume",
            f"{pgpass}:/run/secrets/pgpass:ro",
            "--env",
            "PGPASSFILE=/run/secrets/pgpass",
            POSTGRES_PRODUCTION_IMAGE,
            *client,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _client_host(host: str) -> str:
    return "host.docker.internal" if host in {"127.0.0.1", "localhost", "::1"} else host


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")
