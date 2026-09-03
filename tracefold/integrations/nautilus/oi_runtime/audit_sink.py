"""Bounded callback-safe Observation buffer and durable day-start fact helpers."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from typing import Any

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
_ENTRY_RESERVE_COUNT = 8
_ENTRY_RESERVE_BYTES = 64 * 1_024
_EQUITY_SCALE = Decimal(1_000_000)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservationFactory:
    runtime_profile_id: str
    runtime_release: str
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
        payload_digest = _sha256(payload)
        references = tuple(sorted(set(native_identity_references)))
        event_id = fixed_event_id or _sha256(
            {
                "profile": self.runtime_profile_id,
                "release": self.runtime_release,
                "strategy": self.execution_strategy,
                "kind": normalized_kind,
                "signal": signal_id,
                "command": command_id,
                "native": references,
                "payload": payload_digest,
                "identity": event_identity,
            }
        )
        return ExecutionObservationV1.model_validate(
            {
                "event_id": event_id,
                "runtime_profile_id": self.runtime_profile_id,
                "runtime_release": self.runtime_release,
                "execution_strategy": self.execution_strategy,
                "signal_id": signal_id,
                "command_id": command_id,
                "normalized_kind": normalized_kind,
                "occurred_at_ns": occurred_at_ns,
                "observed_at_ns": observed_at_ns,
                "native_identity_references": references,
                "summary": dict(summary or {}),
                "payload_digest": payload_digest,
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
        scaled = equity_usd * _EQUITY_SCALE
        summary: dict[str, str | int] = {
            "risk_fact": "day_start_equity",
            "utc_day": utc_day,
            "equity_usd_decimal": format(equity_usd, "f"),
        }
        if scaled == scaled.to_integral_value():
            summary["equity_usd_micros"] = int(scaled)
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
                "profile": self.runtime_profile_id,
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
    micros = summary.get("equity_usd_micros")
    if not isinstance(utc_day, str):
        raise ValueError("oi_runtime_day_start_observation_invalid")
    if isinstance(decimal_value, str):
        try:
            equity_usd = Decimal(decimal_value)
        except ArithmeticError as exc:
            raise ValueError("oi_runtime_day_start_observation_invalid") from exc
        if not equity_usd.is_finite() or equity_usd <= 0 or format(equity_usd, "f") != decimal_value:
            raise ValueError("oi_runtime_day_start_observation_invalid")
    elif type(micros) is int and micros > 0:
        equity_usd = Decimal(micros) / _EQUITY_SCALE
    else:
        raise ValueError("oi_runtime_day_start_observation_invalid")
    return DayStartBaseline(
        utc_day=utc_day,
        equity_usd=equity_usd,
        recorded_at_ns=observation.occurred_at_ns,
        event_id=observation.event_id,
    )


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
        self._bytes = 0
        self._healthy = True
        self._failure_reason: str | None = None
        self._gap_sequence = 0
        self._gap_dropped_count = 0
        self._gap_started_at_ns = 0
        self._gap_last_observed_at_ns = 0
        self._gap_event_id: str | None = None
        self._conflict_sequence = 0
        self._conflict_count = 0
        self._conflict_first_event_id: str | None = None
        self._conflict_started_at_ns = 0
        self._conflict_last_observed_at_ns = 0
        self._conflict_gap_event_id: str | None = None
        self._rejected_sequence = 0
        self._rejected_count = 0
        self._rejected_first_event_id: str | None = None
        self._rejected_kind_counts: dict[str, int] = {}
        self._rejected_started_at_ns = 0
        self._rejected_last_observed_at_ns = 0
        self._rejected_gap_event_id: str | None = None
        self._lock = Lock()

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._healthy

    @property
    def failure_reason(self) -> str | None:
        with self._lock:
            return self._failure_reason

    @property
    def queued_count(self) -> int:
        with self._lock:
            return len(self._values)

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def can_accept_exposure(self) -> bool:
        with self._lock:
            return (
                self._healthy
                and len(self._values) + _ENTRY_RESERVE_COUNT <= self._max_count
                and self._bytes + _ENTRY_RESERVE_BYTES <= self._max_bytes
            )

    def offer(self, value: ExecutionObservationV1) -> bool:
        size = len(value.model_dump_json().encode())
        with self._lock:
            for queued, _ in self._values:
                if queued.event_id != value.event_id:
                    continue
                if queued == value:
                    return True
                self._healthy = False
                self._failure_reason = "audit_identity_conflict"
                if self._conflict_count == 0:
                    self._conflict_first_event_id = value.event_id
                    self._conflict_started_at_ns = value.occurred_at_ns
                self._conflict_count += 1
                self._conflict_last_observed_at_ns = max(
                    self._conflict_last_observed_at_ns,
                    value.observed_at_ns,
                )
                return False
            if len(self._values) >= self._max_count or self._bytes + size > self._max_bytes:
                self._healthy = False
                self._failure_reason = "audit_queue_overflow"
                if self._gap_dropped_count == 0:
                    self._gap_started_at_ns = value.occurred_at_ns
                self._gap_dropped_count += 1
                self._gap_last_observed_at_ns = max(self._gap_last_observed_at_ns, value.observed_at_ns)
                return False
            self._values.append((value, size))
            self._bytes += size
            return True

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
            self._enqueue_gap_when_room()
            self._enqueue_conflict_gap_when_room()
            self._enqueue_rejected_gap_when_room()
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
            if self._gap_event_id is not None and self._gap_event_id in event_ids:
                self._gap_event_id = None
            if self._conflict_gap_event_id is not None and self._conflict_gap_event_id in event_ids:
                self._conflict_gap_event_id = None
            if self._rejected_gap_event_id is not None and self._rejected_gap_event_id in event_ids:
                self._rejected_gap_event_id = None
            self._enqueue_gap_when_room()
            self._enqueue_conflict_gap_when_room()
            self._enqueue_rejected_gap_when_room()
            self._republish_health()

    def _quarantine(self, batch: Sequence[ExecutionObservationV1]) -> None:
        """Take a refused batch out of the queue and start the gap record that replaces it."""

        self._drop(batch)
        with self._lock:
            if self._rejected_count == 0:
                self._rejected_first_event_id = batch[0].event_id
                self._rejected_started_at_ns = batch[0].occurred_at_ns
            self._rejected_count += len(batch)
            self._rejected_last_observed_at_ns = max(
                self._rejected_last_observed_at_ns,
                *(value.observed_at_ns for value in batch),
            )
            for value in batch:
                self._rejected_kind_counts[value.normalized_kind] = (
                    self._rejected_kind_counts.get(value.normalized_kind, 0) + 1
                )
            self._enqueue_rejected_gap_when_room()
            self._republish_health()

    def _drop(self, batch: Sequence[ExecutionObservationV1]) -> tuple[str, ...]:
        event_ids = tuple(value.event_id for value in batch)
        with self._lock:
            for event_id in event_ids:
                queued, size = self._values.popleft()
                if queued.event_id != event_id:
                    raise RuntimeError("oi_runtime_audit_queue_corrupted")
                self._bytes -= size
        return event_ids

    def _republish_health(self) -> None:
        """Unhealthy exactly while some loss of audit truth is not yet durably recorded."""

        if self._conflict_count > 0 or self._conflict_gap_event_id is not None:
            self._healthy = False
            self._failure_reason = "audit_identity_conflict"
        elif self._rejected_count > 0 or self._rejected_gap_event_id is not None:
            self._healthy = False
            self._failure_reason = "audit_append_rejected"
        elif self._gap_dropped_count == 0 and self._gap_event_id is None:
            self._healthy = True
            self._failure_reason = None
        else:
            self._healthy = False
            self._failure_reason = "audit_queue_overflow"

    def _enqueue_gap_when_room(self) -> None:
        if self._gap_dropped_count == 0 or self._gap_event_id is not None:
            return
        self._gap_sequence += 1
        value = self.factory.create(
            normalized_kind="audit_gap",
            occurred_at_ns=self._gap_started_at_ns,
            observed_at_ns=self._gap_last_observed_at_ns,
            summary={
                "cause": "audit_queue_overflow",
                "dropped_count": self._gap_dropped_count,
            },
            payload={
                "cause": "audit_queue_overflow",
                "dropped_count": self._gap_dropped_count,
                "gap_sequence": self._gap_sequence,
            },
            event_identity=(
                f"queue-overflow:{self._gap_sequence}:{self._gap_started_at_ns}:"
                f"{self._gap_last_observed_at_ns}:{self._gap_dropped_count}"
            ),
        )
        size = len(value.model_dump_json().encode())
        if len(self._values) >= self._max_count or self._bytes + size > self._max_bytes:
            return
        self._values.append((value, size))
        self._bytes += size
        self._gap_event_id = value.event_id
        self._gap_dropped_count = 0
        self._gap_started_at_ns = 0
        self._gap_last_observed_at_ns = 0

    def _enqueue_conflict_gap_when_room(self) -> None:
        if self._conflict_count == 0 or self._conflict_gap_event_id is not None:
            return
        self._conflict_sequence += 1
        value = self.factory.create(
            normalized_kind="audit_gap",
            occurred_at_ns=self._conflict_started_at_ns,
            observed_at_ns=self._conflict_last_observed_at_ns,
            summary={
                "cause": "audit_identity_conflict",
                "conflict_count": self._conflict_count,
            },
            payload={
                "cause": "audit_identity_conflict",
                "conflict_count": self._conflict_count,
                "conflict_sequence": self._conflict_sequence,
                "first_event_id": self._conflict_first_event_id,
            },
            event_identity=(
                f"identity-conflict:{self._conflict_sequence}:{self._conflict_first_event_id}:"
                f"{self._conflict_started_at_ns}:{self._conflict_last_observed_at_ns}:{self._conflict_count}"
            ),
        )
        size = len(value.model_dump_json().encode())
        if len(self._values) >= self._max_count or self._bytes + size > self._max_bytes:
            return
        self._values.append((value, size))
        self._bytes += size
        self._conflict_gap_event_id = value.event_id
        self._conflict_count = 0
        self._conflict_first_event_id = None
        self._conflict_started_at_ns = 0
        self._conflict_last_observed_at_ns = 0

    def _enqueue_rejected_gap_when_room(self) -> None:
        """Name what the ledger lost: how many, the first identity, and which kinds.

        `trading_execution_metadata_valid` allows 16 keys and 2048 bytes. Three fixed keys plus the
        ten-value `normalized_kind` vocabulary is 13 keys of short names and integers, so this summary
        cannot outgrow the CHECK that the gap record exists to report on.
        """

        if self._rejected_count == 0 or self._rejected_gap_event_id is not None:
            return
        self._rejected_sequence += 1
        kind_counts = dict(sorted(self._rejected_kind_counts.items()))
        summary: dict[str, str | int | bool] = {
            "cause": "audit_append_rejected",
            "dropped_count": self._rejected_count,
            "first_event_id": self._rejected_first_event_id or "",
        }
        summary.update((f"kind.{kind}", count) for kind, count in kind_counts.items())
        value = self.factory.create(
            normalized_kind="audit_gap",
            occurred_at_ns=self._rejected_started_at_ns,
            observed_at_ns=self._rejected_last_observed_at_ns,
            summary=summary,
            payload={
                "cause": "audit_append_rejected",
                "dropped_count": self._rejected_count,
                "first_event_id": self._rejected_first_event_id,
                "kind_counts": kind_counts,
                "gap_sequence": self._rejected_sequence,
            },
            event_identity=(
                f"append-rejected:{self._rejected_sequence}:{self._rejected_first_event_id}:"
                f"{self._rejected_started_at_ns}:{self._rejected_last_observed_at_ns}:{self._rejected_count}"
            ),
        )
        size = len(value.model_dump_json().encode())
        if len(self._values) >= self._max_count or self._bytes + size > self._max_bytes:
            return
        self._values.append((value, size))
        self._bytes += size
        self._rejected_gap_event_id = value.event_id
        self._rejected_count = 0
        self._rejected_first_event_id = None
        self._rejected_kind_counts = {}
        self._rejected_started_at_ns = 0
        self._rejected_last_observed_at_ns = 0


__all__ = [
    "AuditAppendRejected",
    "AuditSink",
    "ObservationFactory",
    "day_start_baseline_from_observation",
]
