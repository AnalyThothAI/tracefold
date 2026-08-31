"""Bus contract: envelope, routing keys, queue names, settlement vocabulary (no aio-pika)."""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from .models import NEWS_BUS_SCHEMA_VERSION

EXCHANGE: Final = "news"
DLX: Final = "news.dlx"

Q_RAW: Final = "news.raw"
Q_TRIAGE: Final = "news.triage"
Q_DELIVER: Final = "news.deliver"
Q_DEAD: Final = "news.dead"

RK_RAW_LIVE: Final = "raw.opennews.{strategy_id}"
RK_RAW_RECOVERY: Final = "raw.recovery.{strategy_id}"
RK_EVENT: Final = "event.{dedupe_family}.{queue_priority}"
RK_VERDICT_PUSH: Final = "verdict.push"

# RabbitMQ 4.3 quorum queues count deliveries themselves; both headers below are broker-written and
# read-only for Tracefold. `x-delivery-count` counts only counted failures and is absent on the first
# delivery, so it is what an attempt number is derived from. `x-acquired-count` counts every time a
# consumer took the message, including the uncounted defer returns — measured present on every 4.3.5
# dead letter. The two diverge exactly when a message was deferred rather than failing, which is the
# one thing an operator reading `news dlq inspect` cannot tell from the delivery count alone.
DELIVERY_COUNT_HEADER: Final = "x-delivery-count"
ACQUIRED_COUNT_HEADER: Final = "x-acquired-count"
_BODY_FIELDS: Final = frozenset({"schema_version", "kind", "message_id", "trace_id", "occurred_at_ms", "payload"})
_IDENTIFIER_MAX_BYTES: Final = 128

MessageKind = Literal["raw", "event", "verdict"]


@dataclass(frozen=True, slots=True)
class BusMessage:
    kind: MessageKind
    message_id: str
    routing_key: str
    payload: Mapping[str, Any]
    trace_id: str
    occurred_at_ms: int
    priority: int = 0
    # Which handler attempt this delivery is. Derived from the broker's `x-delivery-count`; a publisher
    # never sets it and no application path may increment it.
    attempt: int = 1
    headers: Mapping[str, Any] = field(default_factory=dict)

    def body(self) -> bytes:
        return json.dumps(
            {
                "schema_version": NEWS_BUS_SCHEMA_VERSION,
                "kind": self.kind,
                "message_id": self.message_id,
                "trace_id": self.trace_id,
                "occurred_at_ms": self.occurred_at_ms,
                "payload": self.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class BrokerPublishFailure:
    """Latest confirmed-publish failure observed by one Workers process."""

    error_code: str
    at_ms: int


class BusDecodeError(ValueError):
    pass


class BrokerBackpressure(RuntimeError):
    """The broker rejected a confirmed publish."""


class BrokerUnavailable(RuntimeError):
    """The broker could not complete a confirmed publish."""


def decode_body(body: bytes, *, routing_key: str, priority: int, headers: Mapping[str, Any] | None) -> BusMessage:
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BusDecodeError("news_bus_body_invalid") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != NEWS_BUS_SCHEMA_VERSION:
        raise BusDecodeError("news_bus_schema_invalid")
    if set(raw) != _BODY_FIELDS:
        raise BusDecodeError("news_bus_fields_invalid")
    kind = raw.get("kind")
    if kind not in {"raw", "event", "verdict"}:
        raise BusDecodeError("news_bus_kind_invalid")
    payload = raw.get("payload")
    if not isinstance(payload, dict) or _has_nonfinite(payload):
        raise BusDecodeError("news_bus_payload_invalid")
    message_id = _identifier(raw.get("message_id"), field="message_id")
    trace_id = _identifier(raw.get("trace_id"), field="trace_id")
    occurred_at_ms = raw.get("occurred_at_ms")
    if type(occurred_at_ms) is not int or occurred_at_ms <= 0:
        raise BusDecodeError("news_bus_timestamp_invalid")
    hdr = dict(headers or {})
    return BusMessage(
        kind=kind,
        message_id=message_id,
        routing_key=routing_key,
        payload=payload,
        trace_id=trace_id,
        occurred_at_ms=occurred_at_ms,
        priority=int(priority or 0),
        attempt=_attempt_from_broker(hdr),
        headers=hdr,
    )


def _attempt_from_broker(headers: Mapping[str, Any]) -> int:
    """Which handler attempt this delivery is, read from the broker's own failed-delivery counter.

    `x-delivery-count` is absent on the first delivery and counts only deliveries the consumer returned
    as failures (`basic.reject` with requeue). An uncounted return (`basic.nack` with requeue, the defer
    lane) leaves it alone, so a deferred message never spends the transient budget. Anything other than
    a non-negative integer means the delivery cannot be attributed and fails closed.
    """

    if DELIVERY_COUNT_HEADER not in headers:
        return 1
    delivered = headers[DELIVERY_COUNT_HEADER]
    if type(delivered) is not int or delivered < 0:
        raise BusDecodeError("news_bus_delivery_count_invalid")
    return delivered + 1


def _identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _IDENTIFIER_MAX_BYTES:
        raise BusDecodeError(f"news_bus_{field}_invalid")
    return value


def _has_nonfinite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_has_nonfinite(key) or _has_nonfinite(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return any(_has_nonfinite(item) for item in value)
    return False


def new_trace_id() -> str:
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


class TransientError(RuntimeError):
    """Retryable failure of this message (upstream 5xx, statement timeout).

    Returned to the broker as a counted failure, delayed natively, and dead-lettered once the broker's
    delivery limit is spent."""


class DeferError(RuntimeError):
    """The process could not admit the message right now (DB lane saturated).

    Returned to the broker uncounted, so it is delayed like a transient failure but never spends the
    transient delivery budget and can never become terminal on its own."""


class PermanentError(RuntimeError):
    """Non-retryable failure; message is dead-lettered after settlement."""


Handler = Callable[[BusMessage], Awaitable[None]]

__all__ = [
    "ACQUIRED_COUNT_HEADER",
    "DELIVERY_COUNT_HEADER",
    "DLX",
    "EXCHANGE",
    "Q_DEAD",
    "Q_DELIVER",
    "Q_RAW",
    "Q_TRIAGE",
    "RK_EVENT",
    "RK_RAW_LIVE",
    "RK_RAW_RECOVERY",
    "RK_VERDICT_PUSH",
    "BrokerBackpressure",
    "BrokerPublishFailure",
    "BrokerUnavailable",
    "BusDecodeError",
    "BusMessage",
    "DeferError",
    "Handler",
    "PermanentError",
    "TransientError",
    "decode_body",
    "new_trace_id",
    "now_ms",
]
