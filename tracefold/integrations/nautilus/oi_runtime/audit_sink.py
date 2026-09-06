"""Bounded callback-safe Observation buffer and durable day-start fact helpers."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from threading import Lock
from typing import Any, Literal

from tracefold.trading import (
    MAX_OBSERVATION_APPEND_BATCH,
    MAX_OBSERVATION_APPEND_BYTES,
    ExecutionObservationV1,
)

from .risk import DayStartBaseline

_DEFAULT_MAX_COUNT = 1_024
_DEFAULT_MAX_BYTES = 4 * 1_048_576
# One flush is one durable append, so the buffer stops at exactly what the durable writer accepts;
# the two numbers used to be re-typed on both sides of the seam (#510 E).
_FLUSH_COUNT = MAX_OBSERVATION_APPEND_BATCH
_FLUSH_BYTES = MAX_OBSERVATION_APPEND_BYTES


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservationFactory:
    account_slot: str
    execution_strategy: str

    def create(
        self,
        *,
        normalized_kind: str,
        occurred_at_ns: int,
        observed_at_ns: int,
        signal_id: str | None = None,
        command_id: str | None = None,
        native_identity_references: Sequence[str] = (),
        summary: Mapping[str, str | int | bool] | None = None,
        payload: object,
        event_identity: str | None = None,
        fixed_event_id: str | None = None,
    ) -> ExecutionObservationV1:
        # The native payload is not stored (#520 PR-C), but its digest and the normalized
        # reference set still fix the event id, so no two native events collapse onto one.
        references = tuple(sorted(set(native_identity_references)))
        event_id = fixed_event_id or _sha256(
            {
                "account_slot": self.account_slot,
                "strategy": self.execution_strategy,
                "kind": normalized_kind,
                "signal": signal_id,
                "command": command_id,
                "native": references,
                "payload": _sha256(payload),
                "identity": event_identity,
            }
        )
        return ExecutionObservationV1.model_validate(
            {
                "event_id": event_id,
                "account_slot": self.account_slot,
                "execution_strategy": self.execution_strategy,
                "signal_id": signal_id,
                "command_id": command_id,
                "normalized_kind": normalized_kind,
                "occurred_at_ns": occurred_at_ns,
                "observed_at_ns": observed_at_ns,
                "native_identity_references": references,
                "summary": dict(summary or {}),
            }
        )

    def day_start_baseline(
        self,
        *,
        utc_day: str,
        equity_usd: Decimal,
        recorded_at_ns: int,
    ) -> tuple[DayStartBaseline, ExecutionObservationV1]:
        if not equity_usd.is_finite() or equity_usd <= 0:
            raise ValueError("oi_runtime_day_start_equity_precision_invalid")
        # One encoding of one number. The scaled-integer copy beside it could only ever be written
        # when it was exactly representable, so every row that had it also had the decimal, and the
        # reader below had to decide which of two encodings of the same equity to believe (#537 PR-4).
        summary: dict[str, str | int] = {
            "risk_fact": "day_start_equity",
            "utc_day": utc_day,
            "equity_usd_decimal": format(equity_usd, "f"),
        }
        event_identity = f"day-start:{utc_day}"
        fixed_event_id = self.day_start_event_id(utc_day)
        observation = self.create(
            normalized_kind="risk",
            occurred_at_ns=recorded_at_ns,
            observed_at_ns=recorded_at_ns,
            summary=summary,
            payload=summary,
            event_identity=event_identity,
            fixed_event_id=fixed_event_id,
        )
        return (
            DayStartBaseline(
                utc_day=utc_day,
                equity_usd=equity_usd,
                recorded_at_ns=recorded_at_ns,
                event_id=observation.event_id,
            ),
            observation,
        )

    def day_start_event_id(self, utc_day: str) -> str:
        if len(utc_day) != 10:
            raise ValueError("oi_runtime_utc_day_invalid")
        return _sha256(
            {
                "account_slot": self.account_slot,
                "strategy": self.execution_strategy,
                "risk_fact": "day_start_equity",
                "utc_day": utc_day,
            }
        )


def day_start_baseline_from_observation(observation: ExecutionObservationV1) -> DayStartBaseline:
    summary = observation.summary
    if observation.normalized_kind != "risk" or summary.get("risk_fact") != "day_start_equity":
        raise ValueError("oi_runtime_day_start_observation_invalid")
    utc_day = summary.get("utc_day")
    decimal_value = summary.get("equity_usd_decimal")
    if not isinstance(utc_day, str) or not isinstance(decimal_value, str):
        raise ValueError("oi_runtime_day_start_observation_invalid")
    try:
        equity_usd = Decimal(decimal_value)
    except ArithmeticError as exc:
        raise ValueError("oi_runtime_day_start_observation_invalid") from exc
    if not equity_usd.is_finite() or equity_usd <= 0 or format(equity_usd, "f") != decimal_value:
        raise ValueError("oi_runtime_day_start_observation_invalid")
    return DayStartBaseline(
        utc_day=utc_day,
        equity_usd=equity_usd,
        recorded_at_ns=observation.occurred_at_ns,
        event_id=observation.event_id,
    )


type AuditGapCause = Literal["audit_identity_conflict", "audit_append_rejected", "audit_queue_overflow"]


@dataclass(slots=True)
class _AuditGap:
    """One cause of audit loss, accumulating until the record that names it is durable.

    There were three near-identical copies of this: one per cause, each with its own five counters,
    its own sequence, its own `_enqueue_*_when_room` and its own summary shape, so an overflow record
    said `dropped_count` while an identity conflict said `conflict_count` and only one of the three
    named the first event id it lost. They are one fact with three causes (#537 PR-4).
    """

    cause: AuditGapCause
    sequence: int = 0
    dropped_count: int = 0
    first_event_id: str | None = None
    kind_counts: dict[str, int] = field(default_factory=dict)
    started_at_ns: int = 0
    last_observed_at_ns: int = 0
    pending_event_id: str | None = None

    @property
    def outstanding(self) -> bool:
        """Some loss this cause explains is not yet durably recorded."""

        return self.dropped_count > 0 or self.pending_event_id is not None

    def record(self, value: ExecutionObservationV1) -> None:
        if self.dropped_count == 0:
            self.first_event_id = value.event_id
            self.started_at_ns = value.occurred_at_ns
        self.dropped_count += 1
        self.last_observed_at_ns = max(self.last_observed_at_ns, value.observed_at_ns)
        self.kind_counts[value.normalized_kind] = self.kind_counts.get(value.normalized_kind, 0) + 1

    def settled(self) -> None:
        self.dropped_count = 0
        self.first_event_id = None
        self.kind_counts = {}
        self.started_at_ns = 0
        self.last_observed_at_ns = 0


class AuditAppendRejected(Exception):
    """The database refused a batch on integrity grounds, so retrying it cannot ever succeed.

    The App-side writer is the only place that knows psycopg, so it translates
    `psycopg.errors.IntegrityError` into this. Everything the sink does with a rejection - drop the
    batch, record the gap, keep going - depends only on "no retry can fix this", which is exactly what
    an integrity error means and exactly what a connection or timeout error does not.
    """


class AuditSink:
    """Native callbacks append in memory; a background owner performs PostgreSQL I/O."""

    def __init__(
        self,
        *,
        factory: ObservationFactory,
        max_count: int = _DEFAULT_MAX_COUNT,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        if max_count <= 0 or max_bytes <= 0:
            raise ValueError("oi_runtime_audit_bounds_invalid")
        self._max_count = max_count
        self._max_bytes = max_bytes
        self.factory = factory
        self._values: deque[tuple[ExecutionObservationV1, int]] = deque()
        # The queue, indexed by the identity `offer` answers on. A native callback offers on the
        # trading event loop, and answering "is this event already queued" by walking the deque made
        # that answer cost the whole queue -- up to `max_count` comparisons per callback, worst at
        # exactly the moment the queue is deepest (#589 PR-2). The dict is maintained by the two
        # writers below and holds the same values the deque does.
        self._index: dict[str, ExecutionObservationV1] = {}
        self._bytes = 0
        self._healthy = True
        self._failure_reason: str | None = None
        # Ordered by how much they say about the ledger: a conflicting identity is a contradiction,
        # a refused batch is a verdict, an overflow is pressure. `failure_reason` reports the first
        # one still outstanding.
        self._gaps: tuple[_AuditGap, ...] = (
            _AuditGap("audit_identity_conflict"),
            _AuditGap("audit_append_rejected"),
            _AuditGap("audit_queue_overflow"),
        )
        self._lock = Lock()

    @property
    def healthy(self) -> bool:
        """Whether every observation offered so far is still on its way to PostgreSQL.

        This is a status fact the operator page and the `audit_gap` observations report, not a gate.
        Binance keeps the account's own order and fill history; refusing to open a position because
        the local audit copy of it is unwritable spends the risk of *not* acting to protect a copy
        (#520 PR-B).
        """

        with self._lock:
            return self._healthy

    @property
    def failure_reason(self) -> str | None:
        with self._lock:
            return self._failure_reason

    def offer(self, value: ExecutionObservationV1) -> bool:
        size = len(value.model_dump_json().encode())
        with self._lock:
            queued = self._index.get(value.event_id)
            if queued is not None:
                # Same identity: an identical re-offer is already on its way, a different body under
                # the same identity is a contradiction the ledger has to name.
                if queued == value:
                    return True
                self._gap("audit_identity_conflict").record(value)
                self._republish_health()
                return False
            if len(self._values) >= self._max_count or self._bytes + size > self._max_bytes:
                self._gap("audit_queue_overflow").record(value)
                self._republish_health()
                return False
            self._append(value, size)
            return True

    def _append(self, value: ExecutionObservationV1, size: int) -> None:
        """Queue one value and index it. Called with the lock held, by `offer` and by the gap writer."""

        self._values.append((value, size))
        self._index[value.event_id] = value
        self._bytes += size

    def flush_once(
        self,
        writer: Callable[[Sequence[ExecutionObservationV1]], Any],
    ) -> tuple[ExecutionObservationV1, ...]:
        """Drain what the database will take, and return everything that left the queue.

        Returned values are the ones the caller may now settle: durably appended, or quarantined
        because the database refused them. A quarantined disposition still resolves its Signal or
        Command - the runtime lost the audit fact, not the input, and leaving inputs pending forever
        is how a single rejected observation stopped a whole runtime on 2026-09-02 (#510 A).

        A transient failure (connection, timeout) still keeps its batch at the head and re-raises.
        """

        dequeued: list[ExecutionObservationV1] = []
        quarantined = False
        while True:
            batch = self._next_batch()
            if not batch:
                break
            try:
                writer(tuple(batch))
            except AuditAppendRejected:
                if quarantined:
                    # One poisoned batch per pass. A systemically rejected queue drains one batch per
                    # cycle instead of spinning here, and the gap this pass wrote is already durable.
                    break
                self._quarantine(batch)
                dequeued.extend(batch)
                quarantined = True
                continue
            except Exception:
                with self._lock:
                    self._healthy = False
                    self._failure_reason = "audit_append_failed"
                raise
            self._settle(batch)
            dequeued.extend(batch)
            break
        return tuple(dequeued)

    def _next_batch(self) -> list[ExecutionObservationV1]:
        with self._lock:
            self._enqueue_gaps_when_room()
            batch: list[ExecutionObservationV1] = []
            batch_bytes = 0
            for value, size in self._values:
                if len(batch) >= _FLUSH_COUNT or batch_bytes + size > _FLUSH_BYTES:
                    break
                batch.append(value)
                batch_bytes += size
            return batch

    def _settle(self, batch: Sequence[ExecutionObservationV1]) -> None:
        event_ids = self._drop(batch)
        with self._lock:
            for gap in self._gaps:
                if gap.pending_event_id is not None and gap.pending_event_id in event_ids:
                    gap.pending_event_id = None
            self._enqueue_gaps_when_room()
            self._republish_health()

    def _quarantine(self, batch: Sequence[ExecutionObservationV1]) -> None:
        """Take a refused batch out of the queue and start the gap record that replaces it."""

        self._drop(batch)
        with self._lock:
            rejected = self._gap("audit_append_rejected")
            for value in batch:
                rejected.record(value)
            self._enqueue_gaps_when_room()
            self._republish_health()

    def _drop(self, batch: Sequence[ExecutionObservationV1]) -> tuple[str, ...]:
        event_ids = tuple(value.event_id for value in batch)
        with self._lock:
            for event_id in event_ids:
                queued, size = self._values.popleft()
                if queued.event_id != event_id:
                    raise RuntimeError("oi_runtime_audit_queue_corrupted")
                if self._index.get(event_id) is queued:
                    del self._index[event_id]
                self._bytes -= size
        return event_ids

    def _gap(self, cause: AuditGapCause) -> _AuditGap:
        for gap in self._gaps:
            if gap.cause == cause:
                return gap
        raise RuntimeError("oi_runtime_audit_gap_cause_unknown")

    def _republish_health(self) -> None:
        """Unhealthy exactly while some loss of audit truth is not yet durably recorded."""

        outstanding = next((gap for gap in self._gaps if gap.outstanding), None)
        self._healthy = outstanding is None
        self._failure_reason = None if outstanding is None else outstanding.cause

    def _enqueue_gaps_when_room(self) -> None:
        """Name what the ledger lost: the cause, how many, the first identity, and which kinds.

        `ExecutionObservationV1` allows a summary of 16 keys and 2048 bytes. Three fixed keys plus
        the ten-value `normalized_kind` vocabulary is 13 keys of short names and integers, so this
        summary cannot itself be refused by the contract the gap record exists to report on.
        """

        for gap in self._gaps:
            if gap.dropped_count == 0 or gap.pending_event_id is not None:
                continue
            gap.sequence += 1
            kind_counts = dict(sorted(gap.kind_counts.items()))
            summary: dict[str, str | int | bool] = {
                "cause": gap.cause,
                "dropped_count": gap.dropped_count,
                "first_event_id": gap.first_event_id or "",
            }
            summary.update((f"kind.{kind}", count) for kind, count in kind_counts.items())
            value = self.factory.create(
                normalized_kind="audit_gap",
                occurred_at_ns=gap.started_at_ns,
                observed_at_ns=gap.last_observed_at_ns,
                summary=summary,
                payload={
                    "cause": gap.cause,
                    "dropped_count": gap.dropped_count,
                    "first_event_id": gap.first_event_id,
                    "kind_counts": kind_counts,
                    "gap_sequence": gap.sequence,
                },
                event_identity=(
                    f"{gap.cause}:{gap.sequence}:{gap.first_event_id}:"
                    f"{gap.started_at_ns}:{gap.last_observed_at_ns}:{gap.dropped_count}"
                ),
            )
            size = len(value.model_dump_json().encode())
            if len(self._values) >= self._max_count or self._bytes + size > self._max_bytes:
                gap.sequence -= 1
                continue
            self._append(value, size)
            gap.pending_event_id = value.event_id
            gap.settled()


__all__ = [
    "AuditAppendRejected",
    "AuditSink",
    "ObservationFactory",
    "day_start_baseline_from_observation",
]
