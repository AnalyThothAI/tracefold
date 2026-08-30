"""aio-pika adapter: topology declaration, confirmed publish, bounded-concurrency consume loops, DLQ tooling.

This is the only module allowed to import ``aio_pika``. Business consumers depend on
``tracefold.news.bus`` protocols and never touch AMQP objects.

Retry is the broker's (#400). A typed consumer outcome becomes exactly one AMQP settlement, and RabbitMQ
4.3 quorum delayed retry decides when the message comes back and when it becomes terminal. There is no
retry lane, no republish and no application attempt counter; ``tracefold.news.broker_policy`` holds the
one policy document that configures it, and this adapter verifies rather than repairs it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, unquote, urlsplit

import aio_pika
import httpx
from aio_pika import DeliveryMode, ExchangeType
from aio_pika.abc import AbstractChannel, AbstractRobustChannel, AbstractRobustConnection
from aio_pika.exceptions import (
    AMQPError,
    ChannelInvalidStateError,
    ChannelNotFoundEntity,
    DeliveryError,
    PublishError,
)

from tracefold.news import broker_policy
from tracefold.news import bus as news_bus
from tracefold.news.bus import (
    DLX,
    EXCHANGE,
    Q_DEAD,
    Q_DELIVER,
    Q_RAW,
    Q_TRIAGE,
    BusDecodeError,
    BusMessage,
    DeferError,
    Handler,
    PermanentError,
    TransientError,
    decode_body,
)
from tracefold.news.telemetry import NewsDurableEventTelemetryPort, NewsRabbitConsumerFatalReason
from tracefold.platform.docker_host import local_docker_host_amqp_url

log = logging.getLogger("tracefold.news.bus")

# A definitions import is a cluster-wide write and a queue listing is a statistics read; both can take
# seconds on a loaded node. The Janitor bounds its own snapshot separately, so this only has to be
# generous enough that a busy broker does not fail a deploy-time policy apply.
_MANAGEMENT_TIMEOUT_SECONDS: Final = 20.0
_DEFAULT_MANAGEMENT_PORT: Final = 15672
# How long a policy import may take to show up as a queue's effective policy before the apply is called
# a failure. The management API refreshes queue statistics on its own interval.
_POLICY_EFFECTIVE_TIMEOUT_SECONDS: Final = 30.0
_QUEUE_COLUMNS: Final = ",".join(
    (
        "name",
        "consumers",
        "messages",
        "messages_ready",
        "messages_unacknowledged",
        "messages_delayed",
        "messages_dlx",
        "message_bytes",
        "effective_policy_definition",
    )
)


def _q(name_prefix: str, name: str) -> str:
    return f"{name_prefix}.{name}" if name_prefix else name


@dataclass(frozen=True, slots=True)
class QueueSpec:
    name: str
    bindings: tuple[str, ...]
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Topology:
    exchange: str
    dlx: str
    queues: tuple[QueueSpec, ...]

    @property
    def queue_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.queues)

    @property
    def exchange_names(self) -> tuple[str, ...]:
        return (self.exchange, self.dlx)


# The retired Analyst lane (#57). It has been empty for a year and its declaration is force-dropped.
RETIRED_QUEUES: Final = ("news.deep",)
# The self-built TTL retry lane replaced by broker-native delayed retry (#400); one name covered both a
# queue and a fanout exchange. The application never publishes to it, never declares it and never
# deletes it in production: the cutover runbook proves it drained to zero and then deletes it by hand,
# because an automatic delete could destroy a message that never drained.
REMOVED_RETRY_LANE: Final = "news.retry"


def topology(prefix: str = "") -> Topology:
    """One topic exchange, one DLX, three business queues and the dead-letter queue.

    Queue arguments carry only what a policy cannot: the queue type, single-active consumption, and the
    dead-letter queue's evidence-preserving delivery limit. Delayed retry, the delivery limit,
    at-least-once dead lettering, reject-publish overflow and the byte bounds all come from
    `broker_policy`, so a missing policy cannot be masked by a stale queue argument.
    """

    ex = _q(prefix, EXCHANGE)
    dlx = _q(prefix, DLX)
    quorum: Mapping[str, Any] = {"x-queue-type": "quorum"}
    queues = (
        QueueSpec(_q(prefix, Q_RAW), ("raw.#",), {**quorum, "x-single-active-consumer": True}),
        QueueSpec(_q(prefix, Q_TRIAGE), ("event.#",), quorum),
        QueueSpec(_q(prefix, Q_DELIVER), ("verdict.push",), {**quorum, "x-single-active-consumer": True}),
        QueueSpec(
            _q(prefix, Q_DEAD),
            (),
            {**quorum, "x-delivery-limit": broker_policy.DEAD_LETTER_DELIVERY_LIMIT},
        ),
    )
    return Topology(exchange=ex, dlx=dlx, queues=queues)


class BrokerPolicyMismatch(RuntimeError):
    """The broker's effective policy is not the checked-in News retry contract."""


