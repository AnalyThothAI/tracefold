"""RabbitMQ adapter tests: topology, bounded-concurrency consume, retry/defer lanes, DLQ tooling, teardown.

Requires a broker at TRACEFOLD_TEST_AMQP_URL (default amqp://tracefold:tracefold@127.0.0.1:5672/,
the compose broker); skips otherwise. Every test declares its own prefixed topology and deletes it.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest

from tracefold.integrations.rabbitmq import RabbitMQBus, topology
from tracefold.news.bus import (
    Q_DEAD,
    Q_TRIAGE,
    BusMessage,
    DeferError,
    PermanentError,
    TransientError,
    new_trace_id,
)

pytestmark = pytest.mark.integration

AMQP_URL = os.environ.get("TRACEFOLD_TEST_AMQP_URL", "amqp://tracefold:tracefold@127.0.0.1:5672/")


def _broker_reachable() -> bool:
    parsed = urlsplit(AMQP_URL)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 5672), timeout=1.5):
            return True
    except OSError:
        return False


skip_without_broker = pytest.mark.skipif(not _broker_reachable(), reason="RabbitMQ test broker not reachable")


@asynccontextmanager
async def _bus() -> AsyncIterator[RabbitMQBus]:
    prefix = f"tf_test_{uuid.uuid4().hex[:8]}"
    bus = RabbitMQBus(url=AMQP_URL, name_prefix=prefix, connect_timeout_seconds=5, retry_ttl_ms=2_000)
    await bus.connect()
    try:
        yield bus
    finally:
        deleted = await bus.delete_topology()
        assert set(deleted) >= set(topology(prefix).queue_names)
        await bus.close()


def _event(message_id: str, payload: dict[str, object], *, priority: int = 0) -> BusMessage:
    return BusMessage(
        kind="event",
        message_id=message_id,
        routing_key="event.general.normal",
        payload=payload,
        trace_id=new_trace_id(),
        occurred_at_ms=1,
        priority=priority,
    )


@skip_without_broker
def test_topology_is_four_queues_one_dlq_one_retry_lane() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            declared = await bus.declare_topology()
            names = {name.split(".", 1)[1] for name in declared["queues"]}
            assert names == {"news.raw", "news.triage", "news.deep", "news.deliver", "news.dead", "news.retry"}
            depths = await bus.queue_depths()
            assert set(depths) == set(declared["queues"])

    asyncio.run(scenario())


@skip_without_broker
def test_consume_handles_up_to_prefetch_messages_concurrently() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            seen: list[float] = []
            done = asyncio.Event()

            async def handler(message: BusMessage) -> None:
                seen.append(time.monotonic())
                await asyncio.sleep(1.0)
                if len(seen) >= 4:
                    done.set()

            stop = asyncio.Event()
            consumer = asyncio.create_task(bus.consume(Q_TRIAGE, handler, prefetch=4, stop_event=stop))
            started = time.monotonic()
            for index in range(4):
                await bus.publish(_event(f"ok:{index}", {"index": index}))
            try:
                await asyncio.wait_for(done.wait(), timeout=15)
            finally:
                stop.set()
                await asyncio.wait_for(consumer, timeout=10)
            # four 1 s handlers overlapped: all four started within the first handler's second
            assert max(seen) - started < 2.0

    asyncio.run(scenario())


@skip_without_broker
def test_transient_is_counted_defer_is_not_and_permanent_dead_letters() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            seen: list[BusMessage] = []
            finished = asyncio.Event()

            async def handler(message: BusMessage) -> None:
                seen.append(message)
                mode = message.payload.get("mode")
                if mode == "transient" and message.attempt < 2:
                    raise TransientError("boom")
                if mode == "defer" and len([m for m in seen if m.payload.get("mode") == "defer"]) < 2:
                    raise DeferError("db lane busy")
                if mode == "permanent":
                    raise PermanentError("nope")
                if (
                    any(m.attempt >= 2 for m in seen if m.payload.get("mode") == "transient")
                    and len([m for m in seen if m.payload.get("mode") == "defer"]) >= 2
                    and any(m.payload.get("mode") == "permanent" for m in seen)
                ):
                    finished.set()

            stop = asyncio.Event()
            consumer = asyncio.create_task(bus.consume(Q_TRIAGE, handler, prefetch=2, stop_event=stop))
            for mode in ("transient", "defer", "permanent"):
                await bus.publish(_event(f"{mode}:1", {"mode": mode}))
            try:
                await asyncio.wait_for(finished.wait(), timeout=25)
            finally:
                stop.set()
                await asyncio.wait_for(consumer, timeout=10)
            transient_attempts = sorted(m.attempt for m in seen if m.payload.get("mode") == "transient")
            assert transient_attempts[:2] == [1, 2]  # retry lane redelivered with an incremented attempt
            defer_attempts = [m.attempt for m in seen if m.payload.get("mode") == "defer"]
            assert defer_attempts == [1, 1]  # defer requeued through the same lane without counting
            depths = await bus.queue_depths()
            assert depths[bus.queue_name(Q_DEAD)]["messages"] == 1
            dead = await bus.dead_letters(limit=5)
            assert len(dead) == 1 and dead[0]["message_id"] == "permanent:1"
            assert dead[0]["routing_key"] == "event.general.normal" and dead[0]["reason"] == "rejected"
            # replay puts it back on the topic exchange with a fresh attempt counter, purge empties the DLQ
            assert await bus.replay_dead_letters(limit=5) == 1
            assert (await bus.queue_depths())[bus.queue_name(Q_DEAD)]["messages"] == 0
            assert (await bus.queue_depths())[bus.queue_name(Q_TRIAGE)]["messages"] == 1
            await bus.publish(_event("permanent:2", {"mode": "permanent"}))
            stop2 = asyncio.Event()

            async def reject_all(message: BusMessage) -> None:
                if len(seen) > 100:
                    return
                seen.append(message)
                raise PermanentError("nope")

            consumer2 = asyncio.create_task(bus.consume(Q_TRIAGE, reject_all, prefetch=1, stop_event=stop2))
            for _ in range(50):
                if (await bus.queue_depths())[bus.queue_name(Q_DEAD)]["messages"] >= 2:
                    break
                await asyncio.sleep(0.2)
            stop2.set()
            await asyncio.wait_for(consumer2, timeout=10)
            assert await bus.purge_dead_letters() >= 2
            assert (await bus.queue_depths())[bus.queue_name(Q_DEAD)]["messages"] == 0

    asyncio.run(scenario())
