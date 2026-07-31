import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege, ReadOnlySqlTransaction

from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage, prepare_postgres_database
from tracefold.app.bootstrap import bootstrap_workers
from tracefold.app.database import WorkerDatabase
from tracefold.app.http.app import create_app
from tracefold.app.worker_http import create_workers_app
from tracefold.app.worker_manifest import worker_names
from tracefold.app.worker_runtime_status import WorkerRuntimeStatusRepository
from tracefold.platform.config.settings import Settings
from tracefold.platform.postgres.runtime_roles import (
    RUNTIME_LOGIN_ROLES,
    provision_runtime_role_passwords,
    revoke_legacy_runtime_login,
    runtime_role_contract,
)


def test_postgres_runtime_roles_enforce_read_write_and_ddl_boundaries():
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    runtime_id = "00000000-0000-0000-0000-000000000099"
    try:
        owners = conn.execute(
            """
            SELECT
              pg_get_userbyid((SELECT nspowner FROM pg_namespace WHERE nspname = 'public'))
                AS schema_owner,
              pg_get_userbyid((SELECT relowner FROM pg_class WHERE relname = 'events'))
                AS events_owner
            """
        ).fetchone()
        assert owners == {
            "schema_owner": "tracefold_owner",
            "events_owner": "tracefold_owner",
        }

        conn.execute("SET ROLE tracefold_serve")
        assert conn.execute("SELECT count(*) AS count FROM events").fetchone()["count"] == 0
        with pytest.raises((InsufficientPrivilege, ReadOnlySqlTransaction)):
            conn.execute(
                """
                INSERT INTO worker_runtime_status(
                  unit_name, runtime_id, runtime_version, effective_status,
                  heartbeat_at_ms, updated_at_ms
                )
                VALUES ('forbidden', %s, 'test', 'running', 1, 1)
                """,
                (runtime_id,),
            )
        conn.rollback()

        conn.execute("SET ROLE tracefold_workers")
        conn.execute(
            """
            INSERT INTO worker_runtime_status(
              unit_name, runtime_id, runtime_version, effective_status,
              heartbeat_at_ms, updated_at_ms
            )
            VALUES ('permission_test', %s, 'test', 'running', 1, 1)
            """,
            (runtime_id,),
        )
        conn.execute("DELETE FROM worker_runtime_status WHERE unit_name = 'permission_test'")
        with pytest.raises(InsufficientPrivilege):
            conn.execute("CREATE TABLE public.worker_ddl_forbidden(id integer)")
        with pytest.raises(InsufficientPrivilege):
            conn.execute("TRUNCATE TABLE public.events")
        conn.rollback()

        conn.execute("SET ROLE tracefold_migrate")
        conn.execute("SET ROLE tracefold_owner")
        conn.execute("CREATE TABLE public.migrate_ddl_allowed(id integer)")
        conn.execute("DROP TABLE public.migrate_ddl_allowed")
        conn.commit()
    finally:
        conn.close()


def test_role_password_provisioning_and_legacy_revoke_are_transactional(
    tmp_path: Path,
) -> None:
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    password_files: dict[str, Path] = {}
    for role in RUNTIME_LOGIN_ROLES:
        path = tmp_path / f"{role}.password"
        path.write_text(f"test-only-{role}-password", encoding="utf-8")
        password_files[role] = path
    try:
        conn.execute(
            """
            DO $role$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_app'
              ) THEN
                CREATE ROLE tracefold_app LOGIN;
              ELSE
                ALTER ROLE tracefold_app LOGIN;
              END IF;
            END
            $role$
            """
        )
        provision_runtime_role_passwords(
            conn,
            password_files=password_files,
        )
        preflight = runtime_role_contract(
            conn,
            expect_legacy_revoked=False,
        )
        assert preflight["ok"] is True

        revoke_legacy_runtime_login(conn)
        finalized = runtime_role_contract(
            conn,
            expect_legacy_revoked=True,
        )
        assert finalized["ok"] is True
    finally:
        conn.rollback()
        conn.close()


