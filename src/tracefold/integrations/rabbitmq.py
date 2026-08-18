"""aio-pika adapter: topology declaration, confirmed publish, prefetch/ack consume loops.

This is the only module allowed to import ``aio_pika``. Business consumers depend on
``tracefold.news.bus`` protocols and never touch AMQP objects.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType
from aio_pika.abc import AbstractChannel, AbstractRobustChannel, AbstractRobustConnection
from aio_pika.exceptions import AMQPError, DeliveryError

from tracefold.news.bus import (
    CONTROL_EXCHANGE,
    DLX,
    EXCHANGE,
    MAX_TRANSIENT_ATTEMPTS,
    Q_DEAD,
    Q_DEEP,
    Q_DELIVER,
    Q_RAW,
    Q_TRANSLATE,
    Q_TRIAGE,
    RETRY_TTL_MS,
    BusDecodeError,
    BusMessage,
    Handler,
    PermanentError,
    TransientError,
    decode_body,
)

log = logging.getLogger("tracefold.news.bus")

RAW_MAX_LENGTH = 100_000


class BrokerBackpressure(RuntimeError):
    """Publish was rejected by the broker (queue overflow with reject-publish)."""


class BrokerUnavailable(RuntimeError):
    pass


def _q(name_prefix: str, name: str) -> str:
    return f"{name_prefix}.{name}" if name_prefix else name


def retry_exchange_name(prefix: str, ttl_ms: int) -> str:
    return _q(prefix, f"news.retry.{ttl_ms // 1000}s")


@dataclass(frozen=True, slots=True)
class QueueSpec:
    name: str
    bindings: tuple[str, ...]
    arguments: Mapping[str, Any]


def topology(prefix: str = "") -> tuple[str, str, str, tuple[QueueSpec, ...], tuple[tuple[str, str, int], ...]]:
    """Return (exchange, dlx, control_exchange, queues, retry_lanes[(exchange, queue, ttl_ms)])."""

    ex = _q(prefix, EXCHANGE)
    dlx = _q(prefix, DLX)
    ctl = _q(prefix, CONTROL_EXCHANGE)
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
        QueueSpec(_q(prefix, Q_TRANSLATE), ("verdict.push",), {**base, "x-delivery-limit": 3}),
        QueueSpec(_q(prefix, Q_DEEP), ("verdict.escalate",), {**base, "x-delivery-limit": 2}),
        QueueSpec(
            _q(prefix, Q_DELIVER),
            ("verdict.push", "verdict.deep"),
            {**base, "x-single-active-consumer": True, "x-delivery-limit": 1},
        ),
        QueueSpec(_q(prefix, Q_DEAD), (), {"x-queue-type": "quorum"}),
    )
    retry_lanes = tuple(
        (retry_exchange_name(prefix, ttl), _q(prefix, f"news.retry.{ttl // 1000}s"), ttl) for ttl in RETRY_TTL_MS
    )
    return ex, dlx, ctl, queues, retry_lanes


class RabbitMQBus:
    """One robust connection; one publisher channel; per-consumer channels with prefetch."""

    def __init__(self, *, url: str, name_prefix: str = "", connect_timeout_seconds: float = 10.0) -> None:
        self._url = url
        self._prefix = name_prefix
        self._connect_timeout = connect_timeout_seconds
        self._connection: AbstractRobustConnection | None = None
        self._publish_channel: AbstractRobustChannel | None = None
        self._exchange: aio_pika.abc.AbstractRobustExchange | None = None
        self._control_exchange: aio_pika.abc.AbstractRobustExchange | None = None
        self._retry_exchanges: dict[int, aio_pika.abc.AbstractRobustExchange] = {}
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
        ex_name, dlx_name, ctl_name, queues, retry_lanes = topology(self._prefix)
        exchange = await ch.declare_exchange(ex_name, ExchangeType.TOPIC, durable=True)
        dlx = await ch.declare_exchange(dlx_name, ExchangeType.FANOUT, durable=True)
        control = await ch.declare_exchange(ctl_name, ExchangeType.FANOUT, durable=True)
        declared: dict[str, Any] = {"exchange": ex_name, "dlx": dlx_name, "control": ctl_name, "queues": []}
        for spec in queues:
            queue = await ch.declare_queue(spec.name, durable=True, arguments=dict(spec.arguments))
            if spec.name == self.queue_name(Q_DEAD):
                await queue.bind(dlx, routing_key="#")
            for key in spec.bindings:
                await queue.bind(exchange, routing_key=key)
            declared["queues"].append(spec.name)
        for retry_ex_name, retry_queue_name, ttl in retry_lanes:
            retry_ex = await ch.declare_exchange(retry_ex_name, ExchangeType.FANOUT, durable=True)
            retry_queue = await ch.declare_queue(
                retry_queue_name,
                durable=True,
                arguments={"x-queue-type": "quorum", "x-message-ttl": ttl, "x-dead-letter-exchange": ex_name},
            )
            await retry_queue.bind(retry_ex, routing_key="#")
            self._retry_exchanges[ttl] = retry_ex
            declared["queues"].append(retry_queue_name)
        self._exchange = exchange
        self._control_exchange = control
        return declared

    async def _channel(self) -> AbstractChannel:
        if self._connection is None:
            raise BrokerUnavailable("news_broker_not_connected")
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
    def _amqp_message(self, message: BusMessage, *, extra_headers: Mapping[str, Any] | None = None) -> aio_pika.Message:
        headers = {**dict(message.headers), "x-news-attempt": message.attempt, "x-news-trace": message.trace_id}
        if extra_headers:
            headers.update(extra_headers)
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

    async def publish_control(self, message: BusMessage) -> None:
        if self._control_exchange is None:
            await self.connect()
        if self._control_exchange is None:
            raise BrokerUnavailable("news_broker_not_connected")
        await self._control_exchange.publish(self._amqp_message(message), routing_key="", timeout=10)

    async def publish_retry(self, message: BusMessage) -> bool:
        """Re-publish into a TTL lane; returns False when attempts are exhausted."""

        if message.attempt >= MAX_TRANSIENT_ATTEMPTS:
            return False
        ttl = RETRY_TTL_MS[min(message.attempt - 1, len(RETRY_TTL_MS) - 1)]
        retry_ex = self._retry_exchanges.get(ttl)
        if retry_ex is None:
            await self.connect()
            retry_ex = self._retry_exchanges[ttl]
        retried = BusMessage(
            kind=message.kind,
            message_id=message.message_id,
            routing_key=message.routing_key,
            payload=message.payload,
            trace_id=message.trace_id,
            occurred_at_ms=message.occurred_at_ms,
            priority=message.priority,
            attempt=message.attempt + 1,
            headers=message.headers,
        )
        await retry_ex.publish(self._amqp_message(retried), routing_key=message.routing_key, timeout=10)
        return True

    # ---------------- consume ----------------
    async def consume(self, queue: str, handler: Handler, *, prefetch: int, stop_event: asyncio.Event) -> None:
        """Run until stop_event; each message is handled inside its own ack/nack envelope."""

        while not stop_event.is_set():
            try:
                await self.connect()
                channel = await self._channel()
                await channel.set_qos(prefetch_count=max(1, int(prefetch)))
                amqp_queue = await channel.declare_queue(self.queue_name(queue), passive=True)
                async with amqp_queue.iterator() as iterator:
                    stop_task = asyncio.create_task(stop_event.wait())
                    try:
                        while not stop_event.is_set():
                            next_task = asyncio.create_task(iterator.__anext__())
                            done, _ = await asyncio.wait({next_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                            if next_task not in done:
                                next_task.cancel()
                                with contextlib.suppress(BaseException):
                                    await next_task
                                break
                            incoming = next_task.result()
                            await self._handle(incoming, handler)
                    finally:
                        stop_task.cancel()
                        with contextlib.suppress(BaseException):
                            await stop_task
                with contextlib.suppress(Exception):
                    await channel.close()
            except asyncio.CancelledError:
                raise
            except StopAsyncIteration:
                continue
            except (AMQPError, OSError, BrokerUnavailable) as exc:
                log.warning("news bus consumer %s reconnecting after %s", queue, type(exc).__name__)
                await _sleep_or_stop(stop_event, 2.0)

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

    async def consume_control(self, handler: Handler, *, stop_event: asyncio.Event) -> None:
        """Consume the fanout control exchange through a private, auto-delete queue."""

        while not stop_event.is_set():
            try:
                await self.connect()
                channel = await self._channel()
                await channel.set_qos(prefetch_count=1)
                exchange = await channel.declare_exchange(
                    _q(self._prefix, CONTROL_EXCHANGE), ExchangeType.FANOUT, durable=True
                )
                queue = await channel.declare_queue("", exclusive=True, auto_delete=True)
                await queue.bind(exchange, routing_key="#")
                async with queue.iterator() as iterator:
                    stop_task = asyncio.create_task(stop_event.wait())
                    try:
                        while not stop_event.is_set():
                            next_task = asyncio.create_task(iterator.__anext__())
                            done, _ = await asyncio.wait({next_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                            if next_task not in done:
                                next_task.cancel()
                                with contextlib.suppress(BaseException):
                                    await next_task
                                break
                            await self._handle(next_task.result(), handler)
                    finally:
                        stop_task.cancel()
                        with contextlib.suppress(BaseException):
                            await stop_task
                with contextlib.suppress(Exception):
                    await channel.close()
            except asyncio.CancelledError:
                raise
            except (AMQPError, OSError, BrokerUnavailable, StopAsyncIteration) as exc:
                log.warning("news control consumer reconnecting after %s", type(exc).__name__)
                await _sleep_or_stop(stop_event, 2.0)

    async def queue_depths(self) -> dict[str, dict[str, int]]:
        """Passive declare each queue and report message/consumer counts (cheap, no mgmt API)."""

        await self.connect()
        channel = await self._channel()
        out: dict[str, dict[str, int]] = {}
        try:
            _, _, _, queues, retry_lanes = topology(self._prefix)
            names = [spec.name for spec in queues] + [name for _, name, _ in retry_lanes]
            for name in names:
                declared = await channel.declare_queue(name, passive=True)
                out[name] = {
                    "messages": int(declared.declaration_result.message_count or 0),
                    "consumers": int(declared.declaration_result.consumer_count or 0),
                }
        finally:
            with contextlib.suppress(Exception):
                await channel.close()
        return out


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)


__all__ = ["BrokerBackpressure", "BrokerUnavailable", "QueueSpec", "RabbitMQBus", "retry_exchange_name", "topology"]
