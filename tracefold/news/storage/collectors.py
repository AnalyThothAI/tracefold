"""Typed collector state, serialized by one row lock and one UPDATE per mutation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from .sql_values import _dumps

RECOVERY_BACKLOG_LIMIT = 20


class CollectorState(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class OpenNewsState(CollectorState):
    connected: bool = False
    last_frame_at_ms: int | None = None
    last_publish_at_ms: int | None = None
    last_error_code: str | None = None
    broker_snapshot: dict[str, Any] = Field(default_factory=dict)
    next_incident_id: int = Field(default=1, ge=1)


class ChainTapeState(CollectorState):
    high_water_block: int = Field(default=0, ge=0)
    high_water_tx_index: int = Field(default=-1, ge=-1)
    roster_version: int = Field(default=0, ge=0)
    last_outcome: Literal["", "success", "partial", "error"] = ""
    last_error: str | None = None
    last_success_at_ms: int | None = Field(default=None, gt=0)
    ignored_inbound_total: int = Field(default=0, ge=0)
    unknown_total: int = Field(default=0, ge=0)
    noise_through_block: int = Field(default=0, ge=0)
    noise_through_tx_index: int = Field(default=-1, ge=-1)
    detection_cutover_at_ms: int = 0
    coverage_from_ms: int | None = None
    scanned_at_ms: int | None = None
    scanned_block: int | None = None
    scanned_log: int | None = None
    gap_at_ms: int | None = None
    next_attempt_at_ms: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    blocked_tx_hash: str | None = None
    enrichment_error: str | None = None


class WalletRosterState(CollectorState):
    last_attempt_at_ms: int | None = None
    last_success_at_ms: int | None = None
    last_error: str | None = None
    next_attempt_at_ms: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)


class InstrumentCatalogState(CollectorState):
    venues: dict[str, int] = Field(default_factory=dict)


class OpenNewsIncident(CollectorState):
    incident_id: int = Field(gt=0)
    cause_class: Literal[
        "planned_shutdown",
        "network_connect",
        "authentication",
        "provider_close",
        "protocol_error",
        "idle_timeout",
        "broker_backpressure",
        "broker_unavailable",
        "process_outage",
        "triage_circuit_open",
        "unknown",
    ]
    opened_at_ms: int = Field(ge=0)
    closed_at_ms: int | None = None
    planned: bool = False
    close_code: int | None = None
    recovery_status: Literal["pending", "recovered", "partial", "unavailable", "not_applicable"] = "pending"
    recovered_count: int = 0
    recovery_from_at_ms: int | None = None
    recovery_to_at_ms: int | None = None
    last_error_code: str | None = None
    updated_at_ms: int

    @model_validator(mode="after")
    def chronological(self) -> OpenNewsIncident:
        if self.closed_at_ms is not None and self.closed_at_ms < self.opened_at_ms:
            raise ValueError("news_incident_closed_before_opened")
        return self


class CollectorIncidents(RootModel[list[OpenNewsIncident]]):
    @model_validator(mode="after")
    def unique_identities(self) -> CollectorIncidents:
        ids = [item.incident_id for item in self.root]
        causes = [item.cause_class for item in self.root if item.closed_at_ms is None]
        if len(ids) != len(set(ids)) or len(causes) != len(set(causes)):
            raise ValueError("news_incidents_duplicate_identity_or_open_cause")
        return self

    def retain(self) -> None:
        active = [item for item in self.root if item.closed_at_ms is None or item.recovery_status == "pending"]
        settled = sorted(
            (item for item in self.root if item.closed_at_ms is not None and item.recovery_status != "pending"),
            key=lambda item: item.incident_id,
            reverse=True,
        )[:20]
        self.root = sorted([*active, *settled], key=lambda item: item.incident_id)


_INCIDENTS_SQL = """
    SELECT x.incident_id,x.cause_class,x.opened_at_ms,x.closed_at_ms,x.planned,x.close_code,
           x.recovery_status,x.recovered_count,x.recovery_from_at_ms,x.recovery_to_at_ms,
           x.last_error_code,x.updated_at_ms
    FROM news_collectors c CROSS JOIN LATERAL jsonb_to_recordset(c.incidents) AS x(
      incident_id bigint, cause_class text, opened_at_ms bigint, closed_at_ms bigint, planned boolean,
      close_code integer, recovery_status text, recovered_count integer, recovery_from_at_ms bigint,
      recovery_to_at_ms bigint, last_error_code text, updated_at_ms bigint)
    WHERE c.collector_id = 'opennews'