class RabbitMQBus:
    """One robust connection; one publisher channel; per-consumer channels with bounded concurrency."""

    def __init__(
        self,
        *,
        url: str,
        name_prefix: str = "",
        connect_timeout_seconds: float = 10.0,
        management_url: str | None = None,
        retry_delay_ms: int = broker_policy.RETRY_DELAY_MS,
        telemetry: NewsDurableEventTelemetryPort | None = None,
    ) -> None:
        self._url = local_docker_host_amqp_url(url)
        self._prefix = name_prefix
        self._connect_timeout = connect_timeout_seconds
        self._retry_delay_ms = int(retry_delay_ms)
        self._telemetry = telemetry
        self._management = _Management(self._url, management_url)
        self._connection: AbstractRobustConnection | None = None
        self._publish_channel: AbstractRobustChannel | None = None
        self._exchange: aio_pika.abc.AbstractRobustExchange | None = None
        self._lock = asyncio.Lock()

    @property
    def prefix(self) -> str:
        return self._prefix

    def queue_name(self, name: str) -> str:
        return _q(self._prefix, name)

    async def connect(self) -> None:
        async with self._lock:
            if (
                self._connection is not None
                and not self._connection.is_closed
                and self._publish_channel is not None
                and not self._publish_channel.is_closed
                and self._exchange is not None
            ):
                return
            stale_connection = self._connection
            self._connection = None
            self._publish_channel = None
            self._exchange = None
            if stale_connection is not None:
                with contextlib.suppress(Exception):
                    await stale_connection.close()
            connection: AbstractRobustConnection | None = None
            try:
                connection = await aio_pika.connect_robust(self._url, timeout=self._connect_timeout)
                channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
                self._connection = connection
                self._publish_channel = channel
                await self.declare_topology(channel)
            except BaseException as exc:
                self._connection = None
                self._publish_channel = None
                self._exchange = None
                if connection is not None:
                    with contextlib.suppress(Exception):
                        await connection.close()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, (TimeoutError, AMQPError, ChannelInvalidStateError, OSError)):
                    raise news_bus.BrokerUnavailable(f"news_broker_connect_failed:{type(exc).__name__}") from exc
                raise

    async def declare_topology(self, channel: AbstractRobustChannel | None = None) -> dict[str, Any]:
        ch = channel or self._publish_channel
        if ch is None:
            raise news_bus.BrokerUnavailable("news_broker_not_connected")
        spec = topology(self._prefix)
        exchange = await ch.declare_exchange(spec.exchange, ExchangeType.TOPIC, durable=True)
        dlx = await ch.declare_exchange(spec.dlx, ExchangeType.FANOUT, durable=True)
        for queue_spec in spec.queues:
            queue = await ch.declare_queue(queue_spec.name, durable=True, arguments=dict(queue_spec.arguments))
            if queue_spec.name == self.queue_name(Q_DEAD):
                await queue.bind(dlx, routing_key="#")
            for key in queue_spec.bindings:
                await queue.bind(exchange, routing_key=key)
        for retired in RETIRED_QUEUES:
            with contextlib.suppress(Exception):
                await ch.queue_delete(self.queue_name(retired), if_unused=False, if_empty=False)
        self._exchange = exchange
        return {
            "exchange": spec.exchange,
            "dlx": spec.dlx,
            "queues": list(spec.queue_names),
        }

    async def delete_topology(self) -> list[str]:
        """Delete every queue and exchange of this prefix (test teardown / operator reset)."""

        await self.connect()
        channel = await self._channel()
        spec = topology(self._prefix)
        deleted: list[str] = []
        try:
            for name in (*spec.queue_names, self.queue_name(REMOVED_RETRY_LANE)):
                with contextlib.suppress(Exception):
                    await channel.queue_delete(name)
                    deleted.append(name)
            for name in (*spec.exchange_names, self.queue_name(REMOVED_RETRY_LANE)):
                with contextlib.suppress(Exception):
                    await channel.exchange_delete(name)
                    deleted.append(name)
        finally:
            with contextlib.suppress(Exception):
                await channel.close()
        with contextlib.suppress(Exception):
            await self._management.delete_policies(
                [policy.name for policy in broker_policy.policies(name_prefix=self._prefix)]
            )
        self._exchange = None
        return deleted

    async def _channel(self) -> AbstractChannel:
        if self._connection is None:
            raise news_bus.BrokerUnavailable("news_broker_not_connected")
        return await self._connection.channel()

    async def close(self) -> None:
        conn = self._connection
        self._connection = None
        self._publish_channel = None
        self._exchange = None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()
            await asyncio.sleep(0.05)  # let robust-connection callbacks drain before the loop closes

    # ---------------- publish ----------------
    def _amqp_message(self, message: BusMessage) -> aio_pika.Message:
        headers = {**dict(message.headers), "x-news-trace": message.trace_id}
        return aio_pika.Message(
            message.body(),
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
            priority=int(message.priority or 0),
            message_id=message.message_id,
            headers=headers,
        )

    async def publish(self, message: BusMessage) -> None:
        if self._exchange is None:
            await self.connect()
        exchange = self._exchange
        if exchange is None:
            raise news_bus.BrokerUnavailable("news_broker_not_connected")
        await self._publish_confirmed(exchange, message)

    async def _publish_confirmed(self, exchange: aio_pika.abc.AbstractExchange, message: BusMessage) -> None:
        try:
            await exchange.publish(self._amqp_message(message), routing_key=message.routing_key, timeout=10)
        except PublishError as exc:
            raise news_bus.BrokerUnavailable("news_broker_publish_unroutable") from exc
        except DeliveryError as exc:
            raise news_bus.BrokerBackpressure("news_broker_publish_rejected") from exc
        except (TimeoutError, AMQPError, ChannelInvalidStateError, OSError) as exc:
            raise news_bus.BrokerUnavailable(f"news_broker_publish_failed:{type(exc).__name__}") from exc

    # ---------------- consume ----------------
    async def consume(self, queue: str, handler: Handler, *, prefetch: int, stop_event: asyncio.Event) -> None:
        """Run until stop_event; up to ``prefetch`` messages are handled concurrently, each in its own ack envelope."""

        width = max(1, int(prefetch))
        while not stop_event.is_set():
            channel: AbstractChannel | None = None
            try:
                await self.connect()
                channel = await self._channel()
                await channel.set_qos(prefetch_count=width)
                amqp_queue = await channel.declare_queue(self.queue_name(queue), passive=True)
                slots = asyncio.Semaphore(width)
                async with amqp_queue.iterator() as iterator:
                    stop_task = asyncio.create_task(stop_event.wait())
                    try:
                        async with asyncio.TaskGroup() as messages:
                            while not stop_event.is_set():
                                await slots.acquire()
                                if stop_event.is_set():
                                    slots.release()
                                    break
                                next_task = asyncio.create_task(iterator.__anext__())
                                try:
                                    done, _ = await asyncio.wait(
                                        {next_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                                    )
                                    if next_task not in done:
                                        next_task.cancel()
                                        with contextlib.suppress(asyncio.CancelledError):
                                            await next_task
                                        slots.release()
                                        break
                                    incoming = next_task.result()
                                except BaseException:
                                    if not next_task.done():
                                        next_task.cancel()
                                        with contextlib.suppress(asyncio.CancelledError):
                                            await next_task
                                    slots.release()
                                    raise
                                messages.create_task(self._handle_released(queue, incoming, handler, slots))
                    finally:
                        stop_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await stop_task
            except asyncio.CancelledError:
                raise
            except StopAsyncIteration:
                continue
            except (AMQPError, OSError, news_bus.BrokerUnavailable) as exc:
                log.warning("news bus consumer %s reconnecting after %s", queue, type(exc).__name__)
                await _sleep_or_stop(stop_event, 2.0)
            finally:
                if channel is not None and not channel.is_closed:
                    try:
                        await channel.close()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._record_consumer_fatal(queue, "settlement")
                        raise

    async def _handle_released(
        self,
        queue: str,
        incoming: aio_pika.abc.AbstractIncomingMessage,
        handler: Handler,
        slots: asyncio.Semaphore,
    ) -> None:
        try:
            await self._handle(queue, incoming, handler)
        finally:
            slots.release()

    async def _handle(self, queue: str, incoming: aio_pika.abc.AbstractIncomingMessage, handler: Handler) -> None:
        """One delivery, one settlement. The typed domain outcome is the only thing that chooses which.

        `reject(requeue=True)` is the broker's counted failure: it increments `x-delivery-count`, is
        delayed by the queue's delayed-retry policy, and becomes terminal once the delivery limit is
        spent. `nack(requeue=True)` is the uncounted return: delayed the same way, but it never spends
        the budget, so a process that cannot admit the message can say so indefinitely. An unclassified
        exception settles nothing and fails the consumer, leaving the message unacked for the broker to
        return when the channel dies.
        """

        try:
            message = decode_body(
                incoming.body,
                routing_key=str(incoming.routing_key or ""),
                priority=int(incoming.priority or 0),
                headers=dict(incoming.headers or {}),
            )
        except BusDecodeError:
            await self._settle(queue, incoming.reject(requeue=False))
            return
        try:
            await handler(message)
        except DeferError:
            await self._settle(queue, incoming.nack(requeue=True))
            return
        except TransientError:
            await self._settle(queue, incoming.reject(requeue=True))
            return
        except PermanentError as exc:
            log.warning("news bus permanent failure for %s: %s", message.message_id, exc)
            await self._settle(queue, incoming.reject(requeue=False))
            return
        except Exception as exc:
            reason: NewsRabbitConsumerFatalReason = (
                "broker" if isinstance(exc, (news_bus.BrokerBackpressure, news_bus.BrokerUnavailable)) else "handler"
            )
            self._record_consumer_fatal(queue, reason)
            log.error("news bus handler crashed for %s (%s)", message.message_id, type(exc).__name__)
            raise
        await self._settle(queue, incoming.ack())

    async def _settle(self, queue: str, settlement: Awaitable[None]) -> None:
        try:
            await settlement
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_consumer_fatal(queue, "settlement")
            raise

    def _record_consumer_fatal(self, queue: str, reason: NewsRabbitConsumerFatalReason) -> None:
        if self._telemetry is not None:
            self._telemetry.record_news_rabbitmq_consumer_fatal(queue, reason)

    # ---------------- policy ----------------
    async def apply_policies(self) -> dict[str, Any]:
        """Import the checked-in policy document, then read back until the broker reports it.

        Explicit operator/deploy step, never a runtime repair. The import is one declarative write; the
        loop that follows is read-after-write confirmation, because the management API publishes a
        queue's effective policy on its own statistics interval. It never writes twice, and it fails
        rather than returning success the deploy would go on to trust.
        """

        document = broker_policy.definitions_document(
            name_prefix=self._prefix, vhost=self._management.vhost, delay_ms=self._retry_delay_ms
        )
        await self._management.import_definitions(document)
        deadline = asyncio.get_running_loop().time() + _POLICY_EFFECTIVE_TIMEOUT_SECONDS
        while True:
            try:
                await self.verify_policies()
                break
            except BrokerPolicyMismatch:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.5)
        return {"vhost": self._management.vhost, "policies": [entry["name"] for entry in document["policies"]]}

    async def effective_policies(self) -> dict[str, dict[str, Any]]:
        """What RabbitMQ actually applies to each News queue right now."""

        stats = await self._management.queues(topology(self._prefix).queue_names)
        return {name: dict(row.get("effective_policy_definition") or {}) for name, row in stats.items()}

    async def verify_policies(self) -> dict[str, Any]:
        """Fail closed when the broker is not running the checked-in retry contract.

        Verification is not repair: a mismatch is reported and refused, never silently corrected, so a
        removed or edited policy can never degrade into immediate redelivery or at-most-once dead
        lettering without anyone noticing.
        """

        expected = broker_policy.expected_effective_definitions(name_prefix=self._prefix, delay_ms=self._retry_delay_ms)
        actual = await self.effective_policies()
        mismatched = {
            name: {"expected": want, "actual": actual.get(name)}
            for name, want in expected.items()
            if actual.get(name) != want
        }
        if mismatched:
            raise BrokerPolicyMismatch(json.dumps({"news_broker_policy_mismatch": mismatched}, sort_keys=True))
        return {"verified": sorted(expected)}

    async def topology_drift(self) -> dict[str, list[str]]:
        """Names under this prefix that the final topology does not contain.

        The cut retry lane is the reason this exists: after #400 the repository has no code that can
        recreate `news.retry`, so anything still carrying that name is leftover broker state an operator
        has to see and delete, not something the application may quietly remove.
        """

        spec = topology(self._prefix)
        scope = f"{self._prefix}." if self._prefix else ""
        expected_queues = set(spec.queue_names)
        expected_exchanges = set(spec.exchange_names)

        def _mine(name: str) -> bool:
            if self._prefix:
                return name.startswith(scope)
            return name.split(".", 1)[0] == EXCHANGE

        queues = [name for name in await self._management.queue_names() if _mine(name) and name not in expected_queues]
        exchanges = [
            name for name in await self._management.exchange_names() if _mine(name) and name not in expected_exchanges
        ]
        return {"queues": sorted(queues), "exchanges": sorted(exchanges)}

    # ---------------- operator tooling ----------------
    async def broker_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-queue evidence: which queues exist over AMQP, what is in them from the management API.

        Reaching AMQP at all is what proves the broker connection, and its `declare-ok` is the only
        thing that separates "this queue does not exist" from "this queue is empty". What it reports is
        the *ready* count, so it is the fallback rather than the source: the management API's `messages`
        also counts unacked deliveries and at-least-once dead letters the queue is still holding, and a
        held dead letter is exactly the case where the two disagree and the operator needs the larger
        number. Mixing them would also produce a row whose own fields contradict each other.

        The two stay apart on purpose. A management API that is down must not be reported as a
        disconnected broker, and it must not silently report a healthy policy either: the fields it owns
        become `None`, which the status surface reads as unknown rather than as fine.
        """

        depths = await self.queue_depths()
        names = topology(self._prefix).queue_names
        expected = broker_policy.expected_effective_definitions(name_prefix=self._prefix, delay_ms=self._retry_delay_ms)
        try:
            stats = await self._management.queues(names)
        except news_bus.BrokerUnavailable:
            log.warning("news broker management API unavailable; queue policy and retry state are unknown")
            stats = {}
        out: dict[str, dict[str, Any]] = {}
        for name in names:
            if name not in depths:
                # The queue is not there at all. Everything else this row could say would be a guess,
                # and reporting zeros would read as an idle queue rather than a missing one.
                out[name] = {
                    "messages": 0,
                    "consumers": 0,
                    "ready": None,
                    "unacked": None,
                    "delayed": None,
                    "dead_letter_pending": None,
                    "message_bytes": None,
                    "max_length_bytes": None,
                    "bytes_used_bps": None,
                    "policy_ok": None,
                    "missing": True,
                }
                continue
            row = stats.get(name)
            if row is None:
                counts = depths[name]
                out[name] = {
                    "messages": int(counts.get("messages") or 0),
                    "consumers": int(counts.get("consumers") or 0),
                    "ready": None,
                    "unacked": None,
                    "delayed": None,
                    "dead_letter_pending": None,
                    "message_bytes": None,
                    "max_length_bytes": None,
                    "bytes_used_bps": None,
                    "policy_ok": None,
                    "missing": False,
                }
                continue
            effective = dict(row.get("effective_policy_definition") or {})
            limit = int(effective.get("max-length-bytes") or 0)
            used = int(row.get("message_bytes") or 0)
            out[name] = {
                "messages": int(row.get("messages") or 0),
                "consumers": int(row.get("consumers") or 0),
                "ready": int(row.get("messages_ready") or 0),
                "unacked": int(row.get("messages_unacknowledged") or 0),
                "delayed": int(row.get("messages_delayed") or 0),
                "dead_letter_pending": int(row.get("messages_dlx") or 0),
                "message_bytes": used,
                "max_length_bytes": limit or None,
                "bytes_used_bps": min(10_000, round(used * 10_000 / limit)) if limit else None,
                "policy_ok": effective == expected.get(name),
                "missing": False,
            }
        return out

    async def queue_depths(self) -> dict[str, dict[str, int]]:
        """Passive declare each queue and report message/consumer counts (cheap, no mgmt API).

        A queue that does not exist is omitted rather than raised. Passive declaration of a missing
        queue closes the channel, so one channel for the whole topology would turn "the dead-letter
        queue is gone" into "the broker is unreachable" — which is a different incident with a different
        fix. Each queue therefore gets its own channel, and the caller reads absence as absence.
        """

        await self.connect()
        out: dict[str, dict[str, int]] = {}
        for name in topology(self._prefix).queue_names:
            channel = await self._channel()
            try:
                declared = await channel.declare_queue(name, passive=True)
                out[name] = {
                    "messages": int(declared.declaration_result.message_count or 0),
                    "consumers": int(declared.declaration_result.consumer_count or 0),
                }
            except ChannelNotFoundEntity:
                log.warning("news bus queue %s does not exist", name)
            finally:
                with contextlib.suppress(Exception):
                    await channel.close()
        return out

    async def dead_letters(self, *, limit: int) -> list[dict[str, Any]]:
        """Peek up to ``limit`` dead-lettered messages without consuming them."""

        await self.connect()
        channel = await self._channel()
        out: list[dict[str, Any]] = []
        held: list[aio_pika.abc.AbstractIncomingMessage] = []
        try:
            queue = await channel.declare_queue(self.queue_name(Q_DEAD), passive=True)
            for _ in range(max(0, int(limit))):
                incoming = await queue.get(no_ack=False, fail=False)
                if incoming is None:
                    break
                held.append(incoming)
                out.append(_dead_letter_view(incoming))
            for incoming in held:  # requeue only after the whole page was read, so peeks are distinct
                await incoming.nack(requeue=True)
        finally:
            with contextlib.suppress(Exception):
                await channel.close()
        return out

    async def replay_dead_letters(self, *, limit: int) -> int:
        """Republish up to ``limit`` dead letters to the topic exchange with a fresh delivery counter."""

        await self.connect()
        channel = await self._channel()
        replayed = 0
        try:
            queue = await channel.declare_queue(self.queue_name(Q_DEAD), passive=True)
            for _ in range(max(0, int(limit))):
                incoming = await queue.get(no_ack=False, fail=False)
                if incoming is None:
                    break
                try:
                    message = decode_body(
                        incoming.body,
                        routing_key=str(incoming.routing_key or ""),
                        priority=int(incoming.priority or 0),
                        headers={},
                    )
                except BusDecodeError:
                    await incoming.reject(requeue=False)
                    continue
                await self.publish(message)
                await incoming.ack()
                replayed += 1
        finally:
            with contextlib.suppress(Exception):
                await channel.close()
        return replayed

    async def purge_dead_letters(self) -> int:
        await self.connect()
        channel = await self._channel()
        try:
            queue = await channel.declare_queue(self.queue_name(Q_DEAD), passive=True)
            result = await queue.purge()
            return int(getattr(result, "message_count", 0) or 0)
        finally:
            with contextlib.suppress(Exception):
                await channel.close()


class _Management:
    """The RabbitMQ management HTTP API, addressed from the same credentials as the AMQP URL."""

    def __init__(self, amqp_url: str, management_url: str | None) -> None:
        parsed = urlsplit(amqp_url)
        self._username = unquote(parsed.username or "guest")
        self._password = unquote(parsed.password or "guest")
        host = parsed.hostname or "127.0.0.1"
        explicit = (management_url or "").strip().rstrip("/")
        self._base = explicit or f"http://{host}:{_DEFAULT_MANAGEMENT_PORT}"
        self.vhost = unquote(parsed.path.lstrip("/")) or "/"

    @property
    def base_url(self) -> str:
        return self._base

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self._username}:{self._password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, body: Any | None = None) -> Any:
        url = f"{self._base}{path}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(_MANAGEMENT_TIMEOUT_SECONDS)) as client:
                response = await client.request(method, url, headers=self._headers(), json=body)
        except httpx.HTTPError as exc:
            raise news_bus.BrokerUnavailable(f"news_broker_management_failed:{type(exc).__name__}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise news_bus.BrokerUnavailable(f"news_broker_management_status:{response.status_code}")
        if not response.content:
            return None
        return response.json()

    async def queues(self, names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        """One request for the whole topology, narrowed to the columns the snapshot reads.

        Asking per queue would multiply the request count by the topology size on every Janitor tick and
        on every poll an operator makes, for no extra information.
        """

        wanted = set(names)
        rows = await self._request("GET", f"/api/queues/{quote(self.vhost, safe='')}?columns={_QUEUE_COLUMNS}")
        return {str(row["name"]): row for row in rows or [] if isinstance(row, dict) and row.get("name") in wanted}

    async def import_definitions(self, document: Mapping[str, Any]) -> None:
        await self._request("POST", "/api/definitions", dict(document))

    async def queue_names(self) -> tuple[str, ...]:
        rows = await self._request("GET", f"/api/queues/{quote(self.vhost, safe='')}")
        return tuple(str(row.get("name")) for row in rows or [] if isinstance(row, dict))

    async def exchange_names(self) -> tuple[str, ...]:
        rows = await self._request("GET", f"/api/exchanges/{quote(self.vhost, safe='')}")
        return tuple(str(row.get("name")) for row in rows or [] if isinstance(row, dict))

    async def delete_policies(self, names: list[str]) -> None:
        vhost = quote(self.vhost, safe="")
        for name in names:
            await self._request("DELETE", f"/api/policies/{vhost}/{quote(name, safe='')}")


def _dead_letter_view(incoming: aio_pika.abc.AbstractIncomingMessage) -> dict[str, Any]:
    headers = dict(incoming.headers or {})
    death = headers.get("x-death") or []
    first = death[0] if isinstance(death, list) and death and isinstance(death[0], dict) else {}
    return {
        "message_id": incoming.message_id,
        "routing_key": incoming.routing_key,
        "delivery_count": headers.get(news_bus.DELIVERY_COUNT_HEADER),
        "acquired_count": headers.get(news_bus.ACQUIRED_COUNT_HEADER),
        "trace_id": headers.get("x-news-trace"),
        "reason": first.get("reason"),
        "source_queue": first.get("queue"),
        "body": incoming.body[:600].decode("utf-8", errors="replace"),
    }


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)


__all__ = ["BrokerPolicyMismatch", "QueueSpec", "RabbitMQBus", "Topology", "topology"]
