from __future__ import annotations

import asyncio
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_settings_storage,
)
from tests.postgres_test_utils import (
    test_postgres_dsn as _test_postgres_dsn,
)
from tracefold.app.http.app import create_app
from tracefold.app.worker_database import WorkerDatabase
from tracefold.app.workers.runtime import WorkersRuntimeRepository
from tracefold.platform.config.models import Settings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

RUNTIME_ID = "00000000-0000-0000-0000-000000000099"
SECOND_RUNTIME_ID = "00000000-0000-0000-0000-000000000100"
RUNTIME_MANIFEST_BARRIER_SHA = "a" * 64
_PROCESS_ENTRY = Path(__file__).with_name("_workers_runtime_process_entry.py")
_LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def test_serve_runtime_is_read_only_composition_and_status_uses_one_runtime_row(tmp_path) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            repository = WorkersRuntimeRepository(conn)
            assert repository.begin(
                runtime_id=RUNTIME_ID,
                runtime_version="v2",
                runtime_revision="test-release",
                image_digest="sha256:test",
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
    settings = Settings(storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    first = WorkerDatabase.create(settings)
    second = WorkerDatabase.create(settings)
    steady_lock = first.acquire_steady_runtime_lock()
    try:
        assert first.worker_pool.get_stats()["pool_available"] >= 1
        with first.worker_pool.connection() as conn:
            assert conn.execute(
                "SELECT current_user AS role_name, current_setting('application_name') AS application_name"
            ).fetchone() == {
                "role_name": "tracefold",
                "application_name": "tracefold_workers",
            }
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
        repository = WorkersRuntimeRepository(conn)
        with conn.transaction():
            assert repository.begin(
                runtime_id=RUNTIME_ID,
                runtime_version="v2",
                runtime_revision="test-release",
                image_digest="sha256:test",
                started_at_ms=1_000,
                now_ms=1_000,
            )
        with conn.transaction():
            assert not repository.begin(
                runtime_id=SECOND_RUNTIME_ID,
                runtime_version="v2",
                runtime_revision="test-release",
                image_digest="sha256:test",
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
                runtime_revision="test-release",
                image_digest="sha256:test",
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
                runtime_revision="test-release",
                image_digest="sha256:test",
                started_at_ms=2_003,
                now_ms=2_003,
            )
    finally:
        conn.close()


@pytest.mark.slow
def test_real_workers_process_gracefully_stops_and_closes_probe() -> None:
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
def test_real_workers_signal_lane_never_reads_execution_credentials(tmp_path: Path) -> None:
    for name in ("binance_usdm_api_key", "binance_usdm_api_secret"):
        (tmp_path / name).symlink_to(tmp_path / f"missing-{name}")
    port = _free_port()
    process = _start_workers_process(
        "trading_enabled",
        port,
        extra_env={"TRACEFOLD_TEST_CONFIG_DIR": str(tmp_path)},
    )
    try:
        # Readiness is the Decision Plane's whole liveness statement since #520 PR-A: the lane keeps
        # no heartbeat row, and a lane that cannot advance fails the process instead.
        _wait_ready(process, port)

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_trading_wiring_fault_stays_inside_the_trading_capability(tmp_path: Path) -> None:
    """#553 PR-3. A lane that cannot be composed is a Trading fault, not a Workers fault."""

    port = _free_port()
    process = _start_workers_process(
        "trading_wiring_fault",
        port,
        extra_env={"TRACEFOLD_TEST_CONFIG_DIR": str(tmp_path)},
    )
    try:
        _wait_ready(process, port)
        capabilities = _readiness_payload(port)["capabilities"]
        assert capabilities["trading_signal_lane"] == {
            "state": "faulted",
            "reason": "trading_signal_lane_wiring_failed:RuntimeError",
        }
        assert _runtime_row()["lifecycle_state"] == "running"
        assert _runtime_capabilities()["trading_signal_lane"]["state"] == "faulted"

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_trading_lane_fault_keeps_news_fact_writes_and_reads_running(tmp_path: Path) -> None:
    """#553 PR-3 acceptance 1: inject a Trading lane exception; ingestion, writes and reads continue."""

    _create_test_fact_table()
    port = _free_port()
    process = _start_workers_process(
        "trading_lane_fault",
        port,
        extra_env={"TRACEFOLD_TEST_CONFIG_DIR": str(tmp_path)},
    )
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "TRADING_LANE_ABOUT_TO_FAIL")
        _wait_capability(port, "trading_signal_lane", "faulted")

        # The lane is gone; the process is not, and the News ingestion task beside it keeps
        # committing facts. The count has to keep moving *after* the fault, not merely be non-zero.
        after_fault = _test_fact_count()
        _wait_until(lambda: _test_fact_count() > after_fault, "News fact writes stopped with the lane")

        payload = _readiness_payload(port)
        assert payload["ok"] is True
        assert payload["capabilities"]["news_ingestion"] == {"state": "running", "reason": None}
        assert payload["capabilities"]["trading_signal_lane"] == {
            "state": "faulted",
            "reason": "trading_signal_lane:RuntimeError",
        }
        # A running Deliverer task beside a sender that could not be built is still `unavailable`:
        # declaring a task is not a claim that its capability works.
        assert payload["capabilities"]["news_delivery"] == {
            "state": "unavailable",
            "reason": "news_item_push_telegram_bot_token_unavailable",
        }
        row = _runtime_row()
        assert row["lifecycle_state"] == "running"
        assert row["fatal_code"] is None
        assert _runtime_capabilities()["trading_signal_lane"]["state"] == "faulted"

        # A faulted lane must not be read back as a reason to switch off the healthy fact APIs.
        settings = Settings(ws_token="secret", storage=postgres_settings_storage())
        settings.set_config_dir(tmp_path / "app-home")
        with TestClient(create_app(settings=settings)) as client:
            status = client.get("/api/status", headers={"Authorization": "Bearer secret"})
        assert status.status_code == 200
        runtime = status.json()["data"]["runtime"]
        assert runtime["ok"] is True
        assert runtime["workers_runtime"]["state"] == "running"
        assert runtime["workers_runtime"]["capabilities"]["trading_signal_lane"]["state"] == "faulted"

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
        assert _runtime_row()["lifecycle_state"] == "stopped"
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_push_misconfiguration_leaves_delivery_unavailable_and_everything_else_running(
    tmp_path: Path, rabbitmq_url: str
) -> None:
    """#562 §5 row 1. A push target the adapter refuses is one capability's fact, not a dead process.

    The real Workers root, the real News composition, the real broker: the only thing wrong is the
    configured Telegram chat id. Two gates used to turn that typo into a total outage -- `Settings`
    refused to validate it at all, so the process could not start and nothing was left running to say
    why, and the wiring raised on the sender it could not build. Reception, admission, triage and the
    market loop have nothing to do with a push target, and this proves they keep running and keep
    committing real facts while `news_delivery` reads `unavailable`.
    """

    import uuid

    management_url = os.environ.get(
        "TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL",
        f"http://{urllib.parse.urlsplit(rabbitmq_url).hostname or '127.0.0.1'}:15672",
    ).rstrip("/")
    name_prefix = f"tf_push_{uuid.uuid4().hex[:8]}"
    config_dir = tmp_path / "app-home"
    config_dir.mkdir()
    token_file = config_dir / "telegram_bot_token"
    token_file.write_text("123456:abcdefghijklmnopqrstuvwxyzABCDE_12345", encoding="utf-8")
    token_file.chmod(0o600)

    # Workers verifies the checked-in broker policy and refuses to consume without it; applying it is
    # the deployment's job, which this stands in for.
    asyncio.run(_apply_broker_policies(rabbitmq_url, management_url, name_prefix))
    port = _free_port()
    process = _start_workers_process(
        "push_misconfigured",
        port,
        extra_env={"TRACEFOLD_TEST_CONFIG_DIR": str(config_dir)},
        broker=(rabbitmq_url, management_url, name_prefix),
    )
    try:
        _wait_ready(process, port, timeout_seconds=60.0)
        payload = _readiness_payload(port)
        capabilities = payload["capabilities"]
        assert isinstance(capabilities, dict)
        assert capabilities["news_delivery"] == {
            "state": "unavailable",
            "reason": "news_item_push_telegram_sender_invalid",
        }
        assert capabilities["news_ingestion"] == {"state": "running", "reason": None}
        assert capabilities["market_notifications"] == {"state": "running", "reason": None}
        assert payload["ok"] is True
        assert _runtime_row()["fatal_code"] is None
        assert _runtime_capabilities()["news_delivery"]["state"] == "unavailable"

        # And the fact chain is not merely declared: one raw frame through the real broker becomes a
        # durable row, with the push target still misconfigured underneath it.
        before = _news_item_count()
        asyncio.run(_publish_raw_frame(rabbitmq_url, management_url, name_prefix))
        _wait_until(lambda: _news_item_count() > before, "admission stopped with the push target")

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10.0) == 0
        assert _runtime_row()["lifecycle_state"] == "stopped"
    finally:
        _ensure_process_stopped(process)
        asyncio.run(_delete_broker_topology(rabbitmq_url, management_url, name_prefix))


