"""Bus contract: envelope, routing keys, queue names, publisher/consumer protocols (no aio-pika)."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from .models import NEWS_BUS_SCHEMA_VERSION

EXCHANGE: Final = "news"
DLX: Final = "news.dlx"
RETRY_EXCHANGE: Final = "news.retry"

Q_RAW: Final = "news.raw"
Q_TRIAGE: Final = "news.triage"
Q_DELIVER: Final = "news.deliver"
Q_RETRY: Final = "news.retry"
Q_DEAD: Final = "news.dead"

RK_RAW_LIVE: Final = "raw.opennews.{strategy_id}"
RK_RAW_RECOVERY: Final = "raw.recovery.{strategy_id}"
RK_EVENT: Final = "event.{family}.{priority}"
RK_VERDICT_PUSH: Final = "verdict.push"

RETRY_TTL_MS: Final = 30_000
MAX_TRANSIENT_ATTEMPTS: Final = 3

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
        ).encode("utf-8")


class BusDecodeError(ValueError):
    pass


def decode_body(body: bytes, *, routing_key: str, priority: int, headers: Mapping[str, Any] | None) -> BusMessage:
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BusDecodeError("news_bus_body_invalid") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != NEWS_BUS_SCHEMA_VERSION:
        raise BusDecodeError("news_bus_schema_invalid")
    kind = raw.get("kind")
    if kind not in {"raw", "event", "verdict"}:
        raise BusDecodeError("news_bus_kind_invalid")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise BusDecodeError("news_bus_payload_invalid")
    hdr = dict(headers or {})
    attempt = int(hdr.get("x-news-attempt") or 1)
    return BusMessage(
        kind=kind,
        message_id=str(raw.get("message_id") or ""),
        routing_key=routing_key,
        payload=payload,
        trace_id=str(raw.get("trace_id") or ""),
        occurred_at_ms=int(raw.get("occurred_at_ms") or 0),
        priority=int(priority or 0),
        attempt=attempt,
        headers=hdr,
    )


def new_trace_id() -> str:
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


class TransientError(RuntimeError):
    """Retryable failure of this message (upstream 5xx, statement timeout); counted, dead-lettered after 3."""


class DeferError(RuntimeError):
    """The process could not admit the message right now (DB lane saturated); requeued uncounted via the retry lane."""


class PermanentError(RuntimeError):
    """Non-retryable failure; message is dead-lettered after settlement."""


class Publisher(Protocol):
    async def publish(self, message: BusMessage) -> None: ...


Handler = Callable[[BusMessage], Awaitable[None]]


class Consumer(Protocol):
    async def consume(self, queue: str, handler: Handler, *, prefetch: int, stop_event: Any) -> None: ...


__all__ = [
    "DLX",
    "EXCHANGE",
    "MAX_TRANSIENT_ATTEMPTS",
    "Q_DEAD",
    "Q_DELIVER",
    "Q_RAW",
    "Q_RETRY",
    "Q_TRIAGE",
    "RETRY_EXCHANGE",
    "RETRY_TTL_MS",
    "RK_EVENT",
    "RK_RAW_LIVE",
    "RK_RAW_RECOVERY",
    "RK_VERDICT_PUSH",
    "BusDecodeError",
    "BusMessage",
    "Consumer",
    "DeferError",
    "Handler",
    "PermanentError",
    "Publisher",
    "TransientError",
    "decode_body",
    "new_trace_id",
    "now_ms",
]
