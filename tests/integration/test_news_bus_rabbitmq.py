"""RabbitMQ adapter tests: topology, bounded-concurrency consume, retry/defer lanes, DLQ tooling, teardown.

Requires a broker at TRACEFOLD_TEST_AMQP_URL (default amqp://tracefold:tracefold@127.0.0.1:5672/,
the compose broker). The explicit fixture fails closed in evidence mode. Every test declares its own
prefixed topology and deletes it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.request
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote, unquote, urlsplit

import aio_pika
import pytest
from aio_pika import DeliveryMode, ExchangeType

from tracefold.integrations.rabbitmq import RabbitMQBus, topology
from tracefold.news.bus import (
    Q_DEAD,
    Q_RETRY,
    Q_TRIAGE,
    RETRY_TTL_MS,
    BrokerBackpressure,
    BrokerUnavailable,
    BusMessage,
    DeferError,
    PermanentError,
    TransientError,
    new_trace_id,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("rabbitmq_url")]

AMQP_URL = os.environ.get("TRACEFOLD_TEST_AMQP_URL", "amqp://tracefold:tracefold@127.0.0.1:5672/")
_AMQP = urlsplit(AMQP_URL)
MANAGEMENT_URL = os.environ.get(
    "TRACEFOLD_TEST_RABBITMQ_MANAGEMENT_URL",
    f"http://{_AMQP.hostname or '127.0.0.1'}:15672",
).rstrip("/")


@asynccontextmanager
async def _bus(*, retry_ttl_ms: int = 2_000) -> AsyncIterator[RabbitMQBus]:
    prefix = f"tf_test_{uuid.uuid4().hex[:8]}"
    bus = RabbitMQBus(
        url=AMQP_URL,
        name_prefix=prefix,
        connect_timeout_seconds=5,
        retry_ttl_ms=retry_ttl_ms,
    )
    await bus.connect()
    try:
        yield bus
    finally:
        deleted = await bus.delete_topology()
        assert set(deleted) >= set(topology(prefix).queue_names)
        await bus.close()


def _event(
    message_id: str,
    payload: dict[str, object],
    *,
    priority: int = 0,
    routing_key: str = "event.general.normal",
) -> BusMessage:
    return BusMessage(
        kind="event",
        message_id=message_id,
        routing_key=routing_key,
        payload=payload,
        trace_id=new_trace_id(),
        occurred_at_ms=1,
        priority=priority,
    )


def _exception_leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for nested in exc.exceptions for leaf in _exception_leaves(nested)]
    return [exc]


async def _wait_for_depth(bus: RabbitMQBus, queue: str, expected: int, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if (await bus.queue_depths())[bus.queue_name(queue)]["messages"] == expected:
            return
        await asyncio.sleep(0.05)
    assert (await bus.queue_depths())[bus.queue_name(queue)]["messages"] == expected


def _management_json(path: str) -> object:
    username = unquote(_AMQP.username or "guest")
    password = unquote(_AMQP.password or "guest")
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    request = urllib.request.Request(  # noqa: S310 - test-only HTTP management endpoint
        f"{MANAGEMENT_URL}{path}",
        headers={"Authorization": f"Basic {token}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def _management_vhost() -> str:
    raw = unquote(_AMQP.path.lstrip("/"))
    return raw or "/"


def test_topology_is_three_queues_one_dlq_one_retry_lane() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            declared = await bus.declare_topology()
            names = {name.split(".", 1)[1] for name in declared["queues"]}
            assert names == {"news.raw", "news.triage", "news.deliver", "news.dead", "news.retry"}  # no news.deep (#57)
            depths = await bus.queue_depths()
            assert set(depths) == set(declared["queues"])

    asyncio.run(scenario())


def test_declarations_confirms_and_publisher_properties_match_the_runtime_contract() -> None:
    async def scenario() -> None:
        async with _bus(retry_ttl_ms=RETRY_TTL_MS) as bus:
            await bus.declare_topology()
            declared = topology(bus.prefix)
            vhost = quote(_management_vhost(), safe="")

            expected_exchanges = {
                declared.exchange: ("topic", True),
                declared.dlx: ("fanout", True),
                declared.retry_exchange: ("fanout", True),
            }
            for name, expected in expected_exchanges.items():
                exchange = _management_json(f"/api/exchanges/{vhost}/{quote(name, safe='')}")
                assert isinstance(exchange, dict)
                assert (exchange["type"], exchange["durable"]) == expected

            expected_queues = {spec.name: dict(spec.arguments) for spec in declared.queues}
            expected_queues[declared.retry_queue] = {
                "x-queue-type": "quorum",
                "x-message-ttl": RETRY_TTL_MS,
                "x-dead-letter-exchange": declared.exchange,
            }
            expected_bindings = {
                **{
                    spec.name: {(declared.exchange, key) for key in spec.bindings} or {(declared.dlx, "#")}
                    for spec in declared.queues
                },
                declared.retry_queue: {(declared.retry_exchange, "#")},
            }
            for name, arguments in expected_queues.items():
                queue = _management_json(f"/api/queues/{vhost}/{quote(name, safe='')}")
                assert isinstance(queue, dict)
                assert queue["durable"] is True
                assert queue["arguments"] == arguments
                bindings = _management_json(f"/api/queues/{vhost}/{quote(name, safe='')}/bindings")
                assert isinstance(bindings, list)
                actual = {(binding["source"], binding["routing_key"]) for binding in bindings if binding.get("source")}
                assert actual == expected_bindings[name]

            connection = await aio_pika.connect_robust(AMQP_URL, timeout=5)
            channel = await connection.channel(publisher_confirms=True)
            overflow_name = f"{bus.prefix}.confirm"
            try:
                triage = await channel.declare_queue(bus.queue_name(Q_TRIAGE), passive=True)
                published = _event("publisher-contract", {"value": 1}, priority=7)
                await bus.publish(published)
                incoming = await triage.get(timeout=5)
                assert incoming is not None
                assert incoming.content_type == "application/json"
                assert incoming.delivery_mode == DeliveryMode.PERSISTENT
                assert incoming.priority == 7
                assert incoming.message_id == published.message_id
                assert incoming.headers["x-news-attempt"] == 1
                assert incoming.headers["x-news-trace"] == published.trace_id
                await incoming.ack()

                exchange = await channel.declare_exchange(
                    declared.exchange,
                    ExchangeType.TOPIC,
                    durable=True,
                    passive=True,
                )
                overflow = await channel.declare_queue(
                    overflow_name,
                    durable=True,
                    arguments={
                        "x-queue-type": "quorum",
                        "x-max-length": 1,
                        "x-overflow": "reject-publish",
                    },
                )
                await overflow.bind(exchange, routing_key="contract.confirm")
                await bus.publish(_event("confirm:first", {}, routing_key="contract.confirm"))
                rejected = False
                for index in range(64):
                    try:
                        await bus.publish(_event(f"confirm:overflow:{index}", {}, routing_key="contract.confirm"))
                    except BrokerBackpressure:
                        rejected = True
                        break
                assert rejected, "the confirmed publisher never surfaced broker overflow"
            finally:
                await channel.queue_delete(overflow_name, if_unused=False, if_empty=False)
                await channel.close()
                await connection.close()

    asyncio.run(scenario())


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


def test_unclassified_exception_fails_consumer_without_terminal_settlement() -> None:
    async def scenario() -> None:
        async with _bus() as bus:

            async def crash(_message: BusMessage) -> None:
                raise RuntimeError("handler bug")

            consumer = asyncio.create_task(bus.consume(Q_TRIAGE, crash, prefetch=1, stop_event=asyncio.Event()))
            await bus.publish(_event("unclassified:1", {}))
            with pytest.raises(BaseExceptionGroup) as caught:
                await asyncio.wait_for(consumer, timeout=10)
            assert any(
                type(leaf) is RuntimeError and str(leaf) == "handler bug" for leaf in _exception_leaves(caught.value)
            )
            assert (await bus.queue_depths())[bus.queue_name(Q_DEAD)]["messages"] == 0
            await _wait_for_depth(bus, Q_TRIAGE, 1)

            recovered = asyncio.Event()
            stop = asyncio.Event()

            async def recover(message: BusMessage) -> None:
                assert message.message_id == "unclassified:1"
                recovered.set()
                stop.set()

            consumer2 = asyncio.create_task(bus.consume(Q_TRIAGE, recover, prefetch=1, stop_event=stop))
            await asyncio.wait_for(recovered.wait(), timeout=10)
            await asyncio.wait_for(consumer2, timeout=10)
            assert (await bus.queue_depths())[bus.queue_name(Q_DEAD)]["messages"] == 0

    asyncio.run(scenario())


def test_unroutable_retry_publish_fails_consumer_and_preserves_original() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            await bus.publish(_event("retry-unroutable:1", {}))
            connection = await aio_pika.connect_robust(AMQP_URL, timeout=5)
            channel = await connection.channel(publisher_confirms=True)
            try:
                await channel.queue_delete(bus.queue_name(Q_RETRY), if_unused=False, if_empty=False)
            finally:
                await channel.close()
                await connection.close()

            async def retry(_message: BusMessage) -> None:
                raise TransientError("try again")

            consumer = asyncio.create_task(bus.consume(Q_TRIAGE, retry, prefetch=1, stop_event=asyncio.Event()))
            with pytest.raises(BaseExceptionGroup) as caught:
                await asyncio.wait_for(consumer, timeout=10)
            assert any(isinstance(leaf, BrokerUnavailable) for leaf in _exception_leaves(caught.value))

            # Restore this test's private retry lane before passive queue inspection and recovery.
            await bus.declare_topology()
            assert (await bus.queue_depths())[bus.queue_name(Q_DEAD)]["messages"] == 0
            await _wait_for_depth(bus, Q_TRIAGE, 1)

            recovered = asyncio.Event()
            stop = asyncio.Event()

            async def recover(message: BusMessage) -> None:
                assert message.message_id == "retry-unroutable:1"
                recovered.set()
                stop.set()

            consumer2 = asyncio.create_task(bus.consume(Q_TRIAGE, recover, prefetch=1, stop_event=stop))
            await asyncio.wait_for(recovered.wait(), timeout=10)
            await asyncio.wait_for(consumer2, timeout=10)

    asyncio.run(scenario())


def test_transient_exhaustion_dead_letters_only_after_the_attempt_limit() -> None:
    async def scenario() -> None:
        async with _bus(retry_ttl_ms=100) as bus:
            attempts: list[int] = []
            stop = asyncio.Event()

            async def retry(message: BusMessage) -> None:
                attempts.append(message.attempt)
                raise TransientError("still unavailable")

            consumer = asyncio.create_task(bus.consume(Q_TRIAGE, retry, prefetch=1, stop_event=stop))
            await bus.publish(_event("transient-exhausted:1", {}))
            await _wait_for_depth(bus, Q_DEAD, 1)
            stop.set()
            await asyncio.wait_for(consumer, timeout=10)

            assert attempts == [1, 2, 3]
            dead = await bus.dead_letters(limit=1)
            assert dead[0]["message_id"] == "transient-exhausted:1"
            assert dead[0]["reason"] == "rejected"

    asyncio.run(scenario())
