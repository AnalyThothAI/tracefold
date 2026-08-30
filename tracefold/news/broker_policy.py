"""The RabbitMQ policy document that owns News retry, dead lettering and resource bounds (#400).

RabbitMQ 4.3 quorum queues carry retry natively: a returned delivery is delayed inside the same queue,
the broker counts failed deliveries in ``x-delivery-count``, and ``delivery-limit`` decides when a
message becomes terminal. Tracefold therefore keeps no retry lane, scheduler or attempt header; this
module is the only place the retry contract is written down, and the checked-in
``docker/rabbitmq/definitions.json`` is generated from it.

The application never repairs policy drift. It declares queue type, exchanges and bindings, verifies
that the effective policy matches this document, and fails closed when it does not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from .bus import DLX, Q_DEAD, Q_DELIVER, Q_RAW, Q_TRIAGE

# --- retry contract -------------------------------------------------------------------------------
# Frozen from the pre-#400 TTL lane so the migration changes the mechanism, not the observable timing.
RETRY_DELAY_MS: Final = 30_000
# `delayed-retry-type=all` delays both counted returns (basic.reject requeue=true) and uncounted ones
# (basic.nack requeue=true), so a deferred message waits exactly as long as a transient one did.
RETRY_TYPE: Final = "all"
# Measured on RabbitMQ 4.3.5, not inferred: a quorum queue delivers `delivery-limit + 1` times before
# dead-lettering, because the first delivery carries no `x-delivery-count` at all. Three total handler
# attempts is the frozen transient budget, so the limit is two.
TOTAL_TRANSIENT_ATTEMPTS: Final = 3
TRANSIENT_DELIVERY_LIMIT: Final = TOTAL_TRANSIENT_ATTEMPTS - 1
# `news.dead` is terminal: it has nowhere left to dead-letter to, so anything that spends a delivery
# budget there destroys evidence outright. Operator inspection returns each message uncounted, but the
# quorum default limit of 20 is still only twenty chances for anything else — a manual drain, a future
# tool, a consumer someone attaches — to lose a dead letter by returning it. This lifts that ceiling. It
# is queue state rather than retry policy, and it is the one thing `news.dead` declares for itself.
DEAD_LETTER_DELIVERY_LIMIT: Final = 1_000_000

# --- resource bounds ------------------------------------------------------------------------------
# The byte bounds are computed from measurements, not chosen. `docs/OPERATIONS.md` records where each
# number came from and how to re-measure it; the two dictionaries below are the whole input.
#
# Hitting a queue's byte bound rejects that queue's publishes as a typed `BrokerBackpressure`, which
# opens an incident and later replays through Recovery. Hitting the node's `vm_memory_high_watermark`
# instead blocks every publisher on the broker with no typed signal at all. The queue bound must trip
# first, which is what BROKER_BYTE_BUDGET keeps true.
MIB: Final = 1024 * 1024
BACKLOG_MINUTES: Final = 10
MIN_QUEUE_BYTES: Final = 4 * MIB
BROKER_BYTE_BUDGET: Final = 96 * MIB

# Measured 2026-08-30 on the production broker and a 7-day PostgreSQL window. Envelope sizes are the
# broker's own `message_bytes` (body plus AMQP properties and headers), rounded up; the raw figure is
# the p99 of real published frames, and the two small queues carry a fixed-shape `{event_id}` payload.
P99_ENVELOPE_BYTES: Final[dict[str, int]] = {Q_RAW: 2048, Q_TRIAGE: 512, Q_DELIVER: 512, Q_DEAD: 2048}
# Worst single minute observed over that window. `news.raw`'s peak is a Recovery backfill, which is
# itself bounded to 1,000 published messages per 30-second run.
PEAK_MESSAGES_PER_MINUTE: Final[dict[str, int]] = {Q_RAW: 2882, Q_TRIAGE: 111, Q_DELIVER: 7}
# `news.dead` is terminal evidence rather than arrival-driven, so it is sized as a message count an
# operator can still page through instead of as a backlog duration.
DEAD_LETTER_EVIDENCE_MESSAGES: Final = 8192

# The broker this contract was measured against and the version the definitions document declares.
RABBIT_VERSION: Final = "4.3.5"

BUSINESS_QUEUES: Final = (Q_RAW, Q_TRIAGE, Q_DELIVER)
POLICY_QUEUES: Final = (*BUSINESS_QUEUES, Q_DEAD)
POLICY_PRIORITY: Final = 10


def _ceil_power_of_two_mib(value: int) -> int:
    mib = -(-value // MIB)
    return (1 << (mib - 1).bit_length()) * MIB if mib > 1 else MIB


def max_length_bytes(queue: str) -> int:
    """`p99 envelope x peak arrival per minute x BACKLOG_MINUTES`, rounded up to a power-of-two MiB.

    The floor keeps an ordinary burst nowhere near the bound on the two queues whose traffic is tiny,
    and `news.dead` is sized by evidence count because nothing arrives there on a schedule.
    """

    if queue == Q_DEAD:
        return _ceil_power_of_two_mib(P99_ENVELOPE_BYTES[Q_DEAD] * DEAD_LETTER_EVIDENCE_MESSAGES)
    computed = P99_ENVELOPE_BYTES[queue] * PEAK_MESSAGES_PER_MINUTE[queue] * BACKLOG_MINUTES
    return max(MIN_QUEUE_BYTES, _ceil_power_of_two_mib(computed))


MAX_LENGTH_BYTES: Final[dict[str, int]] = {
    queue: max_length_bytes(queue) for queue in (Q_RAW, Q_TRIAGE, Q_DELIVER, Q_DEAD)
}


@dataclass(frozen=True, slots=True)
class BrokerPolicy:
    """One RabbitMQ policy: exactly one queue, exactly one definition."""

    name: str
    pattern: str
    queue: str
    definition: dict[str, Any]

    def as_definition_entry(self, *, vhost: str) -> dict[str, Any]:
        return {
            "vhost": vhost,
            "name": self.name,
            "pattern": self.pattern,
            "apply-to": "queues",
            "priority": POLICY_PRIORITY,
            "definition": dict(self.definition),
        }


def _q(name_prefix: str, name: str) -> str:
    return f"{name_prefix}.{name}" if name_prefix else name


def _policy_name(name_prefix: str, name: str) -> str:
    return _q(name_prefix, name).replace(".", "-")


def policies(*, name_prefix: str = "", delay_ms: int = RETRY_DELAY_MS) -> tuple[BrokerPolicy, ...]:
    """The four policies that configure one News topology.

    Each policy names exactly one queue, so a prefixed test topology and the production topology never
    contend for the single policy RabbitMQ applies to a queue.
    """

    delay = int(delay_ms)
    dlx = _q(name_prefix, DLX)
    business = {
        "delayed-retry-type": RETRY_TYPE,
        "delayed-retry-min": delay,
        "delayed-retry-max": delay,
        "delivery-limit": TRANSIENT_DELIVERY_LIMIT,
        "dead-letter-strategy": "at-least-once",
        "dead-letter-exchange": dlx,
        "overflow": "reject-publish",
    }
    out = [
        BrokerPolicy(
            name=_policy_name(name_prefix, queue),
            pattern=f"^{_q(name_prefix, queue).replace('.', chr(92) + '.')}$",
            queue=_q(name_prefix, queue),
            definition={**business, "max-length-bytes": MAX_LENGTH_BYTES[queue]},
        )
        for queue in BUSINESS_QUEUES
    ]
    out.append(
        BrokerPolicy(
            name=_policy_name(name_prefix, Q_DEAD),
            pattern=f"^{_q(name_prefix, Q_DEAD).replace('.', chr(92) + '.')}$",
            queue=_q(name_prefix, Q_DEAD),
            # The dead-letter queue is terminal: no retry, no delay, no onward dead lettering. It keeps
            # the same reject-publish contract so a full DLQ holds the source message instead of
            # dropping evidence, and the evidence-preserving delivery limit its own declaration carries.
            definition={
                "overflow": "reject-publish",
                "max-length-bytes": MAX_LENGTH_BYTES[Q_DEAD],
            },
        )
    )
    return tuple(out)


def definitions_document(*, name_prefix: str = "", vhost: str = "/", delay_ms: int = RETRY_DELAY_MS) -> dict[str, Any]:
    """A RabbitMQ definitions document carrying only these policies.

    Importing it through the management API's definitions endpoint adds or replaces the four policies
    and leaves users, vhosts, permissions, exchanges and queues untouched.
    """

    return {
        "rabbit_version": RABBIT_VERSION,
        "policies": [
            policy.as_definition_entry(vhost=vhost) for policy in policies(name_prefix=name_prefix, delay_ms=delay_ms)
        ],
    }


def definitions_json(*, name_prefix: str = "", vhost: str = "/") -> str:
    return json.dumps(definitions_document(name_prefix=name_prefix, vhost=vhost), indent=2, sort_keys=True) + "\n"


def expected_effective_definitions(
    *, name_prefix: str = "", delay_ms: int = RETRY_DELAY_MS
) -> dict[str, dict[str, Any]]:
    """Queue name -> the effective policy definition RabbitMQ must report for it."""

    return {policy.queue: dict(policy.definition) for policy in policies(name_prefix=name_prefix, delay_ms=delay_ms)}


__all__ = [
    "BACKLOG_MINUTES",
    "BROKER_BYTE_BUDGET",
    "BUSINESS_QUEUES",
    "DEAD_LETTER_DELIVERY_LIMIT",
    "MAX_LENGTH_BYTES",
    "MIB",
    "MIN_QUEUE_BYTES",
    "P99_ENVELOPE_BYTES",
    "PEAK_MESSAGES_PER_MINUTE",
    "POLICY_PRIORITY",
    "POLICY_QUEUES",
    "RABBIT_VERSION",
    "RETRY_DELAY_MS",
    "RETRY_TYPE",
    "TOTAL_TRANSIENT_ATTEMPTS",
    "TRANSIENT_DELIVERY_LIMIT",
    "BrokerPolicy",
    "definitions_document",
    "definitions_json",
    "expected_effective_definitions",
    "max_length_bytes",
    "policies",
]
