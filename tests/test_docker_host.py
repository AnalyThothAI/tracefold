"""Compose service hosts translate to the published loopback ports outside a container."""

from __future__ import annotations

import pytest

from tracefold.platform import docker_host


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        ("amqp://tracefold:tracefold@rabbitmq:5672/", "amqp://tracefold:tracefold@127.0.0.1:5672/"),
        ("amqps://u%40x:p%3Aw@rabbitmq/vhost?heartbeat=30", "amqps://u%40x:p%3Aw@127.0.0.1:5672/vhost?heartbeat=30"),
        ("amqp://tracefold:tracefold@broker.internal:5672/", "amqp://tracefold:tracefold@broker.internal:5672/"),
    ),
)
def test_amqp_url_translates_only_the_compose_host(monkeypatch, url: str, expected: str) -> None:
    monkeypatch.setattr(docker_host, "running_in_container", lambda: False)
    monkeypatch.delenv("TRACEFOLD_RABBITMQ_PORT", raising=False)
    assert docker_host.local_docker_host_amqp_url(url) == expected


def test_amqp_url_is_untouched_inside_a_container_and_honours_the_port_override(monkeypatch) -> None:
    monkeypatch.setattr(docker_host, "running_in_container", lambda: True)
    assert docker_host.local_docker_host_amqp_url("amqp://a:b@rabbitmq:5672/") == "amqp://a:b@rabbitmq:5672/"
    monkeypatch.setattr(docker_host, "running_in_container", lambda: False)
    monkeypatch.setenv("TRACEFOLD_RABBITMQ_PORT", "15673")
    assert docker_host.local_docker_host_amqp_url("amqp://a:b@rabbitmq/") == "amqp://a:b@127.0.0.1:15673/"
