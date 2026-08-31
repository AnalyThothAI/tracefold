import re
from pathlib import Path

import pytest
import yaml

from tracefold.integrations.rabbitmq import POLICY_EFFECTIVE_TIMEOUT_SECONDS

pytestmark = pytest.mark.deploy


def _seconds(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(value))
    assert match is not None, f"expected a plain seconds duration, got {value!r}"
    return float(match.group(1))


def _healthcheck(service: str) -> dict:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    return dict(compose["services"][service]["healthcheck"])


def test_container_healthcheck_uses_liveness_endpoint():
    compose_yaml = Path("compose.yaml").read_text()

    assert "http://127.0.0.1:8765/healthz" in compose_yaml
    assert "http://127.0.0.1:8765/readyz" not in compose_yaml


def test_workers_start_period_outlasts_the_broker_settle_it_waits_on():
    """Workers reports readiness, so its start period has to cover what readiness actually waits for.

    The attach verifies the effective policy of a topology it has just declared, and RabbitMQ publishes
    that on its own statistics interval — up to `POLICY_EFFECTIVE_TIMEOUT_SECONDS`. A start period
    shorter than that single documented wait reports a correctly starting process as unhealthy and
    fails the deploy `make up` gates on, which is what happened on 2026-08-31 with 20 s.
    """

    workers = _healthcheck("workers")

    assert _seconds(workers["start_period"]) > POLICY_EFFECTIVE_TIMEOUT_SECONDS
    # The retries are the budget *after* the start period; together they must still leave a stuck start
    # detectable well inside the Compose wait timeout the Makefile passes.
    total = _seconds(workers["start_period"]) + int(workers["retries"]) * _seconds(workers["interval"])
    assert POLICY_EFFECTIVE_TIMEOUT_SECONDS < total < 300


def test_every_probe_allows_for_the_interpreter_it_spawns():
    """Each probe starts a Python process before it makes its request; 2 s did not cover that under load."""

    for service in ("serve", "workers", "nautilus"):
        assert _seconds(_healthcheck(service)["timeout"]) >= 5.0, service


def test_postgres_is_bound_to_loopback_for_host_cli():
    compose_yaml = Path("compose.yaml").read_text()

    assert '"${TRACEFOLD_POSTGRES_HOST:-127.0.0.1}:${TRACEFOLD_POSTGRES_PORT:-56532}:5432"' in compose_yaml


def test_dormant_execution_runtime_is_excluded_from_the_default_compose_model():
    compose = yaml.safe_load(Path("compose.yaml").read_text())

    assert compose["services"]["nautilus"]["profiles"] == ["execution"]
