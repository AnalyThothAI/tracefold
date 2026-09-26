"""The RabbitMQ management endpoint a broker test may touch."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

DEFAULT_AMQP_PORT = 5672


def rabbitmq_management_url(amqp_url: str) -> str:
    """The management API of the broker at ``amqp_url``, never a guess about a different broker.

    Tests provision policies and delete topology through this endpoint. Pairing a disposable broker's AMQP
    port with ``host:15672`` silently reaches whichever broker owns 15672 on that host — on an operator
    machine, the live deployment — so a non-default AMQP port must name its management URL explicitly.
    """

    explicit = os.environ.get("TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    parsed = urlsplit(amqp_url)
    if (parsed.port or DEFAULT_AMQP_PORT) != DEFAULT_AMQP_PORT:
        raise RuntimeError(
            f"TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL is required for a broker on AMQP port {parsed.port}; "
            "refusing to assume its management API is on 15672"
        )
    return f"http://{parsed.hostname or '127.0.0.1'}:15672"
