"""The RabbitMQ a broker test may touch: only one the run declared disposable.

There is no default broker. On an operator machine `127.0.0.1:5672` is the live deployment, and a default
there let every local broker test declare topology, import policies and delete them on it — which is how
the live vhost once held ~500 abandoned `tf_*` queues. A run that wants broker coverage declares
`TRACEFOLD_TEST_AMQP_URL` (and, for any broker not on the default port, its management URL).
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

AMQP_URL_ENV = "TRACEFOLD_TEST_AMQP_URL"
MANAGEMENT_URL_ENV = "TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL"
DEFAULT_AMQP_PORT = 5672
UNDECLARED_MESSAGE = (
    f"no disposable RabbitMQ declared (set {AMQP_URL_ENV}, and {MANAGEMENT_URL_ENV} for a non-default port); "
    "broker tests never fall back to the local 5672, which on an operator host is the live deployment"
)


def declared_amqp_url() -> str:
    """The broker this run declared, or an empty string when it declared none."""

    return os.environ.get(AMQP_URL_ENV, "").strip()


def rabbitmq_management_url(amqp_url: str) -> str:
    """The management API of the broker at ``amqp_url``, never a guess about a different broker.

    Pairing a disposable broker's AMQP port with ``host:15672`` silently reaches whichever broker owns 15672
    on that host, so a non-default AMQP port must name its management URL explicitly.
    """

    explicit = os.environ.get(MANAGEMENT_URL_ENV, "").strip()
    if explicit:
        return explicit.rstrip("/")
    parsed = urlsplit(amqp_url)
    if (parsed.port or DEFAULT_AMQP_PORT) != DEFAULT_AMQP_PORT:
        raise RuntimeError(
            f"{MANAGEMENT_URL_ENV} is required for a broker on AMQP port {parsed.port}; "
            "refusing to assume its management API is on 15672"
        )
    return f"http://{parsed.hostname or '127.0.0.1'}:15672"


def declared_management_url(amqp_url: str) -> str:
    """The management API of a declared broker, or an empty string when none was declared."""

    return rabbitmq_management_url(amqp_url) if amqp_url else ""
