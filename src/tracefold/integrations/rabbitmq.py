"""aio-pika adapter: topology declaration, confirmed publish, bounded-concurrency consume loops, DLQ tooling.

This is the only module allowed to import ``aio_pika``. Business consumers depend on
``tracefold.news.bus`` protocols and never touch AMQP objects.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import aio_pika
from aio_pika import DeliveryMode, ExchangeType
from aio_pika.abc import AbstractChannel, AbstractRobustChannel, AbstractRobustConnection
from aio_pika.exceptions import AMQPError, DeliveryError

from tracefold.news.bus import (
    DLX,
    EXCHANGE,
    MAX_TRANSIENT_ATTEMPTS,
    Q_DEAD,
    Q_DELIVER,
    Q_RAW,
    Q_RETRY,
    Q_TRIAGE,
    RETRY_EXCHANGE,
    RETRY_TTL_MS,
    BusDecodeError,
    BusMessage,
    DeferError,
    Handler,
    PermanentError,
    TransientError,
    decode_body,
)
from tracefold.platform.docker_host import local_docker_host_amqp_url

log = logging.getLogger("tracefold.news.bus")

RAW_MAX_LENGTH = 100_000
DEAD_LETTER_DELIVERY_LIMIT = 1_000_000


class BrokerBackpressure(RuntimeError):
    """Publish was rejected by the broker (queue overflow with reject-publish)."""


class BrokerUnavailable(RuntimeError):
    pass


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
    retry_exchange: str
    retry_queue: str
    queues: tuple[QueueSpec, ...]

    @property
    def queue_names(self) -> tuple[str, ...]:
        return (*(spec.name for spec in self.queues), self.retry_queue)

    @property
    def exchange_names(self) -> tuple[str, ...]:
        return (self.exchange, self.dlx, self.retry_exchange)


RETIRED_QUEUES: Final = ("news.deep",)


def topology(prefix: str = "") -> Topology:
    """One topic exchange, one DLX, one 30 s retry lane, three business queues and the dead-letter queue."""

    ex = _q(prefix, EXCHANGE)
    dlx = _q(prefix, DLX)
    base = {"x-queue-type": "quorum", "x-dead-letter-exchange": dlx}
    queues = (
        QueueSpec(
            _q(prefix, Q_RAW),
            ("raw.#",),
            {
                **base,
                "x-single-active-consumer": True,
                "x-max-length": RAW_MAX_LENGTH,
                "x-overflow": "reject-publish",
                "x-delivery-limit": 3,
            },
        ),
        QueueSpec(_q(prefix, Q_TRIAGE), ("event.#",), {**base, "x-delivery-limit": 3}),
        QueueSpec(
            _q(prefix, Q_DELIVER),
            ("verdict.push",),
            {**base, "x-single-active-consumer": True, "x-delivery-limit": 1},
        ),
        # Peeking the DLQ requeues; lift the quorum default delivery limit (20) so peeks never drop evidence.
        QueueSpec(_q(prefix, Q_DEAD), (), {"x-queue-type": "quorum", "x-delivery-limit": DEAD_LETTER_DELIVERY_LIMIT}),
    )
    return Topology(
        exchange=ex,
        dlx=dlx,
        retry_exchange=_q(prefix, RETRY_EXCHANGE),
        retry_queue=_q(prefix, Q_RETRY),
        queues=queues,
    )


class RabbitMQBus:
    """One robust connection; one publisher channel; per-consumer channels with bounded concurrency."""

    def __init__(
        self,
        *,
        url: str,
        name_prefix: str = "",
        connect_timeout_seconds: float = 10.0,
        retry_ttl_ms: int = RETRY_TTL_MS,
    ) -> None:
        self._url = local_docker_host_amqp_url(url)
        self._prefix = name_prefix
        self._connect_timeout = connect_timeout_seconds
        self._retry_ttl_ms = int(retry_ttl_ms)
        self._connection: AbstractRobustConnection | None = None
        self._publish_channel: AbstractRobustChannel | None = None
        self._exchange: aio_pika.abc.AbstractRobustExchange | None = None
        self._retry_exchange: aio_pika.abc.AbstractRobustExchange | None = None
        self._lock = asyncio.Lock()

    @property
    def prefix(self) -> str:
        return self._prefix

    def queue_name(self, name: str) -> str:
        return _q(self._prefix, name)

    async def connect(self) -> None:
        async with self._lock:
            if self._connection is not None and not self._connection.is_closed:
                return
            try:
                self._connection = await aio_pika.connect_robust(self._url, timeout=self._connect_timeout)
            except (TimeoutError, AMQPError, OSError) as exc:
                raise BrokerUnavailable(f"news_broker_connect_failed:{type(exc).__name__}") from exc
            self._publish_channel = await self._connection.channel(publisher_confirms=True)
            await self.declare_topology(self._publish_channel)

    async def declare_topology(self, channel: AbstractRobustChannel | None = None) -> dict[str, Any]:
        ch = channel or self._publish_channel
        if ch is None:
            raise BrokerUnavailable("news_broker_not_connected")
        spec = topology(self._prefix)
        exchange = await ch.declare_exchange(spec.exchange, ExchangeType.TOPIC, durable=True)
        dlx = await ch.declare_exchange(spec.dlx, ExchangeType.FANOUT, durable=True)
        for queue_spec in spec.queues:
            queue = await ch.declare_queue(queue_spec.name, durable=True, arguments=dict(queue_spec.arguments))
            if queue_spec.name == self.queue_name(Q_DEAD):
                await queue.bind(dlx, routing_key="#")
            for key in queue_spec.bindings:
                await queue.bind(exchange, routing_key=key)
        retry_ex = await ch.declare_exchange(spec.retry_exchange, ExchangeType.FANOUT, durable=True)
        retry_queue = await ch.declare_queue(
            spec.retry_queue,
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-message-ttl": self._retry_ttl_ms,
                "x-dead-letter-exchange": spec.exchange,
            },
        )
        await retry_queue.bind(retry_ex, routing_key="#")
        for retired in RETIRED_QUEUES:
            # The Analyst lane (news.deep) was retired in #57; drop its queue if an older deployment declared it.
            with contextlib.suppress(Exception):
                await ch.queue_delete(self.queue_name(retired), if_unused=False, if_empty=False)
        self._exchange = exchange
        self._retry_exchange = retry_ex
        return {
            "exchange": spec.exchange,
            "dlx": spec.dlx,
            "retry_exchange": spec.retry_exchange,
            "queues": list(spec.queue_names),
        }

    async def delete_topology(self) -> list[str]:
        """Delete every queue and exchange of this prefix (test teardown / operator reset)."""

        await self.connect()
        channel = await self._channel()
        spec = topology(self._prefix)
        deleted: list[str] = []
        try:
            for name in spec.queue_names:
                with contextlib.suppress(Exception):
                    await channel.queue_delete(name)
                    deleted.append(name)
            for name in spec.exchange_names:
                with contextlib.suppress(Exception):
                    await channel.exchange_delete(name)
                    deleted.append(name)
        finally:
            with contextlib.suppress(Exception):
                await channel.close()
        self._exchange = None
        self._retry_exchange = None
        return deleted

    async def _channel(self) -> AbstractChannel:
        if self._connection is None:
            raise BrokerUnavailable("news_broker_not_connected")
        return await self._connection.channel()

    async def close(self) -> None:
        conn = self._connection
        self._connection = None
        self._publish_channel = None
        self._exchange = None
        self._retry_exchange = None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()
            await asyncio.sleep(0.05)  # let robust-connection callbacks drain before the loop closes

    # ---------------- publish ----------------
    def _amqp_message(self, message: BusMessage) -> aio_pika.Message:
        headers = {**dict(message.headers), "x-news-attempt": message.attempt, "x-news-trace": message.trace_id}
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
        if self._exchange is None:
            raise BrokerUnavailable("news_broker_not_connected")
        try:
            await self._exchange.publish(self._amqp_message(message), routing_key=message.routing_key, timeout=10)
        except DeliveryError as exc:
            raise BrokerBackpressure("news_broker_publish_rejected") from exc
        except (TimeoutError, AMQPError, OSError) as exc:
            raise BrokerUnavailable(f"news_broker_publish_failed:{type(exc).__name__}") from exc

    async def _publish_retry_lane(self, message: BusMessage, *, attempt: int) -> None:
        if self._retry_exchange is None:
            await self.connect()
        if self._retry_exchange is None:
            raise BrokerUnavailable("news_broker_not_connected")
        retried = BusMessage(
            kind=message.kind,
            message_id=message.message_id,
            routing_key=message.routing_key,
            payload=message.payload,
            trace_id=message.trace_id,
            occurred_at_ms=message.occurred_at_ms,
            priority=message.priority,
            attempt=attempt,
            headers=message.headers,
        )
        await self._retry_exchange.publish(self._amqp_message(retried), routing_key=message.routing_key, timeout=10)

    async def publish_retry(self, message: BusMessage) -> bool:
        """Counted retry through the TTL lane; returns False when attempts are exhausted."""

        if message.attempt >= MAX_TRANSIENT_ATTEMPTS:
            return False
        await self._publish_retry_lane(message, attempt=message.attempt + 1)
        return True

    async def publish_defer(self, message: BusMessage) -> None:
        """Uncounted requeue through the TTL lane (process could not admit the message)."""

        await self._publish_retry_lane(message, attempt=message.attempt)

    # ---------------- consume ----------------
    async def consume(self, queue: str, handler: Handler, *, prefetch: int, stop_event: asyncio.Event) -> None:
        """Run until stop_event; up to ``prefetch`` messages are handled concurrently, each in its own ack envelope."""

        width = max(1, int(prefetch))
        while not stop_event.is_set():
            inflight: set[asyncio.Task[None]] = set()
            try:
                await self.connect()
                channel = await self._channel()
                await channel.set_qos(prefetch_count=width)
                amqp_queue = await channel.declare_queue(self.queue_name(queue), passive=True)
                slots = asyncio.Semaphore(width)
                async with amqp_queue.iterator() as iterator:
                    stop_task = asyncio.create_task(stop_event.wait())
                    try:
                        while not stop_event.is_set():
                            await slots.acquire()
                            next_task = asyncio.create_task(iterator.__anext__())
                            done, _ = await asyncio.wait({next_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                            if next_task not in done:
                                slots.release()
                                next_task.cancel()
                                with contextlib.suppress(BaseException):
                                    await next_task
                                break
                            incoming = next_task.result()
                            task = asyncio.create_task(self._handle_released(incoming, handler, slots))
                            inflight.add(task)
                            task.add_done_callback(inflight.discard)
                    finally:
                        stop_task.cancel()
                        with contextlib.suppress(BaseException):
                            await stop_task
                        if inflight:
                            await asyncio.gather(*inflight, return_exceptions=True)
                with contextlib.suppress(Exception):
                    await channel.close()
            except asyncio.CancelledError:
                raise
            except StopAsyncIteration:
                continue
            except (AMQPError, OSError, BrokerUnavailable) as exc:
                log.warning("news bus consumer %s reconnecting after %s", queue, type(exc).__name__)
                if inflight:
                    await asyncio.gather(*inflight, return_exceptions=True)
                await _sleep_or_stop(stop_event, 2.0)

    async def _handle_released(
        self, incoming: aio_pika.abc.AbstractIncomingMessage, handler: Handler, slots: asyncio.Semaphore
    ) -> None:
        try:
            await self._handle(incoming, handler)
        finally:
            slots.release()

    async def _handle(self, incoming: aio_pika.abc.AbstractIncomingMessage, handler: Handler) -> None:
        try:
            message = decode_body(
                incoming.body,
                routing_key=str(incoming.routing_key or ""),
                priority=int(incoming.priority or 0),
                headers=dict(incoming.headers or {}),
            )
        except BusDecodeError:
            await incoming.reject(requeue=False)
            return
        try:
            await handler(message)
        except DeferError as exc:
            deferred = False
            with contextlib.suppress(Exception):
                await self.publish_defer(message)
                deferred = True
            if deferred:
                await incoming.ack()
            else:
                log.warning("news bus defer failed for %s: %s", message.message_id, exc)
                await incoming.reject(requeue=False)
            return
        except TransientError as exc:
            retried = False
            with contextlib.suppress(Exception):
                retried = await self.publish_retry(message)
            if retried:
                await incoming.ack()
            else:
                log.warning("news bus transient attempts exhausted for %s: %s", message.message_id, exc)
                await incoming.reject(requeue=False)
            return
        except PermanentError as exc:
            log.warning("news bus permanent failure for %s: %s", message.message_id, exc)
            await incoming.reject(requeue=False)
            return
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await incoming.nack(requeue=True)
            raise
        except Exception:
            log.exception("news bus handler crashed for %s", message.message_id)
            await incoming.reject(requeue=False)
            return
        await incoming.ack()

    # ---------------- operator tooling ----------------
    async def queue_depths(self) -> dict[str, dict[str, int]]:
        """Passive declare each queue and report message/consumer counts (cheap, no mgmt API)."""

        await self.connect()
        channel = await self._channel()
        out: dict[str, dict[str, int]] = {}
        try:
            for name in topology(self._prefix).queue_names:
                declared = await channel.declare_queue(name, passive=True)
                out[name] = {
                    "messages": int(declared.declaration_result.message_count or 0),
                    "consumers": int(declared.declaration_result.consumer_count or 0),
                }
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
        """Republish up to ``limit`` dead letters to the topic exchange with a fresh attempt counter."""

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


def _dead_letter_view(incoming: aio_pika.abc.AbstractIncomingMessage) -> dict[str, Any]:
    headers = dict(incoming.headers or {})
    death = headers.get("x-death") or []
    first = death[0] if isinstance(death, list) and death and isinstance(death[0], dict) else {}
    return {
        "message_id": incoming.message_id,
        "routing_key": incoming.routing_key,
        "attempt": headers.get("x-news-attempt"),
        "trace_id": headers.get("x-news-trace"),
        "reason": first.get("reason"),
        "source_queue": first.get("queue"),
        "body": incoming.body[:600].decode("utf-8", errors="replace"),
    }


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)


__all__ = ["BrokerBackpressure", "BrokerUnavailable", "QueueSpec", "RabbitMQBus", "Topology", "topology"]
