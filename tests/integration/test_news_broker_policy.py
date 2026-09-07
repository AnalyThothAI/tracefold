"""Broker-policy drift at the Workers attach is an observation, against a real RabbitMQ.

Retry lives in the broker policy (#400), so the drift these tests create is real: without the
checked-in policy a queue runs immediate redelivery, the quorum default delivery limit and
at-most-once dead lettering. What changed is the response. The attach used to raise
`BrokerPolicyMismatch` out of `_connect_news_bus`, which stopped News entirely -- no ingestion, no
triage, no delivery, no push -- and left the operator a dead process to read the reason out of.
It now logs the mismatch, sets a gauge, and consumes anyway (#598 D5-e): a degraded retry contract
and a running product, which is the smaller failure and the one somebody can see.

`tracefold news bus verify` and `verify_policies` itself stay fail-closed; a diagnostic that answers
"yes" while the broker is drifted is worthless. That half is pinned in `test_news_bus_rabbitmq.py`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest

from tracefold.app.workers.wiring.news import _connect_news_bus
from tracefold.integrations.rabbitmq import (
    POLICY_EFFECTIVE_TIMEOUT_SECONDS,
    BrokerPolicyMismatch,
    RabbitMQBus,
    topology,
)
from tracefold.platform.config.models import Settings
from tracefold.platform.observability import TelemetryRegistry

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("rabbitmq_url")]

AMQP_URL = os.environ.get("TRACEFOLD_TEST_AMQP_URL", "amqp://tracefold:tracefold@127.0.0.1:5672/")
_AMQP = urlsplit(AMQP_URL)
MANAGEMENT_URL = os.environ.get(
    "TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL",
    f"http://{_AMQP.hostname or '127.0.0.1'}:15672",
).rstrip("/")
# The production settle bound is 30 s, and it is not what these tests are about: they are about what
# happens at the end of it. The drift, the queues, the management read and the attach are all real.
SETTLE_SECONDS = 2.0


@pytest.fixture
def prefix() -> Iterator[str]:
    """One disposable prefix per test, deleted with its queues, exchanges and policies."""

    name = f"tf_test_{uuid.uuid4().hex[:8]}"
    yield name
    asyncio.run(_delete(name))


async def _delete(prefix: str) -> None:
    bus = RabbitMQBus(url=AMQP_URL, name_prefix=prefix, connect_timeout_seconds=5, management_url=MANAGEMENT_URL)
    try:
        await bus.delete_topology()
    finally:
        await bus.close()


def _settings(prefix: str) -> Settings:
    return Settings(
        news={"broker": {"url": AMQP_URL, "management_url": MANAGEMENT_URL, "name_prefix": prefix}},
    )


def _short_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tracefold.integrations.rabbitmq.POLICY_EFFECTIVE_TIMEOUT_SECONDS", SETTLE_SECONDS)


def _drift_gauge(telemetry: TelemetryRegistry) -> str:
    """The rendered Prometheus line, which is what a scrape would actually see."""

    return next(
        line
        for line in telemetry.render_prometheus_text().splitlines()
        if line.startswith("tracefold_news_broker_policy_drift ")
    )


def test_workers_attaches_to_a_broker_whose_policy_has_drifted(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    """No policy was ever applied for this prefix, so every declared queue is ungoverned."""

    _short_settle(monkeypatch)
    telemetry = TelemetryRegistry()

    async def scenario() -> None:
        bus = await _connect_news_bus(_settings(prefix), telemetry=telemetry)
        try:
            # It came back connected, and the topology it declared is usable: this is the process
            # that used to be dead at this point.
            depths = await bus.queue_depths()
            assert depths, "the attach returned a bus with no declared queues"
            # The diagnostic is unchanged, and still refuses.
            with pytest.raises(BrokerPolicyMismatch, match="news_broker_policy_mismatch"):
                await bus.verify_policies()
        finally:
            await bus.close()

    asyncio.run(scenario())

    assert _drift_gauge(telemetry) == "tracefold_news_broker_policy_drift 1.0"


def test_a_governed_broker_reports_no_drift(prefix: str) -> None:
    """The same attach against the checked-in policy: no error, and the gauge says so.

    This one waits out the real settle bound rather than shortening it, because that bound is what
    the first boot against a fresh broker actually spends: the management API publishes a freshly
    declared queue's effective policy on its own statistics interval, and a drift report inside that
    window would be a lie about a correctly provisioned broker.
    """

    telemetry = TelemetryRegistry()

    async def scenario() -> None:
        provisioner = RabbitMQBus(
            url=AMQP_URL, name_prefix=prefix, connect_timeout_seconds=5, management_url=MANAGEMENT_URL
        )
        try:
            await provisioner.connect()
            await provisioner.apply_policies()
            await provisioner.verify_policies(settle_timeout_seconds=POLICY_EFFECTIVE_TIMEOUT_SECONDS)
        finally:
            await provisioner.close()

        bus = await _connect_news_bus(_settings(prefix), telemetry=telemetry)
        try:
            assert await bus.verify_policies() == {"verified": sorted(topology(prefix).queue_names)}
        finally:
            await bus.close()

    asyncio.run(scenario())

    assert _drift_gauge(telemetry) == "tracefold_news_broker_policy_drift 0.0"
