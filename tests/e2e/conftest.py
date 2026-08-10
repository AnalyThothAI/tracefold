"""End-to-end test fixtures.

Three session-scope fixtures:
- e2e_postgres: testcontainers Postgres + alembic upgrade head
- e2e_uvicorn: subprocess running tests/e2e/_uvicorn_entry.py against e2e_postgres
- e2e_writer: callable that runs tests/e2e/_writer_entry.py to inject a synthetic event

A single ws_token (`TRACEFOLD_E2E_WS_TOKEN`, default "e2e-token") is shared by both
the uvicorn process and the writer process so HTTP/WS auth lines up.

E2E tests fail with actionable setup guidance when the runtime dependencies are
unavailable; final verification must not be satisfied by an environment skip.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
E2E_WS_TOKEN = "e2e-token"
E2E_MACRO_NOW_MS = "1785247200000"


def pytest_configure(config: pytest.Config) -> None:
    """Make sure in-process HTTP/WS clients bypass any system proxy.

    On macOS the system proxy settings are picked up by Python's urllib /
    httpx when `trust_env=True` (the default), so a request to
    `127.0.0.1:54321` gets routed through `http://127.0.0.1:1080` (or
    whatever the system has) instead of going direct. curl doesn't have this
    problem because it auto-bypasses for the loopback / localhost. Force the
    bypass explicitly so the in-process clients in test_golden_path.py
    behave the same way.
    """
    bypass = "127.0.0.1,localhost,::1"
    for var in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(var, "")
        merged = ",".join([p for p in [existing, bypass] if p])
        os.environ[var] = merged


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/e2e/" in str(item.path):
            item.add_marker(pytest.mark.e2e)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=False).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@pytest.fixture(scope="session")
def e2e_ws_token() -> str:
    return os.environ.get("TRACEFOLD_E2E_WS_TOKEN", E2E_WS_TOKEN)


@pytest.fixture(scope="session")
def e2e_frontend_dist() -> Path:
    """Build the real React application that FastAPI serves in the full-stack lane."""
    web_root = ROOT / "web"
    result = subprocess.run(
        ["npm", "run", "build"],
        cwd=str(web_root),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    dist = web_root / "dist"
    if result.returncode != 0 or not (dist / "index.html").exists():
        pytest.fail(
            "e2e frontend build failed; full-stack acceptance cannot use a mock or preview substitute.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            pytrace=False,
        )
    return dist


@pytest.fixture(scope="session")
def e2e_postgres() -> Iterator[str]:
    """Yield a Postgres DSN backed by testcontainers; alembic-migrated."""
    if not _docker_available():
        pytest.fail(
            "e2e tests require docker but `docker info` failed. Fix options:\n"
            "  1. Start Docker Desktop / colima / OrbStack and rerun.\n"
            "  2. Provide an external Postgres at GMGN_E2E_POSTGRES_DSN once that path is implemented.\n"
            "  3. Do not bypass this lane with an environment skip; an unavailable dependency is a failed gate.",
            pytrace=False,
        )

    from testcontainers.postgres import PostgresContainer

    from tests.tracefold_postgres_container import tracefold_postgres_container
    from tracefold.platform.postgres.postgres_migrations import upgrade_head

    with tracefold_postgres_container(PostgresContainer) as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        try:
            upgrade_head(dsn)
        except Exception as exc:
            pytest.fail(
                f"alembic upgrade head failed against testcontainers PG ({dsn}): {exc}",
                pytrace=False,
            )
        yield dsn


def _wait_for_readyz(url: str, timeout: float = 60.0) -> None:
    """Poll a URL for HTTP 200 by shelling out to curl.

    Notably, doing this in-process via httpx or urllib intermittently fails on
    some macOS + Python 3.13 + uvicorn combos with "Server disconnected /
    Remote end closed connection without response" even when the server is
    fully accepting connections (an external `curl` from the same machine in
    the same window returns 200). Using curl in a subprocess sidesteps that.
    """
    deadline = time.monotonic() + timeout
    last_status: str | None = None
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", url],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            last_status = result.stdout.strip()
            if last_status == "200":
                return
        except (subprocess.TimeoutExpired, OSError) as exc:
            last_status = f"err={exc}"
        time.sleep(1.0)
    raise TimeoutError(f"{url} did not return 200 within {timeout}s; last_status={last_status}")


def _wait_for_port_in_log(log_path: Path, proc: subprocess.Popen[bytes], timeout: float = 60.0) -> int:
    pattern = re.compile(r"^READY port=(\d+)$", re.MULTILINE)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            match = pattern.search(text)
            if match:
                return int(match.group(1))
        if proc.poll() is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            raise RuntimeError(f"uvicorn subprocess exited early (rc={proc.returncode}); log:\n{tail}")
        time.sleep(0.2)
    tail = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    raise TimeoutError(f"uvicorn did not signal READY within {timeout}s; log:\n{tail}")


@pytest.fixture(scope="session")
def e2e_uvicorn(
    e2e_frontend_dist: Path,
    e2e_postgres: str,
    e2e_ws_token: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Spawn uvicorn in a subprocess; yield base URL like http://127.0.0.1:PORT.

    The subprocess's stdout/stderr is redirected directly to a file (no pipe
    in the parent process) to avoid any chance of the parent buffer back-
    pressuring the child. The parent polls the log for `READY port=N`.
    """
    env = {
        **os.environ,
        "TRACEFOLD_POSTGRES_DSN": e2e_postgres,
        "TRACEFOLD_E2E_WS_TOKEN": e2e_ws_token,
        "TRACEFOLD_E2E_FIXED_NOW_MS": E2E_MACRO_NOW_MS,
        "TRACEFOLD_FRONTEND_DIST": str(e2e_frontend_dist),
        "PYTHONPATH": str(ROOT / "src"),
    }
    log_dir = tmp_path_factory.mktemp("e2e-uvicorn")
    log_path = log_dir / "uvicorn.log"
    log_fp = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "tests.e2e._uvicorn_entry", "--port", "0"],
        cwd=str(ROOT),
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
    )
    try:
        port = _wait_for_port_in_log(log_path, proc)
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_readyz(f"{base_url}/readyz")
        except TimeoutError as exc:
            tail = log_path.read_text(encoding="utf-8", errors="replace")
            raise TimeoutError(f"{exc}\nuvicorn log:\n{tail}") from exc
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log_fp.close()


@pytest.fixture(scope="session")
def e2e_writer(e2e_postgres: str, e2e_ws_token: str) -> Callable[[str, str], None]:
    """Callable: writer(event_id, text) injects one synthetic mention via IngestService."""

    def _write(event_id: str, text: str) -> None:
        env = {
            **os.environ,
            "TRACEFOLD_POSTGRES_DSN": e2e_postgres,
            "TRACEFOLD_E2E_WS_TOKEN": e2e_ws_token,
            "PYTHONPATH": str(ROOT / "src"),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.e2e._writer_entry",
                "--event-id",
                event_id,
                "--text",
                text,
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"writer subprocess failed (rc={result.returncode}).\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )

    return _write


@pytest.fixture
def e2e_token_radar_browser_release(
    e2e_postgres: str,
    e2e_uvicorn: str,
    tmp_path: Path,
) -> Callable[[], None]:
    """Run non-mock Radar browser evidence against this test's isolated stack."""
    from tests.e2e.token_radar_release import run_token_radar_browser_release

    def _run() -> None:
        run_token_radar_browser_release(
            postgres_dsn=e2e_postgres,
            base_url=e2e_uvicorn,
            coordination_dir=tmp_path,
        )

    return _run
