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

from tracefold.trading import ExecutionObservationV1

from .risk import DayStartBaseline

_DEFAULT_MAX_COUNT = 1_024
_DEFAULT_MAX_BYTES = 4 * 1_048_576
_FLUSH_COUNT = 128
_FLUSH_BYTES = 1_048_576
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
        scaled = equity_usd * _EQUITY_SCALE
        if scaled != scaled.to_integral_value() or equity_usd <= 0:
            raise ValueError("oi_runtime_day_start_equity_precision_invalid")
        event_identity = f"day-start:{utc_day}"
        fixed_event_id = self.day_start_event_id(utc_day)
        observation = self.create(
            normalized_kind="risk",
            occurred_at_ns=recorded_at_ns,
            observed_at_ns=recorded_at_ns,
            summary={
                "risk_fact": "day_start_equity",
                "utc_day": utc_day,
                "equity_usd_micros": int(scaled),
            },
            payload={
                "risk_fact": "day_start_equity",
                "utc_day": utc_day,
                "equity_usd_micros": int(scaled),
            },
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
    micros = summary.get("equity_usd_micros")
    if not isinstance(utc_day, str) or type(micros) is not int:
        raise ValueError("oi_runtime_day_start_observation_invalid")
    return DayStartBaseline(
        utc_day=utc_day,
        equity_usd=Decimal(micros) / _EQUITY_SCALE,
        recorded_at_ns=observation.occurred_at_ns,
        event_id=observation.event_id,
    )


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
        with self._lock:
            self._enqueue_gap_when_room()
            self._enqueue_conflict_gap_when_room()
            batch: list[ExecutionObservationV1] = []
            batch_bytes = 0
            for value, size in self._values:
                if len(batch) >= _FLUSH_COUNT or batch_bytes + size > _FLUSH_BYTES:
                    break
                batch.append(value)
                batch_bytes += size
        if not batch:
            return ()
        try:
            writer(tuple(batch))
        except Exception:
            with self._lock:
                self._healthy = False
                self._failure_reason = "audit_append_failed"
            raise
        event_ids = tuple(value.event_id for value in batch)
        with self._lock:
            for event_id in event_ids:
                queued, size = self._values.popleft()
                if queued.event_id != event_id:
                    raise RuntimeError("oi_runtime_audit_queue_corrupted")
                self._bytes -= size
            if self._gap_event_id is not None and self._gap_event_id in event_ids:
                self._gap_event_id = None
            if self._conflict_gap_event_id is not None and self._conflict_gap_event_id in event_ids:
                self._conflict_gap_event_id = None
            self._enqueue_gap_when_room()
            self._enqueue_conflict_gap_when_room()
            if self._conflict_count > 0 or self._conflict_gap_event_id is not None:
                self._healthy = False
                self._failure_reason = "audit_identity_conflict"
            elif self._gap_dropped_count == 0 and self._gap_event_id is None:
                self._healthy = True
                self._failure_reason = None
            else:
                self._healthy = False
                self._failure_reason = "audit_queue_overflow"
        return tuple(batch)

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


__all__ = [
    "AuditSink",
    "ObservationFactory",
    "day_start_baseline_from_observation",
]