async def _apply_broker_policies(amqp_url: str, management_url: str, name_prefix: str) -> None:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    bus = RabbitMQBus(
        url=amqp_url,
        name_prefix=name_prefix,
        connect_timeout_seconds=5,
        management_url=management_url,
    )
    await bus.connect()
    try:
        await bus.apply_policies()
    finally:
        await bus.close()


async def _publish_raw_frame(amqp_url: str, management_url: str, name_prefix: str) -> None:
    from tracefold.integrations.rabbitmq import RabbitMQBus
    from tracefold.news.bus import RK_RAW_LIVE, BusMessage, new_trace_id

    bus = RabbitMQBus(
        url=amqp_url,
        name_prefix=name_prefix,
        connect_timeout_seconds=5,
        management_url=management_url,
    )
    await bus.connect()
    try:
        stamp = int(time.time() * 1_000)
        hit = {
            "id": 9_900_001,
            "newsType": "strategy",
            "engineType": "listing",
            "text": "Binance will list PUSHGATE (PUSHGATE) perpetual contracts",
            "source": "binance",
            "coins": [],
            "ts": stamp,
            "strategy": {
                "id": 1353,
                "name": "Listing and Delisting Announcements",
                "engineType": "listing",
                "sourceType": "news",
            },
        }
        await bus.publish(
            BusMessage(
                kind="raw",
                message_id="raw:9900001",
                routing_key=RK_RAW_LIVE.format(strategy_id="1353"),
                payload={
                    "params": hit,
                    "strategy_id": "1353",
                    "ingest_mode": "live",
                    "observed_at_ms": stamp,
                },
                trace_id=new_trace_id(),
                occurred_at_ms=stamp,
            )
        )
    finally:
        await bus.close()


