"""Run the required Chromium smoke against production Workers and FastAPI wiring."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:55432/tracefold_test"
DEFAULT_AMQP_URL = "amqp://tracefold:tracefold@127.0.0.1:5672/"
WS_TOKEN = "browser-smoke-token"
SERVICE_FACT = "BTC OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--playwright-json", required=True, type=Path)
    parser.add_argument("--playwright-selection", required=True, type=Path)
    options = parser.parse_args()
    dsn = os.environ.get("TRACEFOLD_TEST_POSTGRES_DSN", DEFAULT_DSN)
    amqp_url = os.environ.get("TRACEFOLD_TEST_AMQP_URL", DEFAULT_AMQP_URL)
    _require_resources(dsn, amqp_url)
    _reset_postgres(dsn)

    with tempfile.TemporaryDirectory(prefix="tracefold-browser-smoke-") as raw_tmp:
        tmp = Path(raw_tmp)
        app_home = tmp / "app-home"
        app_home.mkdir()
        name_prefix = f"tf_browser_{uuid.uuid4().hex[:10]}"
        worker_port = _unused_port()
        worker_log = tmp / "workers.log"
        serve_log = tmp / "serve.log"
        worker_fp = worker_log.open("w", encoding="utf-8")
        serve_fp = serve_log.open("w", encoding="utf-8")
        worker = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "tests.golden._workers_entry",
                "--dsn",
                dsn,
                "--amqp-url",
                amqp_url,
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
        serve = subprocess.Popen(
            [sys.executable, "-u", "-m", "tests.e2e._uvicorn_entry", "--port", "0"],
            cwd=ROOT,
            env={
                **os.environ,
                "TRACEFOLD_POSTGRES_DSN": dsn,
                "TRACEFOLD_E2E_WS_TOKEN": WS_TOKEN,
                "TRACEFOLD_E2E_APP_HOME": str(app_home),
                "TRACEFOLD_FRONTEND_DIST": str(ROOT / "web" / "dist"),
                "PYTHONPATH": str(ROOT / "src"),
            },
            stdout=serve_fp,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_http(f"http://127.0.0.1:{worker_port}/readyz", worker, worker_log)
            serve_port = _wait_for_logged_port(serve_log, serve)
            base_url = f"http://127.0.0.1:{serve_port}"
            _wait_for_http(f"{base_url}/readyz", serve, serve_log)
            asyncio.run(_publish_opennews(amqp_url, name_prefix))
            _wait_for_service_fact(base_url)
            options.playwright_json.parent.mkdir(parents=True, exist_ok=True)
            options.playwright_selection.parent.mkdir(parents=True, exist_ok=True)
            options.playwright_json.unlink(missing_ok=True)
            options.playwright_selection.unlink(missing_ok=True)
            result = subprocess.run(
                [
                    "node",
                    "node_modules/@playwright/test/cli.js",
                    "test",
                    "--config=playwright.full-stack.config.ts",
                ],
                cwd=ROOT / "web",
                env={
                    **os.environ,
                    "TRACEFOLD_FULL_STACK_URL": base_url,
                    "PLAYWRIGHT_JSON_OUTPUT_NAME": str(options.playwright_json.resolve()),
                    "TRACEFOLD_PLAYWRIGHT_SELECTION_OUTPUT": str(options.playwright_selection.resolve()),
                },
                check=False,
            )
            if result.returncode == 0:
                _assert_runtime_ready(
                    (
                        (f"http://127.0.0.1:{worker_port}/readyz", worker, worker_log),
                        (f"{base_url}/readyz", serve, serve_log),
                    )
                )
            return result.returncode
        finally:
            _terminate(serve)
            _terminate(worker, timeout=35.0)
            serve_fp.close()
            worker_fp.close()
            asyncio.run(_delete_topology(amqp_url, name_prefix))


def _require_resources(dsn: str, amqp_url: str) -> None:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            pass
    except Exception as exc:
        raise SystemExit(f"browser smoke PostgreSQL unavailable: {type(exc).__name__}") from exc
    parsed = urlsplit(amqp_url)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 5672), timeout=2):
            pass
    except OSError as exc:
        raise SystemExit(f"browser smoke RabbitMQ unavailable: {type(exc).__name__}") from exc
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        raise SystemExit("browser smoke requires the production frontend bundle; run the frontend build first")


def _reset_postgres(dsn: str) -> None:
    from tests.postgres_test_utils import reset_postgres_database

    reset_postgres_database(dsn)


async def _publish_opennews(amqp_url: str, name_prefix: str) -> None:
    from tracefold.integrations.rabbitmq import RabbitMQBus
    from tracefold.news.bus import RK_RAW_LIVE, BusMessage, new_trace_id, now_ms

    stamp = now_ms()
    message_id = int(stamp % 1_000_000_000)
    bus = RabbitMQBus(url=amqp_url, name_prefix=name_prefix, connect_timeout_seconds=5.0)
    await bus.connect()
    try:
        await bus.publish(
            BusMessage(
                kind="raw",
                message_id=f"raw:{message_id}",
                routing_key=RK_RAW_LIVE.format(strategy_id="1019"),
                payload={
                    "params": {
                        "id": message_id,
                        "newsType": "strategy",
                        "engineType": "market",
                        "text": SERVICE_FACT,
                        "source": "binance",
                        "coins": [],
                        "ts": stamp,
                        "strategy": {
                            "id": 1019,
                            "name": "OI Event Monitor",
                            "engineType": "market",
                            "sourceType": "market",
                        },
                    },
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


def _wait_for_service_fact(base_url: str) -> None:
    deadline = time.monotonic() + 30.0
    last = ""
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{base_url}/api/news/feed",
            headers={"Authorization": f"Bearer {WS_TOKEN}"},
            timeout=5.0,
            trust_env=False,
        )
        last = response.text[:500]
        if response.status_code == 200 and any(
            event.get("leader_title") == SERVICE_FACT for event in response.json()["data"]["events"]
        ):
            return
        time.sleep(0.1)
    raise AssertionError(f"service fact did not reach the HTTP feed: {last}")


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_logged_port(log_path: Path, proc: subprocess.Popen[bytes]) -> int:
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        match = re.search(r"^READY port=(\d+)$", text, re.MULTILINE)
        if match:
            return int(match.group(1))
        _assert_running(proc, log_path)
        time.sleep(0.1)
    raise AssertionError(f"serve did not report a port:\n{_log(log_path)}")


def _wait_for_http(url: str, proc: subprocess.Popen[bytes], log_path: Path) -> None:
    deadline = time.monotonic() + 60.0
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


def _assert_runtime_ready(
    checks: tuple[tuple[str, subprocess.Popen[bytes], Path], ...],
) -> None:
    """Finish all readiness reads, then prove every participating root is still alive."""

    for _, proc, log_path in checks:
        _assert_running(proc, log_path)
    for url, proc, log_path in checks:
        _assert_http_ready(url, proc, log_path)
    for _, proc, log_path in checks:
        _assert_running(proc, log_path)


def _assert_running(proc: subprocess.Popen[bytes], log_path: Path) -> None:
    if proc.poll() is not None:
        raise AssertionError(f"subprocess exited rc={proc.returncode}:\n{_log(log_path)}")


def _log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _terminate(
    proc: subprocess.Popen[bytes],
    *,
    timeout: float = 5.0,
) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


async def _delete_topology(amqp_url: str, name_prefix: str) -> None:
    from tracefold.integrations.rabbitmq import RabbitMQBus

    bus = RabbitMQBus(url=amqp_url, name_prefix=name_prefix, connect_timeout_seconds=5.0)
    try:
        await bus.connect()
        await bus.delete_topology()
    finally:
        await bus.close()


if __name__ == "__main__":
    raise SystemExit(main())