def test_serve_runtime_exposes_only_the_read_side(tmp_path):
    prepare_postgres_database()
    settings = Settings(ws_token="secret", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")

    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/api/status", headers={"Authorization": "Bearer secret"})
        runtime = client.app.state.service

    assert response.status_code == 200
    assert response.json()["data"]["runtime_role"] == "serve"
    assert runtime.role == "serve"
    assert not hasattr(runtime, "providers")
    assert not hasattr(runtime, "collector")
    assert not hasattr(runtime, "scheduler")


def test_workers_runtime_has_only_internal_operational_routes():
    app = create_workers_app(Settings())

    route_paths = {route.path for route in app.routes}

    assert {"/healthz", "/readyz", "/metrics"} <= route_paths
    assert "/api/status" not in route_paths
    assert "/ws" not in route_paths


def test_only_one_steady_workers_runtime_can_hold_the_database_lock(tmp_path):
    prepare_postgres_database()
    settings = Settings(storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    runtime = bootstrap_workers(settings)
    try:
        with pytest.raises(RuntimeError, match="steady_workers_runtime_already_active"):
            bootstrap_workers(settings)
    finally:
        asyncio.run(runtime.aclose())


def test_steady_and_maintenance_runtimes_are_mutually_exclusive(tmp_path):
    prepare_postgres_database()
    settings = Settings(storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    steady = bootstrap_workers(settings)
    maintenance_db = WorkerDatabase.create(settings)
    try:
        with pytest.raises(RuntimeError, match="steady_workers_runtime_active"):
            maintenance_db.acquire_maintenance_runtime_lock()
    finally:
        asyncio.run(maintenance_db.aclose())
        asyncio.run(steady.aclose())

    maintenance_db = WorkerDatabase.create(settings)
    maintenance_lock = maintenance_db.acquire_maintenance_runtime_lock()
    try:
        with pytest.raises(RuntimeError, match="maintenance_runtime_active"):
            bootstrap_workers(settings)
    finally:
        maintenance_db.release_maintenance_runtime_lock(maintenance_lock)
        asyncio.run(maintenance_db.aclose())


def test_workers_startup_immediately_recovers_old_runtime_claims(tmp_path):
    prepare_postgres_database()
    now_ms = 1_800_000_000_000
    old_runtime_id = "00000000-0000-0000-0000-000000000088"
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO radar_projection_frontiers(
              target_type, target_id, window_key, venue, status,
              first_dirty_at_ms, deadline_at_ms, attempt_count,
              transient_failure_count, input_fingerprint,
              projection_version, claimed_by, claimed_until_ms,
              claimed_input_fingerprint, claimed_projection_version,
              updated_at_ms
            )
            VALUES (
              'Asset', 'asset:solana:test', '1h', 'all', 'running',
              %s, %s, 0, 0, 'input-v1', 'radar-v1', %s, %s,
              'input-v1', 'radar-v1', %s
            )
            """,
            (now_ms - 1_000, now_ms, old_runtime_id, now_ms + 60_000, now_ms),
        )
        conn.execute(
            """
            INSERT INTO asset_profile_refresh_targets(
              provider, target_type, target_id, chain_id, address, symbol,
              dirty_reason, payload_hash, source_watermark_ms, priority,
              due_at_ms, leased_until_ms, lease_owner, attempt_count,
              first_dirty_at_ms, updated_at_ms
            )
            VALUES (
              'gmgn', 'Asset', 'asset:solana:test', 'solana', 'test', 'TEST',
              'serving_entry', 'payload-v1', %s, 10,
              %s, %s, %s, 1, %s, %s
            )
            """,
            (
                now_ms,
                now_ms + 60_000,
                now_ms + 120_000,
                f"asset_profile_refresh:{old_runtime_id}",
                now_ms,
                now_ms,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    settings = Settings(storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    with patch("tracefold.app.bootstrap._now_ms", return_value=now_ms):
        runtime = bootstrap_workers(settings)
    try:
        conn = connect_postgres_test(read_only=False)
        try:
            frontier = conn.execute(
                """
                SELECT status, claimed_by, claimed_until_ms
                  FROM radar_projection_frontiers
                 WHERE target_type = 'Asset'
                   AND target_id = 'asset:solana:test'
                   AND window_key = '1h'
                   AND venue = 'all'
                """
            ).fetchone()
            target = conn.execute(
                """
                SELECT due_at_ms, leased_until_ms, lease_owner, attempt_count
                  FROM asset_profile_refresh_targets
                 WHERE provider = 'gmgn'
                   AND target_type = 'Asset'
                   AND target_id = 'asset:solana:test'
                """
            ).fetchone()
        finally:
            conn.close()
        assert frontier == {
            "status": "dirty",
            "claimed_by": None,
            "claimed_until_ms": None,
        }
        assert target == {
            "due_at_ms": now_ms,
            "leased_until_ms": None,
            "lease_owner": None,
            "attempt_count": 0,
        }
        assert runtime.snapshot.composition["recovered_claims"]["radar_projection_frontiers"] == 1
        assert runtime.snapshot.composition["recovered_claims"]["asset_profile_refresh_targets"] == 1
    finally:
        asyncio.run(runtime.aclose())


def test_serve_reads_worker_status_from_postgres_and_fails_stale_rows_closed(tmp_path):
    prepare_postgres_database()
    now_ms = 1_800_000_000_000
    conn = connect_postgres_test(read_only=False)
    try:
        WorkerRuntimeStatusRepository(conn).publish(
            runtime_id="00000000-0000-0000-0000-000000000001",
            runtime_version="test",
            statuses={
                name: {
                    "enabled": True,
                    "running": True,
                    "effective_status": "running",
                    "unavailable_reason": None,
                    "last_started_at_ms": now_ms - 100,
                    "last_finished_at_ms": None,
                    "last_result": None,
                    "last_error": None,
                    "iteration_duration_p99_ms": None,
                }
                for name in worker_names()
            },
            now_ms=now_ms - 20_000,
        )
    finally:
        conn.close()
    settings = Settings(ws_token="secret", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")

    with (
        patch("tracefold.app.serve_runtime._now_ms", return_value=now_ms),
        TestClient(create_app(settings=settings)) as client,
    ):
        payload = client.get(
            "/api/status",
            headers={"Authorization": "Bearer secret"},
        ).json()["data"]

    assert payload["workers"]["collector"]["effective_status"] == "unavailable"
    assert payload["workers"]["collector"]["unavailable_reason"] == "worker_status_stale"
    assert payload["workers"]["collector"]["runtime_id"] == "00000000-0000-0000-0000-000000000001"
    assert payload["workers"]["collector"]["runtime_version"] == "test"
    assert payload["workers"]["collector"]["heartbeat_at_ms"] == now_ms - 20_000
