# tests/golden/conftest.py
"""Auto-mark golden corpus tests as @pytest.mark.golden.

These tests run a real ingest -> projection pipeline against a real Postgres,
so they keep a dedicated marker instead of sharing the service-level e2e lane.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg
import pytest

DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:55432/tracefold_test"
DEFAULT_AMQP_URL = "amqp://tracefold:tracefold@127.0.0.1:5672/"
GOLDEN_WS_TOKEN = "golden-token"
ROOT = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/golden/" in str(item.path):
            item.add_marker(pytest.mark.golden)


def _dsn_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=False).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _amqp_reachable(url: str) -> bool:
    parsed = urlsplit(url)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 5672), timeout=1.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def golden_rabbitmq_url() -> str:
    url = os.environ.get("TRACEFOLD_TEST_AMQP_URL", DEFAULT_AMQP_URL)
    if not _amqp_reachable(url):
        pytest.fail(
            f"Golden tests require RabbitMQ at {url}; start the declared broker or set TRACEFOLD_TEST_AMQP_URL.",
            pytrace=False,
        )
    return url


@pytest.fixture(scope="session")
def golden_postgres_dsn() -> Iterator[str]:
    existing = os.environ.get("TRACEFOLD_TEST_POSTGRES_DSN", DEFAULT_DSN)

    if _dsn_reachable(existing):
        from tests.postgres_test_utils import ensure_migrated_postgres_resource

        ensure_migrated_postgres_resource(existing, resource_name="PostgreSQL golden resource")
        os.environ["TRACEFOLD_TEST_POSTGRES_DSN"] = existing
        yield existing
        return

    if not _docker_available():
        pytest.fail(
            "Golden tests require a reachable Postgres but none was found. Fix options:\n"
            f"  1. Start your local test DB at {existing}.\n"
            "  2. Provide TRACEFOLD_TEST_POSTGRES_DSN=postgresql://...\n"
            "  3. Start Docker Desktop / colima / OrbStack and rerun.\n"
            "  4. Do not bypass this lane with an environment skip; an unavailable dependency is a failed gate.",
            pytrace=False,
        )

    from testcontainers.postgres import PostgresContainer

    from tests.postgres_test_utils import ensure_migrated_postgres_resource
    from tests.tracefold_postgres_container import tracefold_postgres_container

    with tracefold_postgres_container(PostgresContainer) as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        ensure_migrated_postgres_resource(dsn, resource_name="testcontainers PostgreSQL golden resource")
        os.environ["TRACEFOLD_TEST_POSTGRES_DSN"] = dsn
        yield dsn


@dataclass(frozen=True, slots=True)
class GoldenRuntime:
    base_url: str
    amqp_url: str
    name_prefix: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {GOLDEN_WS_TOKEN}"}

    def publish_opennews(self, params: dict[str, object]) -> None:
        asyncio.run(_publish_opennews(self.amqp_url, self.name_prefix, params))

    def queue_depths(self) -> dict[str, int]:
        return asyncio.run(_wait_for_empty_queues(self.amqp_url, self.name_prefix))


@pytest.fixture
def golden_runtime(
    golden_postgres_dsn: str,
    golden_rabbitmq_url: str,
    tmp_path: Path,
) -> Iterator[GoldenRuntime]:
    """Run the public Workers root and a real HTTP server against one isolated broker topology."""

    _reset_postgres(golden_postgres_dsn)
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    name_prefix = f"tf_golden_{uuid.uuid4().hex[:10]}"
    worker_port = _unused_port()
    worker_log = tmp_path / "workers.log"
    worker_fp = worker_log.open("w", encoding="utf-8")
    worker = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "tests.golden._workers_entry",
            "--dsn",
            golden_postgres_dsn,
            "--amqp-url",
            golden_rabbitmq_url,
            "--name-prefix",
            name_prefix,
            "--probe-port",
            str(worker_port),
            "--app-home",
            str(app_home),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        stdout=worker_fp,
        stderr=subprocess.STDOUT,
    )

    serve_log = tmp_path / "serve.log"
    serve_fp = serve_log.open("w", encoding="utf-8")
    serve = subprocess.Popen(
        [sys.executable, "-u", "-m", "tests.e2e._uvicorn_entry", "--port", "0"],
        cwd=ROOT,
        env={
            **os.environ,
            "TRACEFOLD_POSTGRES_DSN": golden_postgres_dsn,
            "TRACEFOLD_E2E_WS_TOKEN": GOLDEN_WS_TOKEN,
            "TRACEFOLD_E2E_APP_HOME": str(app_home),
            "PYTHONPATH": str(ROOT / "src"),
        },
        stdout=serve_fp,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_for_http(f"http://127.0.0.1:{worker_port}/readyz", worker, worker_log, timeout=60.0)
        serve_port = _wait_for_logged_port(serve_log, serve, timeout=60.0)
        base_url = f"http://127.0.0.1:{serve_port}"
        _wait_for_http(f"{base_url}/readyz", serve, serve_log, timeout=60.0)
        yield GoldenRuntime(base_url=base_url, amqp_url=golden_rabbitmq_url, name_prefix=name_prefix)
        _assert_running(worker, worker_log)
        _assert_running(serve, serve_log)
        _assert_http_ready(
            f"http://127.0.0.1:{worker_port}/readyz",
            worker,
            worker_log,
        )
        _assert_http_ready(f"{base_url}/readyz", serve, serve_log)
    finally:
        _terminate(serve)
        _terminate(worker, timeout=35.0)
        serve_fp.close()
        worker_fp.close()
        asyncio.run(_delete_topology(golden_rabbitmq_url, name_prefix))


def _reset_postgres(dsn: str) -> None:
    from tests.postgres_test_utils import reset_postgres_database

    reset_postgres_database(dsn)


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_logged_port(log_path: Path, proc: subprocess.Popen[bytes], *, timeout: float) -> int:
    import re

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        match = re.search(r"^READY port=(\d+)$", text, re.MULTILINE)
        if match:
            return int(match.group(1))
        _assert_running(proc, log_path)
        time.sleep(0.1)
    raise AssertionError(f"serve did not report a port:\n{_log(log_path)}")


def _wait_for_http(url: str, proc: subprocess.Popen[bytes], log_path: Path, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        _assert_running(proc, log_path)
        try:
            response = httpx.get(url, timeout=1.0, trust_env=False)
            last = f"{response.status_code} {response.text[:200]}"
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(0.1)
    raise AssertionError(f"{url} never became ready ({last}):\n{_log(log_path)}")


def _assert_http_ready(url: str, proc: subprocess.Popen[bytes], log_path: Path) -> None:
    """Require a running process and one final successful readiness response."""

    _assert_running(proc, log_path)
    try:
        response = httpx.get(url, timeout=5.0, trust_env=False)
    except httpx.HTTPError as exc:
        _assert_running(proc, log_path)
        raise AssertionError(f"{url} final readiness request failed ({type(exc).__name__}):\n{_log(log_path)}") from exc
    _assert_running(proc, log_path)
    if response.status_code != 200:
        raise AssertionError(
            f"{url} final readiness failed ({response.status_code} {response.text[:200]}):\n{_log(log_path)}"
        )


def _assert_running(proc: subprocess.Popen[bytes], log_path: Path) -> None:
    if proc.poll() is not None:
        raise AssertionError(f"subprocess exited rc={proc.returncode}:\n{_log(log_path)}")


def _log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _terminate(proc: subprocess.Popen[bytes], *, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


async def _publish_opennews(amqp_url: str, name_prefix: str, params: dict[str, object]) -> None:
    from tracefold.integrations.rabbitmq import RabbitMQBus
    from tracefold.news.bus import RK_RAW_LIVE, BusMessage, new_trace_id, now_ms

    stamp = now_ms()
    bus = RabbitMQBus(url=amqp_url, name_prefix=name_prefix, connect_timeout_seconds=5.0)
    await bus.connect()
    try:
        await bus.publish(
            BusMessage(
                kind="raw",
                message_id=f"raw:{params['id']}",
                routing_key=RK_RAW_LIVE.format(strategy_id="1019"),
                payload={
                    "params": params,
                    "strategy_id": "1019",
                    "ingest_mode": "live",
                    "observed_at_ms": stamp,
                },
                trace_id=new_trace_id(),
                occurred_at_ms=stamp,
            )
        )
    finally:
        await bus.close()


async def _wait_for_empty_queues(amqp_url: str, name_prefix: str) -> dict[str, int]:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    bus = RabbitMQBus(url=amqp_url, name_prefix=name_prefix, connect_timeout_seconds=5.0)
    await bus.connect()
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            raw = await bus.queue_depths()
            depths = {name.removeprefix(f"{name_prefix}."): int(value["messages"]) for name, value in raw.items()}
            if not any(depths.values()) or asyncio.get_running_loop().time() >= deadline:
                return depths
            await asyncio.sleep(0.05)
    finally:
        await bus.close()


async def _delete_topology(amqp_url: str, name_prefix: str) -> None:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    bus = RabbitMQBus(url=amqp_url, name_prefix=name_prefix, connect_timeout_seconds=5.0)
    try:
        await bus.connect()
        await bus.delete_topology()
    finally:
        await bus.close()
