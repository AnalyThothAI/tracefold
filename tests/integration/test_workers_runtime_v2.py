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
from tracefold.app.database import WorkerDatabase
from tracefold.app.http.app import create_app
from tracefold.app.worker_http import _create_workers_probe_app
from tracefold.app.workers import _ProbeState
from tracefold.app.workers_runtime import WorkersRuntimeRepository, workers_runtime_status
from tracefold.platform.config.settings import Settings
from tracefold.platform.postgres.runtime_roles import (
    RUNTIME_LOGIN_ROLES,
    provision_runtime_role_passwords,
    revoke_legacy_runtime_login,
    runtime_role_contract,
)

RUNTIME_ID = "00000000-0000-0000-0000-000000000099"
SECOND_RUNTIME_ID = "00000000-0000-0000-0000-000000000100"
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
    data = response.json()["data"]
    assert set(data) == {"measured_at_ms", "runtime", "providers"}
    assert data["runtime"]["workers_runtime"]["runtime_id"] == RUNTIME_ID
    assert not hasattr(runtime, "providers")
    assert not hasattr(runtime, "collector")
    assert not hasattr(runtime, "scheduler")


def test_workers_probe_has_only_private_operational_routes_and_health_never_calls_readiness() -> None:
    calls = 0

    def readiness() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": False, "reason": "starting"}

    app = _create_workers_probe_app(readiness=readiness, render_metrics=lambda: "")
    with TestClient(app) as client:
        health = client.get("/healthz")
        ready = client.get("/readyz")

    assert health.status_code == 200
    assert calls == 1
    assert ready.status_code == 503
    assert {route.path for route in app.routes} >= {"/healthz", "/readyz", "/metrics"}


def test_workers_metrics_render_does_not_block_readiness() -> None:
    metrics_entered = threading.Event()
    release_metrics = threading.Event()

    def render_metrics() -> str:
        metrics_entered.set()
        assert release_metrics.wait(timeout=2.0)
        return "metric 1\n"

    app = _create_workers_probe_app(
        readiness=lambda: {"ok": True},
        render_metrics=render_metrics,
    )
    with TestClient(app) as client:
        metrics_response: list[object] = []
        metrics_thread = threading.Thread(
            target=lambda: metrics_response.append(client.get("/metrics")),
        )
        release_timer = threading.Timer(0.5, release_metrics.set)
        try:
            metrics_thread.start()
            assert metrics_entered.wait(timeout=1.0)
            release_timer.start()
            started = time.monotonic()
            ready = client.get("/readyz")
            ready_elapsed = time.monotonic() - started
        finally:
            release_metrics.set()
            release_timer.cancel()
            metrics_thread.join(timeout=2.0)

    assert ready.status_code == 200
    assert ready_elapsed < 0.2
    assert len(metrics_response) == 1
    assert metrics_response[0].status_code == 200


def test_workers_probe_fails_closed_when_persisted_heartbeat_is_stale() -> None:
    state = _ProbeState(
        runtime_id=RUNTIME_ID,
        runtime_version="v2",
        started_at_ms=1_000,
        clock_ms=lambda: 16_001,
        lifecycle_state="running",
        heartbeat_at_ms=1_000,
        ready=True,
        unavailable_reason="",
    )

    assert state.payload()["ok"] is False
    assert state.payload()["unavailable_reason"] == "runtime_heartbeat_stale"