async def _delete_broker_topology(amqp_url: str, management_url: str, name_prefix: str) -> None:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    bus = RabbitMQBus(
        url=amqp_url,
        name_prefix=name_prefix,
        connect_timeout_seconds=5,
        management_url=management_url,
    )
    await bus.connect()
    try:
        await bus.delete_topology()
    finally:
        await bus.close()


def _news_item_count() -> int:
    conn = connect_postgres_test(read_only=False)
    try:
        row = conn.execute("SELECT count(*) AS n FROM news_items").fetchone()
        assert row is not None
        return int(row["n"])
    finally:
        conn.close()


@pytest.mark.slow
def test_real_news_program_registration_fault_stays_inside_the_editorial_capability() -> None:
    """#553 PR-3 acceptance 3: the Program manifest is editorial's, and nothing else waits on it."""

    _create_test_fact_table()
    port = _free_port()
    process = _start_workers_process("manifest_registration_fault", port)
    try:
        _wait_for_output(process, "MANIFEST_REGISTRATION_ABOUT_TO_FAIL")
        _wait_ready(process, port)
        payload = _readiness_payload(port)
        assert payload["runtime_manifest_sha"] is None
        assert payload["capabilities"]["news_editorial"] == {
            "state": "faulted",
            "reason": "news_editorial_manifest_registration_failed:RuntimeError",
        }
        assert payload["capabilities"]["news_ingestion"] == {"state": "running", "reason": None}

        after_fault = _test_fact_count()
        _wait_until(lambda: _test_fact_count() > after_fault, "reception stopped with the Program")
        assert _runtime_row()["lifecycle_state"] == "running"
        assert _runtime_capabilities()["news_editorial"]["state"] == "faulted"

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_news_ingestion_task_fault_still_fails_the_workers_root() -> None:
    """#553 PR-3. Reception and admission are the information entry, not an optional capability.

    A receiver crash has always been healed by the process exiting and compose restarting it. Turning
    that into a confined `faulted` capability would replace a self-healing restart with a permanent
    ingestion outage behind a 200 `/readyz`, which is the opposite of what this PR is for.
    """

    port = _free_port()
    process = _start_workers_process("ingestion_task_fault", port)
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "INGESTION_ABOUT_TO_FAIL", timeout_seconds=20.0)
        assert process.wait(timeout=10.0) != 0
        _assert_probe_closed(port)
        row = _runtime_row()
        assert row["lifecycle_state"] == "failed"
        assert row["fatal_code"] == "child_failed"
        # And it is not reported as a capability fault: nothing pretends one lane went down.
        assert _runtime_capabilities().get("news_ingestion", {}).get("state") != "faulted"
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_shared_schema_failure_still_fails_the_workers_root() -> None:
    """#553 PR-3 acceptance 5: confining capability faults did not delete the foundation checks."""

    port = _free_port()
    process = _start_workers_process("schema_mismatch", port)
    try:
        assert process.wait(timeout=10.0) != 0
        _assert_probe_closed(port)
        # The schema gate runs before the singleton row is claimed, so the refusal leaves no runtime
        # to report on: no probe, no row, no capability report claiming anything works.
        assert _optional_runtime_row() is None
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_workers_accept_active_execution_without_owning_the_nautilus_runtime(tmp_path: Path) -> None:
    port = _free_port()
    process = _start_workers_process(
        "trading_execution_requested",
        port,
        extra_env={"TRACEFOLD_TEST_CONFIG_DIR": str(tmp_path)},
    )
    try:
        # Readiness is the Decision Plane's whole liveness statement since #520 PR-A: the lane keeps
        # no heartbeat row, and a lane that cannot advance fails the process instead.
        _wait_ready(process, port)

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_workers_startup_recovers_transient_pooled_heartbeat_failures() -> None:
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
def test_real_market_notification_task_fault_stops_only_that_task() -> None:
    """#553 PR-2's own task, through the real process: the wrapper, the registration, the capability.

    `run_market_notifications` runs the startup sweep once, ticks, and re-raises the first unexpected
    program error out of `advance()` rather than swallowing it. The Workers root then records
    `market_notifications` faulted and leaves the ingestion task beside it committing -- which is the
    whole reason the loop is an optional capability task and not part of the information entry.
    """

    _create_test_fact_table()
    port = _free_port()
    process = _start_workers_process("market_notifications_fault", port)
    try:
        _wait_ready(process, port)
        # The marker only prints on the second turn, so reaching it proves the startup sweep
        # completed and the loop ticked on the stop event at least once before it failed.
        _wait_for_output(process, "MARKET_TASK_ABOUT_TO_FAIL")
        _wait_capability(port, "market_notifications", "faulted")

        after_fault = _test_fact_count()
        _wait_until(lambda: _test_fact_count() > after_fault, "ingestion stopped with the market task")
        payload = _readiness_payload(port)
        assert payload["ok"] is True
        assert payload["capabilities"]["market_notifications"] == {
            "state": "faulted",
            "reason": "market_notifications:RuntimeError",
        }
        assert payload["capabilities"]["news_ingestion"] == {"state": "running", "reason": None}

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_chain_tape_task_fault_stops_only_that_task() -> None:
    """#572 PR-1's own task, through the real process: the wrapper, the registration, the capability.

    `run_chain_tape` ticks and re-raises the first unexpected program error out of `advance()` rather
    than swallowing it. The Workers root then records `chain_tape` faulted and leaves the ingestion task
    beside it committing -- which is the whole reason the tape is an optional capability task and not
    part of the information entry.
    """

    _create_test_fact_table()
    port = _free_port()
    process = _start_workers_process("chain_tape_fault", port)
    try:
        _wait_ready(process, port)
        # The marker only prints on the second turn, so reaching it proves the loop ticked on the stop
        # event at least once before it failed.
        _wait_for_output(process, "CHAIN_TAPE_ABOUT_TO_FAIL")
        _wait_capability(port, "chain_tape", "faulted")

        after_fault = _test_fact_count()
        _wait_until(lambda: _test_fact_count() > after_fault, "ingestion stopped with the chain tape task")
        payload = _readiness_payload(port)
        assert payload["ok"] is True
        assert payload["capabilities"]["chain_tape"] == {
            "state": "faulted",
            "reason": "chain_tape:RuntimeError",
        }
        assert payload["capabilities"]["news_ingestion"] == {"state": "running", "reason": None}

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
    finally:
        _ensure_process_stopped(process)


