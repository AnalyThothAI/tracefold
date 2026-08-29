"""Compose service hosts seen from the developer machine.

Inside the compose network the services are ``postgres`` and ``rabbitmq``; on the host they are the
loopback ports compose publishes. Runtime code translates once, so one ``config.yaml`` serves both.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HOST_LOOPBACK = "127.0.0.1"
COMPOSE_POSTGRES_HOST = "postgres"
COMPOSE_RABBITMQ_HOST = "rabbitmq"
DEFAULT_HOST_POSTGRES_PORT = "56532"
DEFAULT_HOST_RABBITMQ_PORT = "5672"


def running_in_container() -> bool:
    return Path("/.dockerenv").exists()


def host_postgres_port() -> str:
    return os.environ.get("TRACEFOLD_POSTGRES_PORT") or DEFAULT_HOST_POSTGRES_PORT


def host_rabbitmq_port() -> str:
    return os.environ.get("TRACEFOLD_RABBITMQ_PORT") or DEFAULT_HOST_RABBITMQ_PORT


def loopback_url_for_compose_host(url: str, *, compose_host: str, host_port: str) -> str:
    """Rewrite ``scheme://user:pass@<compose_host>[:port]/...`` to the loopback published port."""
    parsed = urlsplit(url)
    if parsed.hostname != compose_host:
        return url
    userinfo, separator, _ = parsed.netloc.rpartition("@")  # keep credentials byte-for-byte (already encoded)
    auth = f"{userinfo}{separator}"
    host = f"{HOST_LOOPBACK}:{host_port}"
    return urlunsplit((parsed.scheme, f"{auth}{host}", parsed.path, parsed.query, parsed.fragment))


def local_docker_host_amqp_url(url: str) -> str:
    if running_in_container():
        return url
    return loopback_url_for_compose_host(url, compose_host=COMPOSE_RABBITMQ_HOST, host_port=host_rabbitmq_port())


__all__ = [
    "COMPOSE_POSTGRES_HOST",
    "COMPOSE_RABBITMQ_HOST",
    "DEFAULT_HOST_POSTGRES_PORT",
    "DEFAULT_HOST_RABBITMQ_PORT",
    "HOST_LOOPBACK",
    "host_postgres_port",
    "host_rabbitmq_port",
    "local_docker_host_amqp_url",
    "loopback_url_for_compose_host",
    "running_in_container",
]
