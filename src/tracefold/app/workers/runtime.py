from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast
from uuid import UUID

from tracefold.platform.postgres.audit import ReadQuerySpec

WORKERS_RUNTIME_STALE_AFTER_MS = 15_000
WORKERS_RUNTIME_VERSION = "2"

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
    "runtime_invariant_failed",
    "resource_operation_overrun",
    "graceful_deadline_exceeded",
    "cleanup_failed",
]

_LIFECYCLE_STATES = frozenset({"starting", "running", "stopping", "stopped", "failed"})
_FATAL_CODES = frozenset(
    {
        "startup_failed",
        "child_failed",
        "control_failed",
        "singleton_lost",
        "runtime_invariant_failed",
        "resource_operation_overrun",
        "graceful_deadline_exceeded",
        "cleanup_failed",
    }
)


class WorkersRuntimeRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def begin(
        self,
        *,
        runtime_id: str,
        runtime_version: str,
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
              started_at_ms, heartbeat_at_ms, fatal_code
            )
            VALUES (true, %s, %s, 'starting', %s, %s, NULL)
            ON CONFLICT(singleton_key) DO UPDATE SET
              runtime_id = excluded.runtime_id,
              runtime_version = excluded.runtime_version,
              lifecycle_state = excluded.lifecycle_state,
              started_at_ms = excluded.started_at_ms,
              heartbeat_at_ms = excluded.heartbeat_at_ms,
              fatal_code = NULL
            """,
            (
                runtime_uuid,
                _required_text(runtime_version, "runtime_version"),
                int(started_at_ms),
                int(now_ms),
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
        state = str(lifecycle_state)
        if state not in _LIFECYCLE_STATES:
            raise ValueError("workers_runtime_lifecycle_state_invalid")
        if state == "failed":
            if fatal_code not in _FATAL_CODES:
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
            (state, int(now_ms), fatal_code, UUID(str(runtime_id))),
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
                   lifecycle_state, started_at_ms, heartbeat_at_ms, fatal_code
              FROM workers_runtime
             WHERE singleton_key
        """,
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
    reason: str | None
    if stale:
        state = "stale"
        reason = "runtime_heartbeat_stale"
    else:
        state = lifecycle
        reason = {
            "starting": "runtime_starting",
            "running": None,
            "stopping": "runtime_stopping",
            "stopped": "runtime_stopped",
            "failed": "runtime_failed",
        }[lifecycle]
    return {
        "runtime_id": str(row["runtime_id"]),
        "runtime_version": str(row["runtime_version"]),
        "state": state,
        "started_at_ms": int(row["started_at_ms"]),
        "heartbeat_at_ms": heartbeat_at_ms,
        "heartbeat_stale_after_ms": WORKERS_RUNTIME_STALE_AFTER_MS,
        "fatal_code": cast(str | None, row.get("fatal_code")),
        "unavailable_reason": reason,
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
    }


def _required_text(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"workers_runtime_{field}_required")
    return normalized


__all__ = [
    "WORKERS_RUNTIME_STALE_AFTER_MS",
    "WORKERS_RUNTIME_VERSION",
    "FatalCode",
    "LifecycleState",
    "WorkersRuntimeRepository",
    "workers_runtime_read_query",
    "workers_runtime_status",
]
