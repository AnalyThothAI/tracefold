from __future__ import annotations

import asyncio
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege, ReadOnlySqlTransaction

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
    prepare_postgres_database,
)
from tests.postgres_test_utils import (
    test_postgres_dsn as _test_postgres_dsn,
)
from tracefold.app.http.app import create_app
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.runtime import WorkersRuntimeRepository
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_item
from tracefold.platform.config.models import Settings
from tracefold.platform.postgres.runtime_roles import (
    RUNTIME_LOGIN_ROLES,
    provision_runtime_role_passwords,
    revoke_legacy_runtime_login,
    runtime_role_contract,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

RUNTIME_ID = "00000000-0000-0000-0000-000000000099"
SECOND_RUNTIME_ID = "00000000-0000-0000-0000-000000000100"
RUNTIME_MANIFEST_BARRIER_SHA = "a" * 64
_PROCESS_ENTRY = Path(__file__).with_name("_workers_runtime_process_entry.py")
_LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def test_postgres_runtime_roles_enforce_read_write_and_ddl_boundaries() -> None:
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("SET ROLE tracefold_serve")
        assert conn.execute("SELECT count(*) AS count FROM workers_runtime").fetchone()["count"] == 0
        with pytest.raises((InsufficientPrivilege, ReadOnlySqlTransaction)):
            conn.execute(
                """
                INSERT INTO workers_runtime(
                  singleton_key, runtime_id, runtime_version, lifecycle_state,
                  started_at_ms, heartbeat_at_ms, fatal_code
                )
                VALUES (true, %s, 'test', 'starting', 1, 1, NULL)
                """,
                (RUNTIME_ID,),
            )
        conn.rollback()

        conn.execute("SET ROLE tracefold_workers")
        conn.execute(
            """
            INSERT INTO workers_runtime(
              singleton_key, runtime_id, runtime_version, lifecycle_state,
              started_at_ms, heartbeat_at_ms, fatal_code
            )
            VALUES (true, %s, 'test', 'starting', 1, 1, NULL)
            """,
            (RUNTIME_ID,),
        )
        conn.execute("DELETE FROM workers_runtime WHERE singleton_key")
        with pytest.raises(InsufficientPrivilege):
            conn.execute("CREATE TABLE public.worker_ddl_forbidden(id integer)")
        conn.rollback()
    finally:
        conn.close()


def test_workers_role_appends_evidence_without_table_rewrite_privilege() -> None:
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    try:
        event = parse_opennews_message(
            {
                "method": "strategy.triggered",
                "params": {
                    "id": 112_901,
                    "text": "Micron says DRAM contract prices rose again in August",
                    "link": "https://example.test/112901",
                    "source": "Reuters",
                    "newsType": "news",
                    "engineType": "news",
                    "ts": "2026-08-21T08:00:00+08:00",
                    "aiRating": {"score": 82, "signal": "long", "status": "done"},
                    "coins": [],
                    "strategy": {
                        "id": 1018,
                        "name": "News Score > 70",
                        "engine_type": "news",
                        "source_type": "news",
                    },
                },
            },
        )
        assert event is not None
        repos = repositories_for_connection(conn)
        with repos.transaction():
            opened = admit_item(
                repos,
                event=event,
                ingest_mode="live",
                observed_at_ms=1_787_287_000_000,
                trace_id="role-authentic-evidence",
                watchlist_symbols=frozenset(),
                now_ms=1_787_287_000_000,
            )

        conn.execute("SET ROLE tracefold_workers")
        conn.execute(
            "UPDATE news_events SET provider_score_max = provider_score_max + 1 WHERE event_id = %s",
            (opened.event_id,),
        )
        evidence = repositories_for_connection(conn).news.append_evidence_snapshot(
            event_id=opened.event_id,
            now_ms=1_787_287_000_001,
        )
        assert evidence["evidence_version"] == 2
        conn.commit()

        with pytest.raises(InsufficientPrivilege):
            conn.execute(
                "UPDATE news_event_evidence_snapshots SET created_at_ms = created_at_ms + 1 WHERE event_id = %s",
                (opened.event_id,),
            )
        conn.rollback()
    finally:
        conn.close()


def test_role_password_provisioning_and_legacy_revoke_are_transactional(tmp_path: Path) -> None:
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
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tracefold_app') THEN
                CREATE ROLE tracefold_app LOGIN;
              ELSE
                ALTER ROLE tracefold_app LOGIN;
              END IF;
            END
            $role$
            """
        )
        provision_runtime_role_passwords(conn, password_files=password_files)
        assert runtime_role_contract(conn, expect_legacy_revoked=False)["ok"] is True
        revoke_legacy_runtime_login(conn)
        assert runtime_role_contract(conn, expect_legacy_revoked=True)["ok"] is True
    finally:
        conn.rollback()
        conn.close()


def test_serve_runtime_is_read_only_composition_and_status_uses_one_runtime_row(tmp_path) -> None:
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            repository = WorkersRuntimeRepository(conn)
            assert repository.begin(
                runtime_id=RUNTIME_ID,
                runtime_version="v2",
                started_at_ms=1_000,
                now_ms=1_000,
            )
            repository.transition(runtime_id=RUNTIME_ID, lifecycle_state="running", now_ms=2_000)
    finally:
        conn.close()
    settings = Settings(ws_token="secret", storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")

    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/api/status", headers={"Authorization": "Bearer secret"})
        runtime = client.app.state.service

    assert response.status_code == 200
    # #256: the serve runtime opens no read-write transaction any more. The ReviewDesk write path it existed
    # for is gone, and `tracefold news review submit` opens its own connection under the same role.
    assert not hasattr(runtime, "review_transaction")
    data = response.json()["data"]
    assert set(data) == {"measured_at_ms", "runtime"}
    assert data["runtime"]["workers_runtime"]["runtime_id"] == RUNTIME_ID
    assert not hasattr(runtime, "providers")
    assert not hasattr(runtime, "collector")
    assert not hasattr(runtime, "scheduler")


@pytest.mark.slow
def test_real_workers_readiness_waits_for_the_persisted_runtime_manifest(tmp_path: Path) -> None:
    prepare_postgres_database()
    port = _free_port()
    release_gate = tmp_path / "release-runtime-manifest"
    entered_gate = Path(f"{release_gate}.entered")
    process = _start_workers_process(
        "manifest_barrier",
        port,
        extra_env={"TRACEFOLD_TEST_MANIFEST_GATE": str(release_gate)},
    )
    try:
        deadline = time.monotonic() + 20.0
        while not entered_gate.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout is not None else ""
                raise AssertionError(
                    f"workers exited before manifest barrier: code={process.returncode}; output={output!r}"
                )
            time.sleep(0.01)
        assert entered_gate.exists(), "workers never reached the runtime-manifest barrier"

        try:
            with _LOCAL_HTTP.open(f"http://127.0.0.1:{port}/readyz", timeout=0.2) as response:
                readiness_status: int | None = response.status
        except urllib.error.HTTPError as exc:
            readiness_status = exc.code
        except OSError:
            readiness_status = None
        assert readiness_status != 200

        conn = connect_postgres_test(read_only=False)
        try:
            assert (
                conn.execute(
                    "SELECT manifest_sha FROM news_agent_runtime_manifests WHERE manifest_sha = %s",
                    (RUNTIME_MANIFEST_BARRIER_SHA,),
                ).fetchone()
                is None
            )
        finally:
            conn.close()

        release_gate.touch()
        _wait_ready(process, port)

        conn = connect_postgres_test(read_only=False)
        try:
            manifest = conn.execute(
                "SELECT manifest_sha FROM news_agent_runtime_manifests WHERE manifest_sha = %s",
                (RUNTIME_MANIFEST_BARRIER_SHA,),
            ).fetchone()
            active = conn.execute(
                "SELECT payload ->> 'runtime_manifest_sha' AS runtime_manifest_sha "
                "FROM news_learning_artifacts WHERE kind = 'active_agent' "
                "ORDER BY created_at_ms DESC, artifact_sha DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert manifest is not None
        assert active is not None
        assert active["runtime_manifest_sha"] == RUNTIME_MANIFEST_BARRIER_SHA

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
    finally:
        release_gate.touch(exist_ok=True)
        _ensure_process_stopped(process)


def test_steady_lock_retains_a_real_control_query_lane_and_excludes_other_runtimes(tmp_path) -> None:
    prepare_postgres_database()
    settings = Settings(storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    first = WorkerDatabase.create(settings)
    second = WorkerDatabase.create(settings)
    steady_lock = first.acquire_steady_runtime_lock()
    try:
        first.prewarm_control_connection()

        def control_query() -> int:
            with first.worker_pool.connection(timeout=0.250) as conn:
                row = conn.execute("SELECT 1 AS ok").fetchone()
                return int(row["ok"])

        assert (
            asyncio.run(
                first.run_control(
                    "worker_pool_control_lane_test",
                    control_query,
                    operation_timeout_seconds=1.0,
                )
            )
            == 1
        )
        with pytest.raises(RuntimeError, match="steady_workers_runtime_already_active"):
            second.acquire_steady_runtime_lock()
        with pytest.raises(RuntimeError, match="steady_workers_runtime_active"):
            second.acquire_maintenance_runtime_lock()
    finally:
        first.release_steady_runtime_lock(steady_lock)
        asyncio.run(first.aclose())
        asyncio.run(second.aclose())


def test_terminal_runtime_rows_allow_immediate_takeover(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        prepare_postgres_database()
        repository = WorkersRuntimeRepository(conn)
        with conn.transaction():
            assert repository.begin(
                runtime_id=RUNTIME_ID,
                runtime_version="v2",
                started_at_ms=1_000,
                now_ms=1_000,
            )
        with conn.transaction():
            assert not repository.begin(
                runtime_id=SECOND_RUNTIME_ID,
                runtime_version="v2",
                started_at_ms=1_500,
                now_ms=1_500,
            )
        with conn.transaction():
            repository.transition(
                runtime_id=RUNTIME_ID,
                lifecycle_state="failed",
                fatal_code="child_failed",
                now_ms=2_000,
            )
        with conn.transaction():
            assert repository.begin(
                runtime_id=SECOND_RUNTIME_ID,
                runtime_version="v2",
                started_at_ms=2_001,
                now_ms=2_001,
            )
            row = repository.read()
            assert row is not None
            assert row["started_at_ms"] == 2_001
            assert row["heartbeat_at_ms"] == 2_001
            repository.transition(runtime_id=SECOND_RUNTIME_ID, lifecycle_state="stopped", now_ms=2_002)
        with conn.transaction():
            assert repository.begin(
                runtime_id=RUNTIME_ID,
                runtime_version="v2",
                started_at_ms=2_003,
                now_ms=2_003,
            )
    finally:
        conn.close()


@pytest.mark.slow
def test_real_workers_process_gracefully_stops_and_closes_probe() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("inert", port)
    try:
        _wait_ready(process, port)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
        _assert_probe_closed(port)
        row = _runtime_row()
        assert row["lifecycle_state"] == "stopped"
        assert row["fatal_code"] is None
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_workers_startup_recovers_transient_pooled_heartbeat_failures() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("control_transient_startup", port)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "CONTROL_TRANSIENT")
        row = _runtime_row()
        assert row["lifecycle_state"] == "running"
        assert row["fatal_code"] is None
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_sigterm_interrupts_persistent_startup_heartbeat_retries() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("control_transient_startup_persistent", port)
    try:
        _wait_for_output(process, "CONTROL_TRANSIENT_PERSISTENT", timeout_seconds=20.0)
        _wait_probe_status(process, port, path="/readyz", expected_status=503)
        starting = _runtime_row()
        assert starting["lifecycle_state"] == "starting"
        assert starting["fatal_code"] is None

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
        _assert_probe_closed(port)
        stopped = _runtime_row()
        assert stopped["runtime_id"] == starting["runtime_id"]
        assert stopped["lifecycle_state"] == "stopped"
        assert stopped["fatal_code"] is None
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_workers_startup_native_control_timeouts_recover_in_the_same_runtime() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("control_native_timeout", port)
    try:
        _wait_probe_status(
            process,
            port,
            path="/readyz",
            expected_status=503,
            timeout_seconds=20.0,
        )
        starting = _runtime_row()
        assert starting["lifecycle_state"] == "starting"
        assert starting["fatal_code"] is None
        process_id = process.pid
        runtime_id = starting["runtime_id"]

        _wait_ready(process, port)
        _wait_for_output(process, "CONTROL_NATIVE_TIMEOUT_RECOVERED")
        assert process.poll() is None
        running = _runtime_row()
        assert process.pid == process_id
        assert running["runtime_id"] == runtime_id
        assert running["lifecycle_state"] == "running"
        assert running["fatal_code"] is None

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_workers_runtime_heartbeat_stale_degrades_readiness_without_killing_root() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("control_transient_runtime", port)
    try:
        _wait_ready(process, port)
        _wait_probe_status(process, port, path="/readyz", expected_status=503)
        _wait_probe_status(process, port, path="/healthz", expected_status=200)
        assert process.poll() is None
        row = _runtime_row()
        assert row["lifecycle_state"] == "running"
        assert row["fatal_code"] is None

        _wait_ready(process, port)
        assert process.poll() is None
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_workers_child_failure_closes_probe_and_persists_fatal_state() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("child_failure", port)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "ABOUT_TO_FAIL")
        assert process.wait(timeout=5.0) != 0
        _assert_probe_closed(port)
        row = _runtime_row()
        assert row["lifecycle_state"] == "failed"
        assert row["fatal_code"] == "child_failed"
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_workers_pinned_session_loss_closes_probe_and_persists_fatal_state() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("inert", port)
    try:
        _wait_ready(process, port)
        conn = connect_postgres_test(read_only=False)
        try:
            row = conn.execute(
                """
                SELECT pid
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND classid = %s
                  AND objid = 1
                  AND granted
                """,
                (int("54524644", 16),),
            ).fetchone()
            assert row is not None
            assert (
                conn.execute(
                    "SELECT pg_terminate_backend(%s) AS terminated",
                    (row["pid"],),
                ).fetchone()["terminated"]
                is True
            )
            conn.commit()
        finally:
            conn.close()

        assert process.wait(timeout=5.0) != 0
        _assert_probe_closed(port)
        runtime = _runtime_row()
        assert runtime["lifecycle_state"] == "failed"
        assert runtime["fatal_code"] == "singleton_lost"
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_workers_never_returning_finite_operation_is_fatal() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("finite_overrun", port)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "FINITE_STARTED")
        assert process.wait(timeout=6.0) != 0
        _assert_probe_closed(port)
        row = _runtime_row()
        assert row["lifecycle_state"] == "failed"
        assert row["fatal_code"] == "resource_operation_overrun"
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_workers_never_returning_control_operation_is_fatal() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("control_overrun", port)
    try:
        _wait_for_output(process, "CONTROL_STARTED", timeout_seconds=15.0)
        # The code-owned control envelope is 1 s of native work plus 6 s of
        # bounded completion grace; leave process-exit scheduling headroom.
        assert process.wait(timeout=9.0) != 0
        _assert_probe_closed(port)
        row = _runtime_row()
        # The only control executor is the failed capability, so the process
        # cannot truthfully persist a final transition through that same hung
        # lane. External restart observes the non-zero exit immediately and
        # the retained starting row fails closed as stale.
        assert row["lifecycle_state"] == "starting"
        assert row["fatal_code"] is None
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_sigterm_after_provider_completion_preserves_inflight_publication() -> None:
    prepare_postgres_database()
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("CREATE TABLE worker_runtime_test_publications(id integer PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    port = _free_port()
    process = _start_workers_process("provider_publication", port)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "PROVIDER_DONE")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
        _assert_probe_closed(port)
        conn = connect_postgres_test(read_only=False)
        try:
            assert (
                conn.execute("SELECT count(*) AS count FROM worker_runtime_test_publications").fetchone()["count"] == 1
            )
        finally:
            conn.close()
        row = _runtime_row()
        assert row["lifecycle_state"] == "stopped"
        assert row["fatal_code"] is None
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_workers_absolute_graceful_deadline_covers_never_returning_future() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("finite_never_returns", port, graceful_timeout_seconds=0.5)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "FINITE_STARTED")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=2.0) != 0
        _assert_probe_closed(port)
        row = _runtime_row()
        assert row["lifecycle_state"] == "failed"
        assert row["fatal_code"] == "graceful_deadline_exceeded"
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_fatal_transition_retries_one_transient_control_write_within_the_watchdog() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("finite_never_returns_failed_transition_once", port)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "FINITE_STARTED")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=4.0) != 0
        _assert_probe_closed(port)
        row = _runtime_row()
        assert row["lifecycle_state"] == "failed"
        assert row["fatal_code"] == "graceful_deadline_exceeded"
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_shutdown_never_returning_control_write_obeys_absolute_graceful_deadline() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("shutdown_stopping_control_never_returns", port)
    try:
        _wait_ready(process, port)
        process.send_signal(signal.SIGTERM)
        _wait_for_output(process, "SHUTDOWN_STOPPING_CONTROL_STARTED")
        assert process.wait(timeout=3.0) != 0
        _assert_probe_closed(port)
    finally:
        _ensure_process_stopped(process)


@pytest.mark.scheduled
def test_production_graceful_deadline_terminates_a_never_returning_future() -> None:
    """Exercise the unmodified 30-second production deadline outside the merge gate."""

    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("finite_never_returns", port)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "FINITE_STARTED")
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=40.0) != 0
        elapsed = time.monotonic() - started
        assert 25.0 <= elapsed <= 40.0
        _assert_probe_closed(port)
        row = _runtime_row()
        assert row["lifecycle_state"] == "failed"
        assert row["fatal_code"] == "graceful_deadline_exceeded"
    finally:
        _ensure_process_stopped(process)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_workers_process(
    mode: str,
    port: int,
    *,
    extra_env: dict[str, str] | None = None,
    graceful_timeout_seconds: float | None = None,
) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        str(_PROCESS_ENTRY),
        "--dsn",
        _test_postgres_dsn(),
        "--port",
        str(port),
        "--mode",
        mode,
    ]
    if graceful_timeout_seconds is not None:
        command.extend(("--graceful-timeout-seconds", str(graceful_timeout_seconds)))
    return subprocess.Popen(
        command,
        cwd=_PROCESS_ENTRY.parents[2],
        env={**os.environ, **(extra_env or {}), "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _wait_ready(
    process: subprocess.Popen[str],
    port: int,
    *,
    timeout_seconds: float = 20.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/readyz"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise AssertionError(
                f"workers process exited before readiness: code={process.returncode}; output={output!r}"
            )
        try:
            with _LOCAL_HTTP.open(url, timeout=0.1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.HTTPError):
            pass
        time.sleep(0.02)
    process.terminate()
    try:
        output, _ = process.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate(timeout=2.0)
    raise AssertionError(f"workers process did not become ready; output={output!r}")


def _wait_probe_status(
    process: subprocess.Popen[str],
    port: int,
    *,
    path: str,
    expected_status: int,
    timeout_seconds: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}{path}"
    last_status: int | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise AssertionError(
                f"workers process exited before {path}={expected_status}: code={process.returncode}; output={output!r}"
            )
        try:
            with _LOCAL_HTTP.open(url, timeout=0.1) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except OSError:
            status = None
        last_status = status
        if status == expected_status:
            return
        time.sleep(0.02)
    raise AssertionError(f"workers probe {path} did not return {expected_status}; last_status={last_status}")


def _wait_metrics(
    process: subprocess.Popen[str],
    port: int,
    expected: tuple[str, ...],
    *,
    timeout_seconds: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/metrics"
    last_metrics = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"workers process exited while waiting for metrics: code={process.returncode}")
        try:
            with _LOCAL_HTTP.open(url, timeout=0.2) as response:
                last_metrics = response.read().decode("utf-8")
        except OSError:
            time.sleep(0.02)
            continue
        if all(value in last_metrics for value in expected):
            return
        time.sleep(0.02)
    raise AssertionError(f"workers metrics missing {expected!r}; metrics={last_metrics!r}")


def _wait_for_output(
    process: subprocess.Popen[str],
    expected: str,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    output = process.stdout
    assert output is not None
    deadline = time.monotonic() + timeout_seconds
    seen: list[str] = []
    lines: queue.Queue[str] = queue.Queue()

    def read_lines() -> None:
        for line in output:
            lines.put(line)

    threading.Thread(target=read_lines, daemon=True).start()
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=min(0.1, deadline - time.monotonic()))
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        seen.append(line)
        if expected in line:
            return
    raise AssertionError(f"missing process marker {expected!r}; output={''.join(seen)!r}")


def _wait_for_outputs(
    process: subprocess.Popen[str],
    expected: tuple[str, ...],
    *,
    timeout_seconds: float,
) -> None:
    output = process.stdout
    assert output is not None
    deadline = time.monotonic() + timeout_seconds
    missing = set(expected)
    seen: list[str] = []
    lines: queue.Queue[str] = queue.Queue()

    def read_lines() -> None:
        for line in output:
            lines.put(line)

    threading.Thread(target=read_lines, daemon=True).start()
    while time.monotonic() < deadline and missing:
        try:
            line = lines.get(timeout=min(0.1, deadline - time.monotonic()))
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        seen.append(line)
        missing = {marker for marker in missing if marker not in line}
    assert not missing, f"missing process markers {sorted(missing)!r}; output={''.join(seen)!r}"


def _assert_probe_closed(port: int) -> None:
    with pytest.raises(OSError):
        _LOCAL_HTTP.open(f"http://127.0.0.1:{port}/healthz", timeout=0.2)


def _runtime_row() -> dict[str, object]:
    conn = connect_postgres_test(read_only=False)
    try:
        row = conn.execute(
            "SELECT runtime_id, lifecycle_state, fatal_code FROM workers_runtime WHERE singleton_key"
        ).fetchone()
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def _ensure_process_stopped(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)
