"""RabbitMQ adapter tests against a real broker: topology, policy, native delayed retry, DLQ tooling.

Requires a RabbitMQ 4.3 broker at TRACEFOLD_TEST_AMQP_URL (default amqp://tracefold:tracefold@127.0.0.1:5672/,
the compose broker) with the management plugin. The explicit fixture fails closed in evidence mode. Every
test declares its own prefixed topology, applies its own prefixed policies, and deletes both.

Nothing here may assert retry behaviour through an application counter: after #400 the delay, the failure
count and the terminal decision are all the broker's, so the tests read the broker's own numbers.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import socket
import subprocess
import time
import urllib.request
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit

import aio_pika
import pytest
from aio_pika import DeliveryMode, ExchangeType

from tracefold.app.cli.commands.db import _observe_drained_news_broker
from tracefold.integrations.rabbitmq import REMOVED_RETRY_LANE, BrokerPolicyMismatch, RabbitMQBus, topology
from tracefold.news import broker_policy
from tracefold.news.bus import (
    Q_DEAD,
    Q_DELIVER,
    Q_RAW,
    Q_TRIAGE,
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
# Tests that only need "the message came back delayed" use a short delay so a three-attempt sequence
# fits in an integration budget. The production 30 s value is asserted directly from the checked-in
# document, and one test runs the whole frozen contract at its real timing.
FAST_DELAY_MS = 500


@asynccontextmanager
async def _bus(*, delay_ms: int = FAST_DELAY_MS, apply_policies: bool = True) -> AsyncIterator[RabbitMQBus]:
    prefix = f"tf_test_{uuid.uuid4().hex[:8]}"
    bus = RabbitMQBus(
        url=AMQP_URL,
        name_prefix=prefix,
        connect_timeout_seconds=5,
        management_url=MANAGEMENT_URL,
        retry_delay_ms=delay_ms,
    )
    await bus.connect()
    if apply_policies:
        await bus.apply_policies()
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


async def _wait_for_depth(bus: RabbitMQBus, queue: str, expected: int, *, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if (await bus.queue_depths())[bus.queue_name(queue)]["messages"] == expected:
            return
        await asyncio.sleep(0.05)
    assert (await bus.queue_depths())[bus.queue_name(queue)]["messages"] == expected


def _genesis_settings(bus: RabbitMQBus) -> SimpleNamespace:
    return SimpleNamespace(
        news=SimpleNamespace(
            broker=SimpleNamespace(
                url=AMQP_URL,
                name_prefix=bus.prefix,
                connect_timeout_seconds=5,
                management_url=MANAGEMENT_URL,
            )
        )
    )


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


def _broker_version() -> str:
    overview = _management_json("/api/overview")
    assert isinstance(overview, dict)
    return str(overview["rabbitmq_version"])


def test_the_broker_is_the_version_the_retry_contract_was_measured_against() -> None:
    """Native delayed retry does not exist before 4.3; a 4.2 broker would silently retry immediately."""

    major, minor = (int(part) for part in _broker_version().split(".")[:2])
    assert (major, minor) >= (4, 3), f"native delayed retry needs RabbitMQ 4.3+, found {_broker_version()}"


def test_genesis_preflight_observes_all_real_drained_queues() -> None:
    async def scenario() -> None:
        async with _bus(delay_ms=broker_policy.RETRY_DELAY_MS) as bus:
            await bus.declare_topology()
            observation = await _observe_drained_news_broker(_genesis_settings(bus))

            assert set(observation["queues"]) == set(topology(bus.prefix).queue_names)
            assert observation["totals"] == {
                "ready": 0,
                "unacked": 0,
                "dead_letter": 0,
                "stale_reference_count": 0,
            }

    asyncio.run(scenario())


def test_genesis_preflight_rejects_a_real_nonempty_queue() -> None:
    async def scenario() -> None:
        async with _bus(delay_ms=broker_policy.RETRY_DELAY_MS) as bus:
            await bus.declare_topology()
            await bus.publish(_event("genesis:not-drained", {}))
            await _wait_for_depth(bus, Q_TRIAGE, 1)

            with pytest.raises(RuntimeError, match=rf"{bus.queue_name(Q_TRIAGE)}\.messages=0"):
                await _observe_drained_news_broker(_genesis_settings(bus))

    asyncio.run(scenario())


def test_topology_is_three_business_queues_one_dlq_and_no_retry_lane() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            declared = await bus.declare_topology()
            names = {name.removeprefix(f"{bus.prefix}.") for name in declared["queues"]}
            assert names == {Q_RAW, Q_TRIAGE, Q_DELIVER, Q_DEAD}
            assert declared["exchange"].endswith("news")
            assert declared["dlx"].endswith("news.dlx")
            assert "retry" not in json.dumps(declared)
            drift = await bus.topology_drift()
            assert drift == {"queues": [], "exchanges": []}

    asyncio.run(scenario())


def test_a_leftover_retry_lane_is_reported_as_drift_and_never_declared() -> None:
    """The application must not recreate the cut lane, and must not silently delete an undrained one."""

    async def scenario() -> None:
        async with _bus() as bus:
            connection = await aio_pika.connect_robust(AMQP_URL, timeout=5)
            channel = await connection.channel(publisher_confirms=True)
            leftover = bus.queue_name(REMOVED_RETRY_LANE)
            try:
                exchange = await channel.declare_exchange(leftover, ExchangeType.FANOUT, durable=True)
                queue = await channel.declare_queue(leftover, durable=True, arguments={"x-queue-type": "quorum"})
                await queue.bind(exchange, routing_key="#")
                await exchange.publish(
                    aio_pika.Message(b"stranded", delivery_mode=DeliveryMode.PERSISTENT), routing_key="x"
                )

                async def _stranded_count() -> int:
                    # Read the management API rather than a passive declare: aio-pika hands back the
                    # queue object it already declared, whose cached declaration result never moves.
                    vhost = quote(_management_vhost(), safe="")
                    row = _management_json(f"/api/queues/{vhost}/{quote(leftover, safe='')}")
                    assert isinstance(row, dict)
                    return int(row.get("messages") or 0)

                deadline = asyncio.get_running_loop().time() + 15
                while asyncio.get_running_loop().time() < deadline and await _stranded_count() < 1:
                    await asyncio.sleep(0.2)
                assert await _stranded_count() == 1
                await bus.declare_topology()
                drift = await bus.topology_drift()
                assert leftover in drift["queues"]
                assert leftover in drift["exchanges"]
                # Declaring the topology must not have consumed or destroyed the stranded message.
                assert await _stranded_count() == 1
            finally:
                await channel.queue_delete(leftover, if_unused=False, if_empty=False)
                await channel.exchange_delete(leftover)
                await channel.close()
                await connection.close()

    asyncio.run(scenario())


def test_effective_policy_is_the_checked_in_retry_contract() -> None:
    async def scenario() -> None:
        async with _bus(delay_ms=broker_policy.RETRY_DELAY_MS) as bus:
            effective = await bus.effective_policies()
            expected = broker_policy.expected_effective_definitions(name_prefix=bus.prefix)
            assert effective == expected
            for queue in (Q_RAW, Q_TRIAGE, Q_DELIVER):
                definition = effective[bus.queue_name(queue)]
                assert definition["delayed-retry-type"] == "all"
                assert definition["delayed-retry-min"] == definition["delayed-retry-max"] == 30_000
                assert definition["delivery-limit"] == broker_policy.TRANSIENT_DELIVERY_LIMIT
                assert definition["dead-letter-strategy"] == "at-least-once"
                assert definition["overflow"] == "reject-publish"
                assert definition["max-length-bytes"] == broker_policy.MAX_LENGTH_BYTES[queue]
            snapshot = await bus.broker_snapshot()
            assert all(row["policy_ok"] for row in snapshot.values())

    asyncio.run(scenario())


def test_removed_policy_fails_closed_instead_of_falling_back_to_immediate_retry() -> None:
    """A missing policy is not a degraded mode: it is the quorum default limit with no delay at all."""

    async def scenario() -> None:
        async with _bus() as bus:
            await bus.verify_policies()
            vhost = quote(_management_vhost(), safe="")
            name = broker_policy.policies(name_prefix=bus.prefix)[1].name
            _management_delete(f"/api/policies/{vhost}/{quote(name, safe='')}")
            deadline = asyncio.get_running_loop().time() + 20
            while asyncio.get_running_loop().time() < deadline:
                try:
                    await bus.verify_policies()
                except BrokerPolicyMismatch as exc:
                    assert bus.queue_name(Q_TRIAGE) in str(exc)
                    snapshot = await bus.broker_snapshot()
                    assert snapshot[bus.queue_name(Q_TRIAGE)]["policy_ok"] is False
                    return
                await asyncio.sleep(0.3)
            raise AssertionError("removing a policy did not fail verification")

    asyncio.run(scenario())


def test_declarations_confirms_and_publisher_properties_match_the_runtime_contract() -> None:
    async def scenario() -> None:
        async with _bus(delay_ms=broker_policy.RETRY_DELAY_MS) as bus:
            await bus.declare_topology()
            declared = topology(bus.prefix)
            vhost = quote(_management_vhost(), safe="")

            expected_exchanges = {declared.exchange: ("topic", True), declared.dlx: ("fanout", True)}
            for name, expected in expected_exchanges.items():
                exchange = _management_json(f"/api/exchanges/{vhost}/{quote(name, safe='')}")
                assert isinstance(exchange, dict)
                assert (exchange["type"], exchange["durable"]) == expected

            expected_queues = {spec.name: dict(spec.arguments) for spec in declared.queues}
            expected_bindings = {
                spec.name: {(declared.exchange, key) for key in spec.bindings} or {(declared.dlx, "#")}
                for spec in declared.queues
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
                assert incoming.headers["x-news-trace"] == published.trace_id
                # The publisher writes no attempt header: the broker owns the failed-delivery counter.
                assert "x-news-attempt" not in incoming.headers
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


@pytest.mark.slow
def test_transient_failures_are_delayed_counted_and_terminal_after_the_frozen_budget() -> None:
    """The whole frozen contract at its real timing: three attempts, 30 s apart, then `news.dead`.

    This is the test that pins `delivery-limit` to the measured value. A limit one higher would produce a
    fourth attempt; one lower would dead-letter after two. Neither would be visible anywhere else.
    """

    async def scenario() -> None:
        async with _bus(delay_ms=broker_policy.RETRY_DELAY_MS) as bus:
            attempts: list[tuple[int, float]] = []
            stop = asyncio.Event()
            started = time.monotonic()

            async def always_transient(message: BusMessage) -> None:
                attempts.append((message.attempt, time.monotonic() - started))
                raise TransientError("still unavailable")

            consumer = asyncio.create_task(bus.consume(Q_TRIAGE, always_transient, prefetch=1, stop_event=stop))
            await bus.publish(_event("transient-frozen:1", {}))
            await _wait_for_depth(bus, Q_DEAD, 1, timeout=120)
            stop.set()
            await asyncio.wait_for(consumer, timeout=10)

            assert [attempt for attempt, _ in attempts] == [1, 2, 3]
            gaps = [later - earlier for (_, earlier), (_, later) in itertools.pairwise(attempts)]
            assert all(25.0 <= gap <= 45.0 for gap in gaps), gaps
            dead = await bus.dead_letters(limit=1)
            assert dead[0]["message_id"] == "transient-frozen:1"
            assert dead[0]["reason"] == "delivery_limit"
            assert dead[0]["delivery_count"] == broker_policy.TRANSIENT_DELIVERY_LIMIT + 1

    asyncio.run(scenario())


@pytest.mark.slow
def test_a_broker_restart_preserves_a_message_inside_its_delayed_retry_window(restartable_rabbitmq: str) -> None:
    """A delay is queue state, not a timer in some process: restarting the broker must not lose it.

    The message is returned as a counted failure and then sits inside its delayed-retry window with the
    broker restarted underneath it. It has to come back, and it has to come back still carrying the
    failure count it had spent, or the frozen three-attempt budget would silently reset on every bounce.
    """

    async def scenario() -> None:
        async with _bus(delay_ms=20_000) as bus:
            first = asyncio.Event()

            async def reject_once(_message: BusMessage) -> None:
                first.set()
                raise TransientError("return me and hold me")

            stop = asyncio.Event()
            consumer = asyncio.create_task(bus.consume(Q_TRIAGE, reject_once, prefetch=1, stop_event=stop))
            await bus.publish(_event("delayed-survivor:1", {}))
            await asyncio.wait_for(first.wait(), timeout=30)
            stop.set()
            await asyncio.wait_for(consumer, timeout=30)

            await asyncio.to_thread(_restart_broker, restartable_rabbitmq)
            await bus.close()

            seen: list[tuple[str, int]] = []
            resumed = asyncio.Event()

            async def after_restart(message: BusMessage) -> None:
                seen.append((message.message_id, message.attempt))
                resumed.set()

            await asyncio.wait_for(bus.consume(Q_TRIAGE, after_restart, prefetch=1, stop_event=resumed), timeout=240)
            assert seen == [("delayed-survivor:1", 2)]
            assert (await bus.queue_depths())[bus.queue_name(Q_DEAD)]["messages"] == 0

    asyncio.run(scenario())


def _restart_broker(container: str) -> None:
    subprocess.run(
        ["docker", "restart", "-t", "30", container],
        capture_output=True,
        check=True,
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((_AMQP.hostname or "127.0.0.1", _AMQP.port or 5672), timeout=2):
                return
        except OSError:
            time.sleep(2)
    raise AssertionError(f"the broker did not come back after restarting {container}")


def test_repeated_defer_is_delayed_without_spending_the_transient_budget() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            attempts: list[int] = []
            enough = asyncio.Event()
            stop = asyncio.Event()
            deferrals = broker_policy.TOTAL_TRANSIENT_ATTEMPTS + 3

            async def defer_then_accept(message: BusMessage) -> None:
                attempts.append(message.attempt)
                if len(attempts) <= deferrals:
                    raise DeferError("db lane busy")
                enough.set()

            consumer = asyncio.create_task(bus.consume(Q_TRIAGE, defer_then_accept, prefetch=1, stop_event=stop))
            started = time.monotonic()
            await bus.publish(_event("defer:1", {}))
            try:
                await asyncio.wait_for(enough.wait(), timeout=60)
            finally:
                stop.set()
                await asyncio.wait_for(consumer, timeout=10)
            # Every redelivery is attempt 1: an uncounted return leaves the broker's failure counter alone,
            # so a process that cannot admit a message can keep saying so without making it terminal.
            assert attempts == [1] * (deferrals + 1)
            assert (await bus.queue_depths())[bus.queue_name(Q_DEAD)]["messages"] == 0
            # It is still delayed: the deferrals could not have been served back-to-back.
            assert time.monotonic() - started >= deferrals * (FAST_DELAY_MS / 1000) * 0.6

    asyncio.run(scenario())


def test_permanent_failure_and_decode_failure_reach_the_dead_letter_queue() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            stop = asyncio.Event()

            async def reject(_message: BusMessage) -> None:
                raise PermanentError("nope")

            consumer = asyncio.create_task(bus.consume(Q_TRIAGE, reject, prefetch=1, stop_event=stop))
            await bus.publish(_event("permanent:1", {"mode": "permanent"}))
            connection = await aio_pika.connect_robust(AMQP_URL, timeout=5)
            channel = await connection.channel(publisher_confirms=True)
            try:
                exchange = await channel.declare_exchange(
                    topology(bus.prefix).exchange, ExchangeType.TOPIC, durable=True, passive=True
                )
                await exchange.publish(
                    aio_pika.Message(b"{not json", delivery_mode=DeliveryMode.PERSISTENT),
                    routing_key="event.general.normal",
                )
            finally:
                await channel.close()
                await connection.close()
            await _wait_for_depth(bus, Q_DEAD, 2, timeout=30)
            stop.set()
            await asyncio.wait_for(consumer, timeout=10)
            dead = await bus.dead_letters(limit=5)
            assert {row["reason"] for row in dead} == {"rejected"}
            assert "permanent:1" in {row["message_id"] for row in dead}
            # Replay returns the decodable dead letter to the topic exchange and drops the one that can
            # never be decoded, so the DLQ empties and the Event is queued again for a human-fixed run.
            assert await bus.replay_dead_letters(limit=5) == 1
            await _wait_for_depth(bus, Q_DEAD, 0)
            await _wait_for_depth(bus, Q_TRIAGE, 1)

            consumer2 = asyncio.create_task(bus.consume(Q_TRIAGE, reject, prefetch=1, stop_event=stop))
            stop.clear()
            await bus.publish(_event("permanent:2", {}))
            await _wait_for_depth(bus, Q_DEAD, 2, timeout=30)
            stop.set()
            await asyncio.wait_for(consumer2, timeout=10)
            assert await bus.purge_dead_letters() == 2
            await _wait_for_depth(bus, Q_DEAD, 0)

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
            seen: list[int] = []

            async def recover(message: BusMessage) -> None:
                assert message.message_id == "unclassified:1"
                seen.append(message.attempt)
                recovered.set()
                stop.set()

            consumer2 = asyncio.create_task(bus.consume(Q_TRIAGE, recover, prefetch=1, stop_event=stop))
            await asyncio.wait_for(recovered.wait(), timeout=20)
            await asyncio.wait_for(consumer2, timeout=10)
            # A channel that dies with the message unacked is a failed delivery to the broker, so the
            # redelivery carries an incremented counter rather than looking like a first attempt.
            assert seen == [2]
            assert (await bus.queue_depths())[bus.queue_name(Q_DEAD)]["messages"] == 0

    asyncio.run(scenario())


async def _hold_a_dead_letter_against_a_missing_dlq(bus: RabbitMQBus) -> dict[str, dict[str, object]]:
    """Delete the DLQ, make one message terminal, and return the snapshot that saw the held dead letter.

    The snapshot is returned rather than re-read: every field below "does this queue exist" comes from
    one management sample, and asking again would compare numbers from two different samples.
    """

    connection = await aio_pika.connect_robust(AMQP_URL, timeout=5)
    channel = await connection.channel(publisher_confirms=True)
    try:
        await channel.queue_delete(bus.queue_name(Q_DEAD), if_unused=False, if_empty=False)
    finally:
        await channel.close()
        await connection.close()

    stop = asyncio.Event()

    async def reject(_message: BusMessage) -> None:
        raise PermanentError("terminal while the DLQ is gone")

    consumer = asyncio.create_task(bus.consume(Q_TRIAGE, reject, prefetch=1, stop_event=stop))
    await bus.publish(_event("orphan:1", {}))
    snapshot: dict[str, dict[str, object]] = {}
    deadline = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        snapshot = await bus.broker_snapshot()
        if int(snapshot[bus.queue_name(Q_TRIAGE)]["dead_letter_pending"] or 0) >= 1:
            break
        await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(consumer, timeout=10)
    return snapshot


def test_an_unavailable_dead_letter_queue_holds_the_source_message() -> None:
    """at-least-once dead lettering: the source queue keeps the message until the DLQ confirms it."""

    async def scenario() -> None:
        async with _bus() as bus:
            snapshot = await _hold_a_dead_letter_against_a_missing_dlq(bus)
            source = snapshot[bus.queue_name(Q_TRIAGE)]
            # The message was neither delivered onward nor deleted: it is held on the source queue as a
            # pending at-least-once dead letter, which is the signal the status surface alerts on.
            assert source["dead_letter_pending"] == 1
            assert source["messages"] == 1
            # A held dead letter is neither ready for a consumer nor unacked by one.
            assert source["ready"] == 0 and source["unacked"] == 0
            # The deleted dead-letter queue reads as absent, not as an idle queue at depth zero.
            assert snapshot[bus.queue_name(Q_DEAD)]["missing"] is True
            await bus.declare_topology()  # restore the DLQ so teardown deletes a topology that exists

    asyncio.run(scenario())


@pytest.mark.slow
def test_a_recovered_dead_letter_queue_receives_the_held_message() -> None:
    """Recovery is eventual, not instant: RabbitMQ retries a blocked dead letter about every 3 minutes.

    Measured on 4.3.5 at roughly 178 s. The point of the test is that the message is still there to be
    transferred, so the timeout is generous rather than a timing assertion.
    """

    async def scenario() -> None:
        async with _bus() as bus:
            held = await _hold_a_dead_letter_against_a_missing_dlq(bus)
            assert held[bus.queue_name(Q_TRIAGE)]["dead_letter_pending"] == 1
            await bus.declare_topology()
            transferred = False
            deadline = asyncio.get_running_loop().time() + 420
            while asyncio.get_running_loop().time() < deadline:
                try:
                    snapshot = await bus.broker_snapshot()
                except BrokerUnavailable:  # a loaded broker may drop one management read; keep waiting
                    await asyncio.sleep(5)
                    continue
                if snapshot[bus.queue_name(Q_DEAD)]["messages"] >= 1:
                    transferred = True
                    break
                await asyncio.sleep(5)
            assert transferred, "the held dead letter never reached the recovered dead-letter queue"
            assert (await bus.broker_snapshot())[bus.queue_name(Q_TRIAGE)]["dead_letter_pending"] == 0
            dead = await bus.dead_letters(limit=5)
            assert [row["message_id"] for row in dead] == ["orphan:1"]

    asyncio.run(scenario())


def test_a_full_dead_letter_queue_rejects_without_losing_the_source_message() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            vhost = quote(_management_vhost(), safe="")
            name = f"{bus.prefix}-dead-tiny"
            _management_put(
                f"/api/policies/{vhost}/{quote(name, safe='')}",
                {
                    "pattern": f"^{bus.queue_name(Q_DEAD).replace('.', chr(92) + '.')}$",
                    "apply-to": "queues",
                    "priority": broker_policy.POLICY_PRIORITY + 10,
                    "definition": {"max-length-bytes": 1024, "overflow": "reject-publish"},
                },
            )
            try:
                deadline = asyncio.get_running_loop().time() + 20
                while asyncio.get_running_loop().time() < deadline:
                    row = await bus.effective_policies()
                    if row[bus.queue_name(Q_DEAD)].get("max-length-bytes") == 1024:
                        break
                    await asyncio.sleep(0.3)
                stop = asyncio.Event()

                async def reject(_message: BusMessage) -> None:
                    raise PermanentError("terminal against a full DLQ")

                consumer = asyncio.create_task(bus.consume(Q_TRIAGE, reject, prefetch=1, stop_event=stop))
                for index in range(6):
                    await bus.publish(_event(f"full:{index}", {"filler": "x" * 400}))
                await asyncio.sleep(12)
                stop.set()
                await asyncio.wait_for(consumer, timeout=10)
                snapshot = await bus.broker_snapshot()
                dead = snapshot[bus.queue_name(Q_DEAD)]
                source = snapshot[bus.queue_name(Q_TRIAGE)]
                assert dead["message_bytes"] <= 2048
                # Nothing vanished: what the DLQ would not take is still accounted for on the source.
                assert dead["messages"] + source["messages"] + source["dead_letter_pending"] >= 6
                assert source["dead_letter_pending"] >= 1
            finally:
                _management_delete(f"/api/policies/{vhost}/{quote(name, safe='')}")

    asyncio.run(scenario())


def test_a_queue_byte_bound_rejects_the_publisher_instead_of_dropping_the_oldest_message() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            vhost = quote(_management_vhost(), safe="")
            name = f"{bus.prefix}-triage-tiny"
            _management_put(
                f"/api/policies/{vhost}/{quote(name, safe='')}",
                {
                    "pattern": f"^{bus.queue_name(Q_TRIAGE).replace('.', chr(92) + '.')}$",
                    "apply-to": "queues",
                    "priority": broker_policy.POLICY_PRIORITY + 10,
                    "definition": {"max-length-bytes": 4096, "overflow": "reject-publish"},
                },
            )
            try:
                deadline = asyncio.get_running_loop().time() + 20
                while asyncio.get_running_loop().time() < deadline:
                    row = await bus.effective_policies()
                    if row[bus.queue_name(Q_TRIAGE)].get("max-length-bytes") == 4096:
                        break
                    await asyncio.sleep(0.3)
                published = 0
                with pytest.raises(BrokerBackpressure):
                    for index in range(64):
                        await bus.publish(_event(f"bound:{index}", {"filler": "y" * 700}))
                        published += 1
                assert published >= 1
                await _wait_for_depth(bus, Q_TRIAGE, published)
                # The oldest message is still there: reject-publish refuses the newest instead.
                connection = await aio_pika.connect_robust(AMQP_URL, timeout=5)
                channel = await connection.channel()
                try:
                    queue = await channel.declare_queue(bus.queue_name(Q_TRIAGE), passive=True)
                    incoming = await queue.get(no_ack=False, fail=False)
                    assert incoming is not None and incoming.message_id == "bound:0"
                    await incoming.nack(requeue=True)
                finally:
                    await channel.close()
                    await connection.close()
            finally:
                _management_delete(f"/api/policies/{vhost}/{quote(name, safe='')}")

    asyncio.run(scenario())


def test_broker_snapshot_reports_what_amqp_alone_cannot() -> None:
    async def scenario() -> None:
        async with _bus() as bus:
            await bus.publish(_event("snapshot:1", {}))
            await _wait_for_depth(bus, Q_TRIAGE, 1)
            # The management API samples queue statistics on its own interval, so the snapshot the
            # Janitor writes trails the exact AMQP depth by a few seconds.
            snapshot = await bus.broker_snapshot()
            deadline = asyncio.get_running_loop().time() + 45
            while asyncio.get_running_loop().time() < deadline:
                snapshot = await bus.broker_snapshot()
                # Every field below the queue's existence comes from one management sample, which the
                # broker refreshes on its own interval, so wait for that sample rather than for AMQP.
                row = snapshot[bus.queue_name(Q_TRIAGE)]
                if row["ready"] == 1 and (row["message_bytes"] or 0) > 0:
                    break
                await asyncio.sleep(0.5)
            assert set(snapshot) == set(topology(bus.prefix).queue_names)
            triage = snapshot[bus.queue_name(Q_TRIAGE)]
            assert triage["messages"] == 1
            assert triage["ready"] == 1
            assert triage["unacked"] == 0
            assert triage["delayed"] == 0
            assert triage["dead_letter_pending"] == 0
            assert triage["message_bytes"] > 0
            assert triage["max_length_bytes"] == broker_policy.MAX_LENGTH_BYTES[Q_TRIAGE]
            assert 0 < triage["bytes_used_bps"] <= 10_000
            assert triage["policy_ok"] is True
            assert triage["missing"] is False

    asyncio.run(scenario())


def _management_put(path: str, body: dict[str, object]) -> None:
    _management_request("PUT", path, body)


def _management_delete(path: str) -> None:
    _management_request("DELETE", path, None)


def _management_request(method: str, path: str, body: dict[str, object] | None) -> None:
    username = unquote(_AMQP.username or "guest")
    password = unquote(_AMQP.password or "guest")
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 - test-only HTTP management endpoint
        f"{MANAGEMENT_URL}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5):
        return
