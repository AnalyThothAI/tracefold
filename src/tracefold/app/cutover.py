from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from tracefold.app.database import (
    WorkerDatabase,
    acquire_maintenance_advisory_lock,
    release_maintenance_advisory_lock,
)
from tracefold.app.hard_cut import rebuild_hard_cut_read_models
from tracefold.platform.postgres.postgres_client import (
    connect_postgres,
    postgres_health_check,
    with_password_from_file,
)
from tracefold.platform.postgres.postgres_migrations import (
    latest_migration_version,
    upgrade_head,
)
from tracefold.platform.postgres.runtime_roles import (
    RUNTIME_LOGIN_ROLES,
    provision_runtime_role_passwords,
    revoke_legacy_runtime_login,
    runtime_role_contract,
)


def execute_hard_cut(
    *,
    settings: Any,
    bootstrap_dsn: str,
    bootstrap_password_file: Path,
    snapshot_confirmed: bool,
    snapshot_waived: bool = False,
) -> dict[str, Any]:
    """Migrate, rebuild, audit, and revoke the legacy runtime login.

    The operator owns stopping the old runtime and taking the recoverable
    snapshot. This command verifies the explicit confirmation and refuses
    visible Tracefold runtime sessions.
    """

    if not snapshot_confirmed and not snapshot_waived:
        raise ValueError("hard_cut_snapshot_confirmation_required")
    if snapshot_confirmed and snapshot_waived:
        raise ValueError("hard_cut_snapshot_decision_conflict")
    bootstrap_url = with_password_from_file(
        str(bootstrap_dsn),
        Path(bootstrap_password_file),
    )
    bootstrap_conn = connect_postgres(bootstrap_url)
    lock_acquired = False
    worker_db: WorkerDatabase | None = None
    primary_error: BaseException | None = None
    try:
        acquire_maintenance_advisory_lock(bootstrap_conn)
        lock_acquired = True
        active_sessions = _active_tracefold_runtime_sessions(bootstrap_conn)
        if active_sessions:
            raise RuntimeError(f"hard_cut_runtime_sessions_active:{len(active_sessions)}")

        upgrade_head(bootstrap_url)
        expected_version = latest_migration_version()
        health = postgres_health_check(
            bootstrap_conn,
            expected_migration_version=expected_version,
        )
        if not bool(health.get("ok")):
            raise RuntimeError("hard_cut_schema_migration_unhealthy")

        password_files = _runtime_password_files(settings)
        with bootstrap_conn.transaction():
            provision_runtime_role_passwords(
                bootstrap_conn,
                password_files=password_files,
            )

        preflight_roles = runtime_role_contract(
            bootstrap_conn,
            expect_legacy_revoked=False,
        )
        if not preflight_roles["ok"]:
            raise RuntimeError("hard_cut_role_preflight_failed:" + ",".join(preflight_roles["failures"]))

        worker_db = WorkerDatabase.create(settings)
        rebuilt = rebuild_hard_cut_read_models(
            db=worker_db,
            settings=settings,
            now_ms=int(time.time() * 1_000),
        )
        asyncio.run(worker_db.aclose())
        worker_db = None

        with bootstrap_conn.transaction():
            revoke_legacy_runtime_login(bootstrap_conn)
        roles = runtime_role_contract(
            bootstrap_conn,
            expect_legacy_revoked=True,
        )
        if not roles["ok"]:
            raise RuntimeError("hard_cut_role_finalization_failed:" + ",".join(roles["failures"]))
        return {
            "status": "cutover_ready",
            "migration_version": expected_version,
            "snapshot_confirmed": bool(snapshot_confirmed),
            "snapshot_waived": bool(snapshot_waived),
            "legacy_runtime_login_revoked": True,
            "roles": roles,
            "read_models": rebuilt,
        }
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors: list[Exception] = []
        if worker_db is not None:
            try:
                asyncio.run(worker_db.aclose())
            except Exception as exc:
                cleanup_errors.append(exc)
        if lock_acquired:
            try:
                release_maintenance_advisory_lock(bootstrap_conn)
            except Exception as exc:
                cleanup_errors.append(exc)
        try:
            bootstrap_conn.close()
        except Exception as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            if primary_error is not None:
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(f"hard_cut_cleanup_error:{type(cleanup_error).__name__}:{cleanup_error}")
            else:
                raise ExceptionGroup("hard_cut_cleanup_failed", cleanup_errors)


def _runtime_password_files(settings: Any) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for role_name in RUNTIME_LOGIN_ROLES:
        role = role_name.removeprefix("tracefold_")
        path = settings.postgres_password_file(role)
        if path is None:
            raise ValueError(f"hard_cut_runtime_password_file_required:{role}")
        files[role_name] = Path(path)
    return files


def _active_tracefold_runtime_sessions(conn: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT usename, application_name, state
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND (
                application_name LIKE 'tracefold%%'
                OR application_name LIKE 'worker:%%'
                OR usename IN (
                  'tracefold_serve',
                  'tracefold_workers',
                  'tracefold_migrate'
                )
              )
            ORDER BY usename, application_name, state
            """
        ).fetchall()
    ]


__all__ = ["execute_hard_cut"]
