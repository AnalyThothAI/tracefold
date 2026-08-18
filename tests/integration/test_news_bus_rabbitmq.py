"""RabbitMQ adapter tests: topology, confirmed publish, consume/ack, retry lane, control fanout.

Requires a broker at TRACEFOLD_TEST_AMQP_URL (default amqp://tracefold:tracefold@127.0.0.1:5672/, the compose broker); skips otherwise.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from urllib.parse import urlsplit

import pytest

from tracefold.integrations.rabbitmq import RabbitMQBus
from tracefold.news.bus import Q_DEAD, Q_TRIAGE, BusMessage, PermanentError, TransientError, new_trace_id

pytestmark = pytest.mark.integration

AMQP_URL = os.environ.get("TRACEFOLD_TEST_AMQP_URL", "amqp://tracefold:tracefold@127.0.0.1:5672/")


def _broker_reachable() -> bool:
    parsed = urlsplit(AMQP_URL)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 5672), timeout=1.5):
            return True
    except OSError:
        return False


@pytest.fixture
def prefix() -> str:
    return f"t{uuid.uuid4().hex[:8]}"


@pytest.mark.skipif(not _broker_reachable(), reason="RabbitMQ test broker not reachable")
def test_topology_publish_consume_retry_and_dead_letter(prefix: str) -> None:
    async def scenario() -> None:
        bus = RabbitMQBus(url=AMQP_URL, name_prefix=prefix, connect_timeout_seconds=5)
        await bus.connect()
        declared = await bus.declare_topology()
        assert f"{prefix}.news.raw" in declared["queues"] and f"{prefix}.news.retry.5s" in declared["queues"]

        seen: list[BusMessage] = []
        stop = asyncio.Event()

        async def handler(message: BusMessage) -> None:
            seen.append(message)
            if message.payload.get("mode") == "transient" and message.attempt < 2:
                raise TransientError("boom")
            if message.payload.get("mode") == "permanent":
                raise PermanentError("nope")
            if (
                len([m for m in seen if m.payload.get("mode") == "ok"]) >= 1
                and any(m.attempt >= 2 for m in seen if m.payload.get("mode") == "transient")
                and any(m.payload.get("mode") == "permanent" for m in seen)
            ):
                stop.set()

        consumer = asyncio.create_task(bus.consume(Q_TRIAGE, handler, prefetch=2, stop_event=stop))
        stamp = 1
        for mode in ("ok", "transient", "permanent"):
            await bus.publish(
                BusMessage(
                    kind="event",
                    message_id=f"{mode}:{prefix}",
                    routing_key="event.general.normal",
                    payload={"mode": mode},
                    trace_id=new_trace_id(),
                    occurred_at_ms=stamp,
                    priority=5 if mode == "ok" else 0,
                )
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=25)
        finally:
            stop.set()
            await asyncio.wait_for(consumer, timeout=10)
        attempts = sorted(m.attempt for m in seen if m.payload.get("mode") == "transient")
        assert attempts[:2] == [1, 2]  # 5s retry lane re-delivered with incremented attempt
        depths = await bus.queue_depths()
        assert depths[f"{prefix}.news.dead"]["messages"] >= 1  # permanent failure landed in the DLQ
        assert Q_DEAD in "news.dead"
        # control fanout
        got = asyncio.Event()

        async def control_handler(message: BusMessage) -> None:
            if message.payload.get("action") == "pause_delivery":
                got.set()

        cstop = asyncio.Event()
        ctask = asyncio.create_task(bus.consume_control(control_handler, stop_event=cstop))
        await asyncio.sleep(1.0)
        await bus.publish_control(
            BusMessage(
                kind="control",
                message_id="c1",
                routing_key="",
                payload={"action": "pause_delivery"},
                trace_id=new_trace_id(),
                occurred_at_ms=1,
            )
        )
        try:
            await asyncio.wait_for(got.wait(), timeout=10)
        finally:
            cstop.set()
            await asyncio.wait_for(ctask, timeout=10)
        await bus.close()

    asyncio.run(scenario())
