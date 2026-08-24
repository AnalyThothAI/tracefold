# tests/golden/conftest.py
"""Auto-mark golden corpus tests as @pytest.mark.golden.

These tests run a real ingest -> projection pipeline against a real Postgres,
so they keep a dedicated marker instead of sharing the service-level e2e lane.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator

import psycopg
import pytest

DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:55432/tracefold_test"


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


@pytest.fixture(scope="session")
def golden_postgres_dsn() -> Iterator[str]:
    existing = os.environ.get("GMGN_TEST_POSTGRES_DSN", DEFAULT_DSN)

    if _dsn_reachable(existing):
        from tests.postgres_test_utils import ensure_migrated_postgres_resource

        ensure_migrated_postgres_resource(existing, resource_name="PostgreSQL golden resource")
        os.environ["GMGN_TEST_POSTGRES_DSN"] = existing
        yield existing
        return

    if not _docker_available():
        pytest.fail(
            "Golden tests require a reachable Postgres but none was found. Fix options:\n"
            f"  1. Start your local test DB at {existing}.\n"
            "  2. Provide GMGN_TEST_POSTGRES_DSN=postgresql://...\n"
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
        os.environ["GMGN_TEST_POSTGRES_DSN"] = dsn
        yield dsn