"""
OPEN_INCIDENTS_SQL = (
    "SELECT incident_id,cause_class,opened_at_ms,planned FROM (" + _INCIDENTS_SQL + ") incidents "  # noqa: S608
    "WHERE closed_at_ms IS NULL ORDER BY incident_id"
)
INGEST_LIVENESS_SQL = """
    SELECT (state->>'connected')::boolean AS connected, updated_at_ms
    FROM news_collectors WHERE collector_id='opennews'
"""
STATUS_INGEST_SQL = """
    SELECT (state->>'connected')::boolean AS connected,
           (state->>'last_frame_at_ms')::bigint AS last_frame_at_ms,
           (state->>'last_publish_at_ms')::bigint AS last_publish_at_ms,
           state->>'last_error_code' AS last_error_code, state->'broker_snapshot' AS broker_snapshot
    FROM news_collectors WHERE collector_id='opennews'
"""


def pending_recovery_incidents_statement(*, limit: int) -> tuple[str, tuple[int]]:
    return (
        "SELECT incident_id,cause_class,opened_at_ms,closed_at_ms,recovery_from_at_ms,recovery_to_at_ms,"  # noqa: S608
        "last_error_code,updated_at_ms FROM (" + _INCIDENTS_SQL + ") incidents "
        "WHERE recovery_status='pending' AND closed_at_ms IS NOT NULL ORDER BY incident_id LIMIT %s",
        (int(limit),),
    )


StateT = TypeVar("StateT", bound=CollectorState)


class CollectorsStorage:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def mutate_collector(
        self, collector_id: str, model: type[StateT], *, now_ms: int
    ) -> Iterator[tuple[StateT, CollectorIncidents]]:
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT state,incidents,updated_at_ms FROM news_collectors WHERE collector_id=%s FOR UPDATE",
                (collector_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"news_collector_missing:{collector_id}")
            state = model.model_validate(row["state"])
            incidents = CollectorIncidents.model_validate(row["incidents"])
            yield state, incidents
            state = model.model_validate(state.model_dump())
            incidents = CollectorIncidents.model_validate(incidents.model_dump())
            incidents.retain()
            if state.model_dump() == row["state"] and incidents.model_dump() == row["incidents"]:
                return
            self.conn.execute(
                "UPDATE news_collectors SET state=%s::jsonb,incidents=%s::jsonb,updated_at_ms=%s WHERE collector_id=%s",
                (
                    _dumps(state.model_dump()),
                    _dumps(incidents.model_dump()),
                    max(now_ms, int(row["updated_at_ms"])),
                    collector_id,
                ),
            )

    def update_ingest_state(
        self,
        *,
        now_ms: int,
        connected: bool | None = None,
        last_frame_at_ms: int | None = None,
        last_publish_at_ms: int | None = None,
        last_error_code: str | None = None,
        clear_error: bool = False,
    ) -> None:
        with self.mutate_collector("opennews", OpenNewsState, now_ms=now_ms) as (state, _):
            if connected is not None:
                state.connected = connected
            if last_frame_at_ms is not None:
                state.last_frame_at_ms = last_frame_at_ms
            if last_publish_at_ms is not None:
                state.last_publish_at_ms = last_publish_at_ms
            if clear_error or last_error_code is not None:
                state.last_error_code = None if clear_error else last_error_code

    def record_published_frame(self, *, now_ms: int) -> int:
        closed = 0
        with self.mutate_collector("opennews", OpenNewsState, now_ms=now_ms) as (state, incidents):
            for incident in incidents.root:
                if incident.closed_at_ms is None and incident.cause_class in {
                    "broker_backpressure",
                    "broker_unavailable",
                }:
                    incident.closed_at_ms = now_ms
                    incident.recovery_to_at_ms = incident.recovery_to_at_ms or now_ms
                    incident.recovery_status = "pending"
                    incident.updated_at_ms = now_ms
                    closed += 1
            if closed or state.last_publish_at_ms is None or now_ms - state.last_publish_at_ms >= 5_000:
                state.last_frame_at_ms = now_ms
                state.last_publish_at_ms = now_ms
                state.last_error_code = None
        return closed

    def ingest_liveness(self) -> dict[str, Any] | None:
        row = self.conn.execute(INGEST_LIVENESS_SQL).fetchone()
        return None if row is None else dict(row)

    def update_broker_snapshot(self, *, snapshot: Mapping[str, Any], now_ms: int) -> None:
        with self.mutate_collector("opennews", OpenNewsState, now_ms=now_ms) as (state, _):
            state.broker_snapshot = {**snapshot, "observed_at_ms": now_ms}

    def open_incident(
        self, *, cause_class: str, now_ms: int, planned: bool = False, close_code: int | None = None
    ) -> int:
        with self.mutate_collector("opennews", OpenNewsState, now_ms=now_ms) as (state, incidents):
            existing = next(
                (i for i in incidents.root if i.cause_class == cause_class and i.closed_at_ms is None), None
            )
            if existing is not None:
                existing.updated_at_ms = max(now_ms, existing.updated_at_ms)
                return existing.incident_id
            incident_id = state.next_incident_id
            state.next_incident_id += 1
            incidents.root.append(
                OpenNewsIncident(
                    incident_id=incident_id,
                    cause_class=cause_class,
                    opened_at_ms=now_ms,
                    updated_at_ms=now_ms,
                    planned=planned,
                    close_code=close_code,
                    recovery_status="not_applicable" if cause_class == "triage_circuit_open" else "pending",
                )
            )
            return incident_id

    def close_open_incidents(self, *, cause_classes: Sequence[str] | None, now_ms: int) -> int:
        count = 0
        with self.mutate_collector("opennews", OpenNewsState, now_ms=now_ms) as (_, incidents):
            for incident in incidents.root:
                if incident.closed_at_ms is None and (cause_classes is None or incident.cause_class in cause_classes):
                    incident.closed_at_ms = now_ms
                    if incident.recovery_to_at_ms is None:
                        incident.recovery_to_at_ms = now_ms
                    if incident.cause_class in {"broker_backpressure", "broker_unavailable"}:
                        incident.recovery_status = "pending"
                    incident.updated_at_ms = now_ms
                    count += 1
        return count

    def pending_recovery_incidents(self, *, limit: int = 20) -> list[dict[str, Any]]:
        sql, params = pending_recovery_incidents_statement(limit=limit)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def recovery_backlog(self) -> dict[str, Any]:
        rows = self.pending_recovery_incidents(limit=RECOVERY_BACKLOG_LIMIT)
        latest = max(
            (row for row in rows if row["last_error_code"] is not None),
            key=lambda row: (row["updated_at_ms"], row["incident_id"]),
            default=None,
        )
        error = None if latest is None else latest["last_error_code"]
        return {
            "pending_count": len(rows),
            "oldest_opened_at_ms": min((row["opened_at_ms"] for row in rows), default=None),
            "last_error_code": error,
            "reason": "recovery_transient" if rows and error else "recovery_pending" if rows else None,
        }

    def open_incident_summary(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[int]] = defaultdict(list)
        for row in self.open_incidents():
            grouped[row["cause_class"]].append(row["opened_at_ms"])
        return [
            {"cause_class": key, "count": len(values), "oldest_opened_at_ms": min(values)}
            for key, values in sorted(grouped.items())
        ]

    def record_recovery_error(self, *, incident_id: int, error_code: str, now_ms: int) -> bool:
        with self.mutate_collector("opennews", OpenNewsState, now_ms=now_ms) as (_, incidents):
            for incident in incidents.root:
                if incident.incident_id == incident_id and incident.recovery_status == "pending":
                    incident.last_error_code = str(error_code)[:200]
                    incident.updated_at_ms = now_ms
                    return True
        return False

    def complete_recovery(
        self,
        *,
        incident_id: int,
        status: str,
        recovered_count: int,
        error_code: str | None,
        recovery_from_at_ms: int | None,
        recovery_to_at_ms: int | None,
        now_ms: int,
    ) -> bool:
        with self.mutate_collector("opennews", OpenNewsState, now_ms=now_ms) as (_, incidents):
            for incident in incidents.root:
                if incident.incident_id == incident_id and incident.recovery_status == "pending":
                    incident.recovery_status = status  # type: ignore[assignment]
                    incident.recovered_count += recovered_count
                    incident.last_error_code = error_code
                    if recovery_from_at_ms is not None:
                        incident.recovery_from_at_ms = recovery_from_at_ms
                    if recovery_to_at_ms is not None:
                        incident.recovery_to_at_ms = recovery_to_at_ms
                    incident.updated_at_ms = now_ms
                    return True
        return False

    def open_incidents(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(OPEN_INCIDENTS_SQL).fetchall()]