def test_steady_and_maintenance_locks_are_mutually_exclusive(tmp_path) -> None:
    prepare_postgres_database()
    settings = Settings(storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    first = WorkerDatabase.create(settings)
    second = WorkerDatabase.create(settings)
    steady_lock = first.acquire_steady_runtime_lock()
    try:
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


@pytest.mark.parametrize(
    ("row", "query_failed", "state", "reason"),
    [
        (None, False, "unavailable", "runtime_missing"),
        (None, True, "unavailable", "runtime_status_query_failed"),
        (
            {
                "runtime_id": RUNTIME_ID,
                "runtime_version": "v2",
                "lifecycle_state": "running",
                "started_at_ms": 1_000,
                "heartbeat_at_ms": 1_000,
                "fatal_code": None,
            },
            False,
            "stale",
            "runtime_heartbeat_stale",
        ),
    ],
)
def test_workers_runtime_status_truth_table(row, query_failed, state, reason) -> None:
    status = workers_runtime_status(row, now_ms=20_000, query_failed=query_failed)
    assert status["state"] == state
    assert status["unavailable_reason"] == reason
    assert status["heartbeat_stale_after_ms"] == 15_000


def test_real_workers_process_gracefully_stops_and_closes_probe() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("inert", port)
    try:
        _wait_ready(process, port)
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
        assert time.monotonic() - started < 5.0
        _assert_probe_closed(port)
        row = _runtime_row()
        assert row["lifecycle_state"] == "stopped"
        assert row["fatal_code"] is None
    finally:
        _ensure_process_stopped(process)


def test_real_workers_child_failure_is_fatal_within_five_seconds() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("child_failure", port)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "ABOUT_TO_FAIL")
        started = time.monotonic()
        assert process.wait(timeout=5.0) != 0
        assert time.monotonic() - started < 5.0
        _assert_probe_closed(port)
        row = _runtime_row()
        assert row["lifecycle_state"] == "failed"
        assert row["fatal_code"] == "child_failed"
    finally:
        _ensure_process_stopped(process)


def test_real_workers_pinned_session_loss_is_fatal_within_five_seconds() -> None:
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

        started = time.monotonic()
        assert process.wait(timeout=5.0) != 0
        assert time.monotonic() - started < 5.0
        _assert_probe_closed(port)
        runtime = _runtime_row()
        assert runtime["lifecycle_state"] == "failed"
        assert runtime["fatal_code"] == "singleton_lost"
    finally:
        _ensure_process_stopped(process)


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


def test_real_workers_never_returning_model_operation_is_fatal() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("model_overrun", port)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "MODEL_STARTED")
        assert process.wait(timeout=6.0) != 0
        _assert_probe_closed(port)
        row = _runtime_row()
        assert row["lifecycle_state"] == "failed"
        assert row["fatal_code"] == "resource_operation_overrun"
    finally:
        _ensure_process_stopped(process)


def test_real_workers_never_returning_control_operation_is_fatal() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("control_overrun", port)
    try:
        _wait_for_output(process, "CONTROL_STARTED", timeout_seconds=15.0)
        assert process.wait(timeout=6.0) != 0
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


def test_real_workers_news_native_timeouts_recover_without_root_restart() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("news_bounded_recovery", port)
    try:
        _wait_ready(process, port)
        _wait_for_outputs(
            process,
            (
                "NEWS_PUSH_BOUNDED_TIMEOUT",
                "NEWS_PUSH_RECOVERED",
                "NEWS_STORY_BOUNDED_TIMEOUT",
                "NEWS_STORY_RECOVERED",
            ),
            timeout_seconds=12.0,
        )
        assert process.poll() is None
        row = _runtime_row()
        assert row["lifecycle_state"] == "running"
        assert row["fatal_code"] is None

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
        row = _runtime_row()
        assert row["lifecycle_state"] == "stopped"
        assert row["fatal_code"] is None
    finally:
        _ensure_process_stopped(process)


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


def test_real_workers_absolute_graceful_deadline_covers_never_returning_future() -> None:
    prepare_postgres_database()
    port = _free_port()
    process = _start_workers_process("finite_never_returns", port)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "FINITE_STARTED")
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=32.0) != 0
        elapsed = time.monotonic() - started
        assert 29.0 <= elapsed <= 32.0
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


def _start_workers_process(mode: str, port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            str(_PROCESS_ENTRY),
            "--dsn",
            _test_postgres_dsn(),
            "--port",
            str(port),
            "--mode",
            mode,
        ],
        cwd=_PROCESS_ENTRY.parents[2],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
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
        row = conn.execute("SELECT lifecycle_state, fatal_code FROM workers_runtime WHERE singleton_key").fetchone()
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
