from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import testcontainers.postgres

from tests import postgres_test_utils
from tests import tracefold_postgres_container as postgres_container_helper
from tests.e2e import conftest as e2e_conftest


def test_e2e_postgres_uses_container_start_as_its_only_docker_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePostgresContainer:
        pass

    source_dsn = "postgresql+psycopg2://postgres:postgres@localhost/tracefold_test"
    expected_dsn = source_dsn.replace("postgresql+psycopg2://", "postgresql://")
    upgraded: list[str] = []

    @contextmanager
    def fake_container(postgres_container_cls: type[FakePostgresContainer]):
        assert postgres_container_cls is FakePostgresContainer
        yield SimpleNamespace(get_connection_url=lambda: source_dsn)

    def reject_redundant_probe(*args: object, **kwargs: object) -> None:
        raise AssertionError("the E2E fixture ran a Docker probe before starting its container")

    monkeypatch.setattr(e2e_conftest.subprocess, "run", reject_redundant_probe)
    monkeypatch.setattr(testcontainers.postgres, "PostgresContainer", FakePostgresContainer)
    monkeypatch.setattr(postgres_container_helper, "tracefold_postgres_container", fake_container)
    monkeypatch.setattr(postgres_test_utils, "upgrade_test_head", upgraded.append)

    fixture = e2e_conftest.e2e_postgres.__wrapped__()
    assert next(fixture) == expected_dsn
    fixture.close()
    assert upgraded == [expected_dsn]
