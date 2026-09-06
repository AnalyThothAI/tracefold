from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout

from tracefold.platform.postgres.audit import INDEXED_ROW_SCAN_BUDGET, ReadQuerySpec
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

WORKERS_RUNTIME_STALE_AFTER_MS = 15_000
WORKERS_RUNTIME_VERSION = "2"

# One capability: what an operator loses when it faults, and the unit a status reader reports on.
# They live beside the runtime row because they are published through it, and because a status route
# must be able to name one without importing the Trading or News composition that runs it.
NEWS_INGESTION = "news_ingestion"
NEWS_EDITORIAL = "news_editorial"
NEWS_DELIVERY = "news_delivery"
NEWS_INSTRUMENTS = "news_instruments"
NEWS_QUOTES = "news_quotes"
NEWS_REACTIONS = "news_reactions"
# The market notification loop's own key. Named for what it does rather than for the package that
# owns it, because that is what an operator reading `/api/status` is looking for: market alerts are a
# capability of the product, not of News's internal layout (#553 PR-2).
MARKET_NOTIFICATIONS = "market_notifications"
# The Robinhood Chain wallet tape's own key (#572 PR-1). Named for the stream an operator loses when it
# faults -- the followed wallets' on-chain fills -- rather than for the provider behind it, because a
# second chain or a second roster site would not be a second capability.
CHAIN_TAPE = "chain_tape"
TRADING_SIGNAL_LANE = "trading_signal_lane"

CapabilityStateName = Literal["running", "faulted", "unavailable", "disabled"]

# What a capability may never confine. Each of these says the shared PostgreSQL layer or a shared
# native permit failed -- not that one business capability's program is wrong -- so recording it as a
# capability fault would hide a process-wide fault behind a green readiness. They stay root fatal,
# which is also the shared DB layer's existing behavior for an error it did not retry.
SHARED_RESOURCE_FAILURES: tuple[type[BaseException], ...] = (
    psycopg.Error,
    PoolTimeout,
    ResourceAdmissionTimeout,
    ResourceOperationOverrun,
)

LifecycleState = Literal[
    "starting",
    "running",
    "stopping",
    "stopped",
    "failed",
]
FatalCode = Literal[
    "startup_failed",
    "child_failed",
    "control_failed",
    "singleton_lost",
    "resource_operation_overrun",
    "graceful_deadline_exceeded",
    "cleanup_failed",
]


@dataclass(frozen=True, slots=True)
class CapabilityState:
    state: CapabilityStateName
    reason: str | None = None


class CapabilityStates:
    """What each Workers capability is doing, kept apart from basic process readiness.

    `ready` answers "does this process still own PostgreSQL and its singleton"; this answers "which
    business capabilities are actually working". Folding the two together is what let one faulted
    lane switch off healthy fact APIs, so they stay two questions with two answers (#553 §6).
    """

    __slots__ = ("_states",)

    def __init__(self) -> None:
        self._states: dict[str, CapabilityState] = {}

    def declare(self, capability: str, state: CapabilityStateName, *, reason: str | None = None) -> None:
        self._states[capability] = CapabilityState(state=state, reason=reason)

    def running(self, capability: str) -> None:
        self.declare(capability, "running")

    def faulted(self, capability: str, reason: str) -> None:
        self.declare(capability, "faulted", reason=reason)

    def unavailable(self, capability: str, reason: str) -> None:
        self.declare(capability, "unavailable", reason=reason)

    def disabled(self, capability: str, reason: str) -> None:
        self.declare(capability, "disabled", reason=reason)

    def payload(self) -> dict[str, dict[str, Any]]:
        return {
            name: {"state": current.state, "reason": current.reason} for name, current in sorted(self._states.items())
        }


class WorkersRuntimeRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def begin(
        self,
        *,
        runtime_id: str,
        runtime_version: str,
        runtime_revision: str,
        image_digest: str,
        started_at_ms: int,
        now_ms: int,
    ) -> bool:
        runtime_uuid = UUID(str(runtime_id))
        row = self.conn.execute("SELECT * FROM workers_runtime WHERE singleton_key FOR UPDATE").fetchone()
        if row is not None and not _takeover_allowed(row, now_ms=now_ms):
            return False
        self.conn.execute(
            """
            INSERT INTO workers_runtime(
              singleton_key, runtime_id, runtime_version, lifecycle_state,
              started_at_ms, heartbeat_at_ms, fatal_code, runtime_revision, image_digest,
              capabilities
            )
            VALUES (true, %s, %s, 'starting', %s, %s, NULL, %s, %s, '{}'::jsonb)
            ON CONFLICT(singleton_key) DO UPDATE SET
              runtime_id = excluded.runtime_id,
              runtime_version = excluded.runtime_version,
              lifecycle_state = excluded.lifecycle_state,
              started_at_ms = excluded.started_at_ms,
              heartbeat_at_ms = excluded.heartbeat_at_ms,
              fatal_code = NULL,
              runtime_revision = excluded.runtime_revision,
              image_digest = excluded.image_digest,
              -- A new runtime inherits no capability report: the previous process's faults describe
              -- a process that is gone, and carrying them forward would report a fault nobody owns.
              capabilities = '{}'::jsonb
            """,
            (
                runtime_uuid,
                _required_text(runtime_version, "runtime_version"),
                int(started_at_ms),
                int(now_ms),
                _required_text(runtime_revision, "runtime_revision"),
                _required_text(image_digest, "image_digest"),
            ),
        )
        return True

    def transition(
        self,
        *,
        runtime_id: str,
        lifecycle_state: LifecycleState,
        now_ms: int,
        fatal_code: FatalCode | None = None,
    ) -> None:
        # `LifecycleState`/`FatalCode` are the compile-time vocabulary and
        # `workers_runtime_lifecycle_state_check` / `workers_runtime_fatal_code_check` are the
        # persisted one; re-listing either here only lets the two copies drift (#589 P-F13). The
        # pairing rule below is the one claim about *two* arguments that a Literal cannot make.
        if lifecycle_state == "failed":
            if fatal_code is None:
                raise ValueError("workers_runtime_fatal_code_required")
        elif fatal_code is not None:
            raise ValueError("workers_runtime_fatal_code_forbidden")
        cursor = self.conn.execute(
            """
            UPDATE workers_runtime
               SET lifecycle_state = %s,
                   heartbeat_at_ms = GREATEST(heartbeat_at_ms, %s),
                   fatal_code = %s
             WHERE singleton_key
               AND runtime_id = %s
            """,
            (lifecycle_state, int(now_ms), fatal_code, UUID(str(runtime_id))),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("workers_runtime_identity_lost")

    def set_capabilities(self, *, runtime_id: str, capabilities: Mapping[str, Any]) -> None:
        """Publish what each business capability of this runtime is doing, for the status readers.

        This writes no heartbeat. A capability report is not liveness evidence -- a process can
        publish one while its control loop is already dead -- so staleness stays the control
        heartbeat's alone.
        """

        cursor = self.conn.execute(
            """
            UPDATE workers_runtime
               SET capabilities = %s::jsonb
             WHERE singleton_key
               AND runtime_id = %s
            """,
            (Jsonb(dict(capabilities)), UUID(str(runtime_id))),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("workers_runtime_identity_lost")

    def heartbeat(self, *, runtime_id: str, now_ms: int) -> None:
        cursor = self.conn.execute(
            """
            UPDATE workers_runtime
               SET heartbeat_at_ms = GREATEST(heartbeat_at_ms, %s)
             WHERE singleton_key
               AND runtime_id = %s
            """,
            (int(now_ms), UUID(str(runtime_id))),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("workers_runtime_identity_lost")

    def read(self) -> dict[str, Any] | None:
        query = workers_runtime_read_query()
        row = self.conn.execute(query.sql, query.params).fetchone()
        return dict(row) if row is not None else None


def workers_runtime_read_query() -> ReadQuerySpec:
    return ReadQuerySpec(
        name="workers_runtime",
        sql="""
            SELECT runtime_id::text AS runtime_id, runtime_version,
                   lifecycle_state, started_at_ms, heartbeat_at_ms, fatal_code,
                   runtime_revision, image_digest, capabilities
              FROM workers_runtime
             WHERE singleton_key
        """,
        max_read_return_amplification=4.0,
        max_scanned_rows=INDEXED_ROW_SCAN_BUDGET,
    )


def workers_runtime_status(
    row: Mapping[str, Any] | None,
    *,
    now_ms: int,
    query_failed: bool = False,
) -> dict[str, Any]:
    if query_failed:
        return _unavailable_runtime("runtime_status_query_failed")
    if row is None:
        return _unavailable_runtime("runtime_missing")
    lifecycle = str(row["lifecycle_state"])
    heartbeat_at_ms = int(row["heartbeat_at_ms"])
    stale = (
        lifecycle in {"starting", "running", "stopping"}
        and int(now_ms) - heartbeat_at_ms > WORKERS_RUNTIME_STALE_AFTER_MS
    )
    state = "stale" if stale else lifecycle
    reason = (
        "runtime_heartbeat_stale"
        if stale
        else {
            "starting": "runtime_starting",
            "running": None,
            "stopping": "runtime_stopping",
            "stopped": "runtime_stopped",
            "failed": "runtime_failed",
        }[lifecycle]
    )
    return {
        "runtime_id": str(row["runtime_id"]),
        "runtime_version": str(row["runtime_version"]),
        "state": state,
        "started_at_ms": int(row["started_at_ms"]),
        "heartbeat_at_ms": heartbeat_at_ms,
        "heartbeat_stale_after_ms": WORKERS_RUNTIME_STALE_AFTER_MS,
        "fatal_code": cast(str | None, row.get("fatal_code")),
        "unavailable_reason": reason,
        # A stale row describes a process that stopped answering, so its last report is not evidence
        # of anything now: a SIGKILLed Workers would otherwise leave a lane reading `running` in
        # PostgreSQL forever. Terminal rows keep their report -- it says what died with the process.
        "capabilities": {} if stale else _capabilities(row.get("capabilities")),
    }


def _takeover_allowed(row: Mapping[str, Any], *, now_ms: int) -> bool:
    if str(row["lifecycle_state"]) in {"stopped", "failed"}:
        return True
    return int(now_ms) - int(row["heartbeat_at_ms"]) > WORKERS_RUNTIME_STALE_AFTER_MS


def _unavailable_runtime(reason: str) -> dict[str, Any]:
    return {
        "runtime_id": None,
        "runtime_version": None,
        "state": "unavailable",
        "started_at_ms": None,
        "heartbeat_at_ms": None,
        "heartbeat_stale_after_ms": WORKERS_RUNTIME_STALE_AFTER_MS,
        "fatal_code": None,
        "unavailable_reason": reason,
        "capabilities": {},
    }


def _capabilities(value: object) -> dict[str, dict[str, Any]]:
    """Read back one runtime's capability report. An unreadable report is reported as absent."""

    raw = json.loads(value) if isinstance(value, (str, bytes)) else value
    if not isinstance(raw, Mapping):
        return {}
    report: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if not isinstance(entry, Mapping) or not isinstance(entry.get("state"), str):
            continue
        reason = entry.get("reason")
        report[str(name)] = {
            "state": str(entry["state"]),
            "reason": str(reason) if isinstance(reason, str) else None,
        }
    return report


def _required_text(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"workers_runtime_{field}_required")
    return normalized


__all__ = [
    "MARKET_NOTIFICATIONS",
    "NEWS_DELIVERY",
    "NEWS_EDITORIAL",
    "NEWS_INGESTION",
    "NEWS_INSTRUMENTS",
    "NEWS_QUOTES",
    "NEWS_REACTIONS",
    "SHARED_RESOURCE_FAILURES",
    "TRADING_SIGNAL_LANE",
    "WORKERS_RUNTIME_STALE_AFTER_MS",
    "WORKERS_RUNTIME_VERSION",
    "CapabilityState",
    "CapabilityStates",
    "FatalCode",
    "LifecycleState",
    "WorkersRuntimeRepository",
    "workers_runtime_read_query",
    "workers_runtime_status",
]
