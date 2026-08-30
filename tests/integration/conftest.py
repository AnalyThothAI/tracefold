# tests/integration/conftest.py
"""Explicit integration-test resources."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from urllib.parse import urlsplit

import psycopg
import pytest

DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:55432/tracefold_test"
DEFAULT_AMQP_URL = "amqp://tracefold:tracefold@127.0.0.1:5672/"


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
def rabbitmq_url() -> str:
    """Return a reachable broker only to integration tests that declare it."""

    url = os.environ.get("TRACEFOLD_TEST_AMQP_URL", DEFAULT_AMQP_URL)
    parsed = urlsplit(url)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 5672), timeout=1.5):
            return url
    except OSError:
        message = f"RabbitMQ test broker is not reachable at {url}"
        if os.environ.get("TRACEFOLD_TEST_RESOURCES_REQUIRED") == "1":
            pytest.fail(message + " (required test resource)", pytrace=False)
        pytest.skip(message + " (local convenience skip; not complete verification)")


@pytest.fixture(scope="session")
def restartable_rabbitmq(rabbitmq_url: str) -> str:
    """The container a test may restart, named explicitly by the operator or by CI.

    Restarting a broker is destructive to whoever else is using it, so a test never discovers one on its
    own: the default local broker is the operator's running deployment. `TRACEFOLD_TEST_RABBITMQ_CONTAINER`
    is the declaration that this particular broker is disposable. When it names a container that is not
    running, that is a broken declaration and fails rather than skipping.
    """

    del rabbitmq_url
    name = os.environ.get("TRACEFOLD_TEST_RABBITMQ_CONTAINER", "").strip()
    if not name:
        message = "no disposable broker declared (set TRACEFOLD_TEST_RABBITMQ_CONTAINER)"
        if os.environ.get("TRACEFOLD_TEST_RESOURCES_REQUIRED") == "1":
            pytest.fail(message + " (required test resource)", pytrace=False)
        pytest.skip(message + "; a test must never restart the operator's own broker")
    probe = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "running":
        pytest.fail(f"TRACEFOLD_TEST_RABBITMQ_CONTAINER={name} is not a running container", pytrace=False)
    return name


@pytest.fixture(scope="session")
def postgres_server_dsn() -> Iterator[str]:
    """Yield one reachable dedicated server database without choosing a test isolation policy."""
    existing = os.environ.get("TRACEFOLD_TEST_POSTGRES_DSN", DEFAULT_DSN)

    if _dsn_reachable(existing):
        os.environ["TRACEFOLD_TEST_POSTGRES_DSN"] = existing
        yield existing
        return

    if os.environ.get("SKIP_INTEGRATION") == "1":
        if os.environ.get("TRACEFOLD_TEST_RESOURCES_REQUIRED") == "1":
            pytest.fail("SKIP_INTEGRATION cannot disable a required PostgreSQL run", pytrace=False)
        pytest.skip(
            "SKIP_INTEGRATION=1 set; integration tests skipped (this run is not complete verification)",
            allow_module_level=True,
        )

    if not _docker_available():
        pytest.fail(
            "Integration tests require a reachable Postgres but none was found. Fix options:\n"
            f"  1. Start your local test DB at {existing} (e.g. `docker compose up -d postgres`).\n"
            "  2. Provide an alternate DSN: TRACEFOLD_TEST_POSTGRES_DSN=postgresql://...\n"
            "  3. Start Docker Desktop / colima / OrbStack and rerun (testcontainers will auto-spin).\n"
            "  4. If you intentionally cannot run integration, set SKIP_INTEGRATION=1 -- but then\n"
            "     this run cannot count as a verification artefact (DoD: see docs/DEVELOPMENT.md).",
            pytrace=False,
        )

    # Spin testcontainers
    from testcontainers.postgres import PostgresContainer

    from tests.tracefold_postgres_container import tracefold_postgres_container

    with tracefold_postgres_container(PostgresContainer) as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        os.environ["TRACEFOLD_TEST_POSTGRES_DSN"] = dsn
        yield dsn


@pytest.fixture(scope="session")
def postgres_clone_factory(postgres_server_dsn: str):
    """Own one migrated baseline for behavior tests that require committed isolation."""
    from tests.postgres_test_utils import MigratedPostgresCloneFactory

    factory = MigratedPostgresCloneFactory(postgres_server_dsn)
    try:
        yield factory
    finally:
        factory.close()


@pytest.fixture()
def postgres_clone_dsn(postgres_clone_factory) -> Iterator[str]:
    """Give one test an isolated clone and route helper-based connections to it."""
    with _routed_postgres_clone(postgres_clone_factory) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def postgres_module_clone_dsn(postgres_clone_factory) -> Iterator[str]:
    """Give one behavior-test module a private migrated database."""
    with _routed_postgres_clone(postgres_clone_factory) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def postgres_migration_dsn(postgres_server_dsn: str) -> Iterator[str]:
    """Give historical migration owners an empty database without the head shortcut."""
    from tests.postgres_test_utils import temporary_unmigrated_postgres_database

    with _routed_postgres_database(temporary_unmigrated_postgres_database(postgres_server_dsn)) as dsn:
        yield dsn


@contextmanager
def _routed_postgres_clone(postgres_clone_factory) -> Iterator[str]:
    with _routed_postgres_database(postgres_clone_factory.clone()) as dsn:
        yield dsn


@contextmanager
def _routed_postgres_database(database: AbstractContextManager[str]) -> Iterator[str]:
    previous = os.environ.get("TRACEFOLD_TEST_POSTGRES_DSN")
    with database as dsn:
        os.environ["TRACEFOLD_TEST_POSTGRES_DSN"] = dsn
        try:
            yield dsn
        finally:
            if previous is None:
                os.environ.pop("TRACEFOLD_TEST_POSTGRES_DSN", None)
            else:
                os.environ["TRACEFOLD_TEST_POSTGRES_DSN"] = previous