@pytest.mark.slow
def test_real_optional_task_fault_stops_only_that_task_and_a_restart_drains_its_backlog(
    tmp_path: Path,
) -> None:
    """#553 PR-3 acceptance 4, the shape #553 PR-2's market notification loop will register as.

    An unexpected program error in one optional task stops that task, reports it, restarts nothing,
    and leaves every other task committing. Recovery is an operator restart, and the work waiting in
    PostgreSQL is still there when the task comes back.
    """

    _create_test_fact_table()
    _create_test_backlog_table()
    resume_gate = tmp_path / "resume-optional-task"
    port = _free_port()
    process = _start_workers_process(
        "optional_task_fault",
        port,
        extra_env={"TRACEFOLD_TEST_RESUME_GATE": str(resume_gate)},
    )
    try:
        _wait_ready(process, port)
        _wait_for_output(process, "OPTIONAL_TASK_ABOUT_TO_FAIL")
        _wait_capability(port, "news_quotes", "faulted")

        after_fault = _test_fact_count()
        _wait_until(lambda: _test_fact_count() > after_fault, "healthy tasks stopped with the faulted one")
        payload = _readiness_payload(port)
        assert payload["ok"] is True
        assert payload["capabilities"]["news_quotes"] == {
            "state": "faulted",
            "reason": "news_quotes:RuntimeError",
        }
        assert payload["capabilities"]["news_ingestion"] == {"state": "running", "reason": None}
        # Nothing restarted it, so its PostgreSQL backlog is still unprocessed.
        assert _unprocessed_backlog() >= 1

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5.0) == 0
        pending_at_stop = _unprocessed_backlog()
        assert pending_at_stop >= 1
    finally:
        _ensure_process_stopped(process)

    resume_gate.touch()
    restarted = _start_workers_process(
        "optional_task_fault",
        _free_port(),
        extra_env={"TRACEFOLD_TEST_RESUME_GATE": str(resume_gate)},
    )
    try:
        _wait_for_output(restarted, "BACKLOG_DRAINED", timeout_seconds=15.0)
        _wait_until(lambda: _unprocessed_backlog() == 0, "the restarted task did not drain its backlog")
        restarted.send_signal(signal.SIGTERM)
        assert restarted.wait(timeout=5.0) == 0
    finally:
        _ensure_process_stopped(restarted)


