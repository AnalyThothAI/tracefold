from __future__ import annotations

import asyncio
from typing import Any, cast

import aio_pika
import pytest
from aio_pika.exceptions import ChannelInvalidStateError

from tracefold.integrations.rabbitmq import RabbitMQBus
from tracefold.news.bus import BrokerUnavailable, BusMessage, DeferError, PermanentError, TransientError
from tracefold.news.telemetry import NewsDurableEventTelemetryPort, NewsRabbitConsumerFatalReason


def _event() -> BusMessage:
    return BusMessage(
        kind="event",
        message_id="event:test",
        routing_key="event.general.normal",
        payload={"event_id": "test"},
        trace_id="trace",
        occurred_at_ms=1,
    )


class _Incoming:
    def __init__(
        self,
        *,
        ack_error: BaseException | None = None,
        reject_error: BaseException | None = None,
        nack_error: BaseException | None = None,
        body: bytes | None = None,
    ) -> None:
        message = _event()
        self.body = message.body() if body is None else body
        self.routing_key = message.routing_key
        self.priority = 0
        self.headers: dict[str, object] = {}
        self.ack_error = ack_error
        self.reject_error = reject_error
        self.nack_error = nack_error
        self.actions: list[tuple[str, bool | None]] = []

    async def ack(self) -> None:
        if self.ack_error is not None:
            raise self.ack_error
        self.actions.append(("ack", None))

    async def reject(self, *, requeue: bool = False) -> None:
        if self.reject_error is not None:
            raise self.reject_error
        self.actions.append(("reject", requeue))

    async def nack(self, *, requeue: bool = True, multiple: bool = False) -> None:
        if self.nack_error is not None:
            raise self.nack_error
        self.actions.append(("nack", requeue))


class _Iterator:
    def __init__(self, incoming: _Incoming) -> None:
        self.incoming = incoming
        self.delivered = False

    async def __aenter__(self) -> _Iterator:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def __anext__(self) -> _Incoming:
        if not self.delivered:
            self.delivered = True
            return self.incoming
        await asyncio.Future()
        raise AssertionError("unreachable")


class _Queue:
    def __init__(self, incoming: _Incoming) -> None:
        self.incoming = incoming

    def iterator(self) -> _Iterator:
        return _Iterator(self.incoming)


class _Channel:
    def __init__(self, incoming: _Incoming, *, close_error: BaseException | None = None) -> None:
        self.incoming = incoming
        self.close_error = close_error
        self.is_closed = False
        self.prefetch: int | None = None

    async def set_qos(self, *, prefetch_count: int) -> None:
        self.prefetch = prefetch_count

    async def declare_queue(self, *_args: object, **_kwargs: object) -> _Queue:
        return _Queue(self.incoming)

    async def close(self) -> None:
        self.is_closed = True
        if self.close_error is not None:
            raise self.close_error


class _ConsumerBus(RabbitMQBus):
    def __init__(
        self,
        channel: _Channel,
        *,
        telemetry: _Telemetry | None = None,
        name_prefix: str = "",
    ) -> None:
        super().__init__(
            url="amqp://unused",
            telemetry=cast(NewsDurableEventTelemetryPort | None, telemetry),
            name_prefix=name_prefix,
        )
        self.channel = channel

    async def connect(self) -> None:
        return None

    async def _channel(self) -> Any:
        return self.channel


class _Telemetry:
    def __init__(self) -> None:
        self.fatals: list[tuple[str, NewsRabbitConsumerFatalReason]] = []

    def record_news_rabbitmq_consumer_fatal(
        self,
        queue: str,
        reason_class: NewsRabbitConsumerFatalReason,
    ) -> None:
        self.fatals.append((queue, reason_class))


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for nested in exc.exceptions for leaf in _leaves(nested)]
    return [exc]


def _run_one(handler: Any, incoming: _Incoming, *, telemetry: _Telemetry | None = None) -> None:
    """Deliver exactly one message to `handler` through the real consume loop, then stop."""

    async def scenario() -> None:
        channel = _Channel(incoming)
        bus = _ConsumerBus(channel, telemetry=telemetry)
        stop = asyncio.Event()

        async def once(message: BusMessage) -> None:
            try:
                await handler(message)
            finally:
                stop.set()

        await asyncio.wait_for(bus.consume("news.triage", once, prefetch=1, stop_event=stop), timeout=1)

    asyncio.run(scenario())


def test_success_acks_and_nothing_else() -> None:
    incoming = _Incoming()

    async def handler(_message: BusMessage) -> None:
        return None

    _run_one(handler, incoming)
    assert incoming.actions == [("ack", None)]


def test_transient_is_the_counted_broker_return_with_no_republish() -> None:
    """A transient failure settles as `basic.reject(requeue=True)` and nothing else.

    That settlement is what increments the broker's `x-delivery-count`, so the delivery limit — not an
    application counter — decides when the message becomes terminal. An ack, a nack or a republish here
    would each silently restore the deleted retry lane's semantics.
    """

    incoming = _Incoming()

    async def handler(_message: BusMessage) -> None:
        raise TransientError("try again")

    _run_one(handler, incoming)
    assert incoming.actions == [("reject", True)]


def test_defer_is_the_uncounted_broker_return() -> None:
    """A defer settles as `basic.nack(requeue=True)`, which RabbitMQ does not count as a failure."""

    incoming = _Incoming()

    async def handler(_message: BusMessage) -> None:
        raise DeferError("db lane busy")

    _run_one(handler, incoming)
    assert incoming.actions == [("nack", True)]


