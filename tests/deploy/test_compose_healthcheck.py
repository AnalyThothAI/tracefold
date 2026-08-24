from pathlib import Path

import pytest

pytestmark = pytest.mark.deploy


def test_container_healthcheck_uses_liveness_endpoint():
    compose_yaml = Path("compose.yaml").read_text()

    assert "http://127.0.0.1:8765/healthz" in compose_yaml
    assert "http://127.0.0.1:8765/readyz" not in compose_yaml


def test_postgres_is_bound_to_loopback_for_host_cli():
    compose_yaml = Path("compose.yaml").read_text()

    assert '"${TRACEFOLD_POSTGRES_HOST:-127.0.0.1}:${TRACEFOLD_POSTGRES_PORT:-56532}:5432"' in compose_yaml