@pytest.mark.slow
def test_real_workers_pinned_session_loss_closes_probe_and_persists_fatal_state() -> None:
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
    broker: tuple[str, str, str] | None = None,
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
    if broker is not None:
        amqp_url, management_url, name_prefix = broker
        command.extend(
            (
                "--amqp-url",
                amqp_url,
                "--management-url",
                management_url,
                "--name-prefix",
                name_prefix,
            )
        )
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


def _readiness_payload(port: int) -> dict[str, object]:
    """The Workers process's own public status document."""

    url = f"http://127.0.0.1:{port}/readyz"
    try:
        with _LOCAL_HTTP.open(url, timeout=1.0) as response:
            return dict(json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        return dict(json.loads(exc.read().decode("utf-8")))


def _wait_capability(
    port: int,
    capability: str,
    state: str,
    *,
    timeout_seconds: float = 10.0,
) -> None:
    last: object = None
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        last = _readiness_payload(port).get("capabilities", {}).get(capability)
        if isinstance(last, dict) and last.get("state") == state:
            return
        time.sleep(0.05)
    raise AssertionError(f"capability {capability} never reached {state!r}; last={last!r}")


def _wait_until(predicate: Callable[[], bool], message: str, *, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def _create_test_fact_table() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS worker_runtime_test_facts(id bigserial PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def _create_test_backlog_table() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS worker_runtime_test_backlog("
            "id bigserial PRIMARY KEY, processed boolean NOT NULL DEFAULT false)"
        )
        conn.commit()
    finally:
        conn.close()


def _test_fact_count() -> int:
    conn = connect_postgres_test(read_only=False)
    try:
        row = conn.execute("SELECT count(*) AS count FROM worker_runtime_test_facts").fetchone()
        assert row is not None
        return int(row["count"])
    finally:
        conn.close()


def _unprocessed_backlog() -> int:
    conn = connect_postgres_test(read_only=False)
    try:
        row = conn.execute("SELECT count(*) AS count FROM worker_runtime_test_backlog WHERE NOT processed").fetchone()
        assert row is not None
        return int(row["count"])
    finally:
        conn.close()


def _runtime_capabilities() -> dict[str, dict[str, object]]:
    conn = connect_postgres_test(read_only=False)
    try:
        row = conn.execute("SELECT capabilities FROM workers_runtime WHERE singleton_key").fetchone()
        assert row is not None
        return dict(row["capabilities"])
    finally:
        conn.close()


def _optional_runtime_row() -> dict[str, object] | None:
    conn = connect_postgres_test(read_only=False)
    try:
        row = conn.execute(
            "SELECT runtime_id, lifecycle_state, fatal_code FROM workers_runtime WHERE singleton_key"
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def _runtime_row() -> dict[str, object]:
    row = _optional_runtime_row()
    assert row is not None
    return row


def _ensure_process_stopped(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)