def test_permanent_failure_is_terminal_rejection() -> None:
    incoming = _Incoming()

    async def handler(_message: BusMessage) -> None:
        raise PermanentError("nope")

    _run_one(handler, incoming)
    assert incoming.actions == [("reject", False)]


def test_undecodable_body_is_terminal_rejection_without_reaching_the_handler() -> None:
    async def scenario() -> None:
        incoming = _Incoming(body=b"{}")
        channel = _Channel(incoming)
        bus = _ConsumerBus(channel)
        stop = asyncio.Event()
        seen: list[BusMessage] = []

        async def handler(message: BusMessage) -> None:
            seen.append(message)

        consumer = asyncio.create_task(bus.consume("news.triage", handler, prefetch=1, stop_event=stop))
        for _ in range(50):
            if incoming.actions:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(consumer, timeout=1)
        assert incoming.actions == [("reject", False)]
        assert seen == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (RuntimeError("handler bug"), "handler"),
        (BrokerUnavailable("handler publish failed"), "broker"),
    ],
)
def test_handler_failure_leaves_the_message_unsettled_and_fails_consume(
    failure: RuntimeError,
    reason: NewsRabbitConsumerFatalReason,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        incoming = _Incoming()
        channel = _Channel(incoming)
        telemetry = _Telemetry()
        bus = _ConsumerBus(channel, telemetry=telemetry, name_prefix="test-run")

        async def handler(_message: BusMessage) -> None:
            raise failure

        with pytest.raises(BaseExceptionGroup) as caught:
            await asyncio.wait_for(
                bus.consume("news.triage", handler, prefetch=1, stop_event=asyncio.Event()),
                timeout=1,
            )

        assert failure in _leaves(caught.value)
        assert incoming.actions == []
        assert channel.is_closed
        assert telemetry.fatals == [("news.triage", reason)]
        assert str(failure) not in caplog.text
        assert type(failure).__name__ in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("settlement", ["ack", "reject", "nack"])
def test_settlement_failure_fails_consume(settlement: str) -> None:
    async def scenario() -> None:
        failure = RuntimeError(f"{settlement} failed")
        incoming = _Incoming(
            ack_error=failure if settlement == "ack" else None,
            reject_error=failure if settlement == "reject" else None,
            nack_error=failure if settlement == "nack" else None,
        )
        channel = _Channel(incoming)
        telemetry = _Telemetry()
        bus = _ConsumerBus(channel, telemetry=telemetry)

        async def handler(_message: BusMessage) -> None:
            if settlement == "reject":
                raise PermanentError("bad message")
            if settlement == "nack":
                raise DeferError("db lane busy")

        with pytest.raises(BaseExceptionGroup) as caught:
            await asyncio.wait_for(
                bus.consume("news.triage", handler, prefetch=1, stop_event=asyncio.Event()),
                timeout=1,
            )

        assert failure in _leaves(caught.value)
        assert channel.is_closed
        assert telemetry.fatals == [("news.triage", "settlement")]

    asyncio.run(scenario())


def test_channel_close_failure_is_not_hidden() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        incoming = _Incoming()
        failure = RuntimeError("channel close failed")
        telemetry = _Telemetry()
        bus = _ConsumerBus(_Channel(incoming, close_error=failure), telemetry=telemetry)

        async def handler(_message: BusMessage) -> None:
            stop.set()

        with pytest.raises(RuntimeError, match="channel close failed"):
            await asyncio.wait_for(bus.consume("news.triage", handler, prefetch=1, stop_event=stop), timeout=1)

        assert incoming.actions == [("ack", None)]
        assert telemetry.fatals == [("news.triage", "settlement")]

    asyncio.run(scenario())


def test_graceful_stop_waits_for_the_inflight_handler() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        started = asyncio.Event()
        release = asyncio.Event()
        incoming = _Incoming()
        bus = _ConsumerBus(_Channel(incoming))

        async def handler(_message: BusMessage) -> None:
            started.set()
            await release.wait()

        consumer = asyncio.create_task(bus.consume("news.triage", handler, prefetch=1, stop_event=stop))
        await asyncio.wait_for(started.wait(), timeout=1)
        stop.set()
        await asyncio.sleep(0)
        assert not consumer.done()
        release.set()
        await asyncio.wait_for(consumer, timeout=1)
        assert incoming.actions == [("ack", None)]

    asyncio.run(scenario())


def test_connect_clears_partial_initialization_and_enables_return_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def __init__(self) -> None:
            self.is_closed = False
            self.closed = False
            self.channel_options: dict[str, object] | None = None

        async def channel(self, **options: object) -> object:
            self.channel_options = options
            return object()

        async def close(self) -> None:
            self.closed = True
            self.is_closed = True

    async def scenario() -> None:
        first = Connection()
        second = Connection()
        connections = iter((first, second))

        async def connect_robust(*_args: object, **_kwargs: object) -> Connection:
            return next(connections)

        monkeypatch.setattr(aio_pika, "connect_robust", connect_robust)
        bus = RabbitMQBus(url="amqp://unused")

        async def fail_declare(_channel: object) -> dict[str, object]:
            raise ChannelInvalidStateError("topology failed")

        monkeypatch.setattr(bus, "declare_topology", fail_declare)
        with pytest.raises(BrokerUnavailable, match="news_broker_connect_failed"):
            await bus.connect()
        assert first.closed
        assert bus._connection is None
        assert bus._publish_channel is None
        assert bus._exchange is None

        async def declare(_channel: object) -> dict[str, object]:
            bus._exchange = cast(Any, object())
            return {}

        monkeypatch.setattr(bus, "declare_topology", declare)
        await bus.connect()
        assert second.channel_options == {"publisher_confirms": True, "on_return_raises": True}

    asyncio.run(scenario())
