from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from tracefold.app.worker_manifest import worker_names

_STALE_AFTER_MS = 15_000


class WorkerRuntimeStatusRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def publish(
        self,
        *,
        runtime_id: str,
        runtime_version: str,
        statuses: Mapping[str, Mapping[str, Any]],
        now_ms: int,
    ) -> None:
        runtime_uuid = UUID(str(runtime_id))
        canonical_names = worker_names()
        if set(statuses) != set(canonical_names):
            raise ValueError("worker_runtime_status_manifest_mismatch")
        for unit_name in canonical_names:
            status = statuses[unit_name]
            effective_status = str(status["effective_status"])
            if effective_status == "intentionally_not_started":
                effective_status = "disabled"
            self.conn.execute(
                """
                INSERT INTO worker_runtime_status(
                  unit_name, runtime_id, runtime_version, effective_status,
                  heartbeat_at_ms, last_started_at_ms, last_finished_at_ms,
                  last_result_json, last_error, deadline_at_ms, queue_depth,
                  oldest_due_at_ms, quarantine_count, updated_at_ms
                )
                VALUES (
                  %(unit_name)s, %(runtime_id)s, %(runtime_version)s, %(effective_status)s,
                  %(now_ms)s, %(last_started_at_ms)s, %(last_finished_at_ms)s,
                  %(last_result_json)s, %(last_error)s, %(deadline_at_ms)s,
                  %(queue_depth)s, %(oldest_due_at_ms)s, %(quarantine_count)s,
                  %(now_ms)s
                )
                ON CONFLICT(unit_name) DO UPDATE SET
                  runtime_id = excluded.runtime_id,
                  runtime_version = excluded.runtime_version,
                  effective_status = excluded.effective_status,
                  heartbeat_at_ms = excluded.heartbeat_at_ms,
                  last_started_at_ms = excluded.last_started_at_ms,
                  last_finished_at_ms = excluded.last_finished_at_ms,
                  last_result_json = excluded.last_result_json,
                  last_error = excluded.last_error,
                  deadline_at_ms = excluded.deadline_at_ms,
                  queue_depth = excluded.queue_depth,
                  oldest_due_at_ms = excluded.oldest_due_at_ms,
                  quarantine_count = excluded.quarantine_count,
                  updated_at_ms = excluded.updated_at_ms
                """,
                {
                    "unit_name": unit_name,
                    "runtime_id": runtime_uuid,
                    "runtime_version": str(runtime_version),
                    "effective_status": effective_status,
                    "now_ms": int(now_ms),
                    "last_started_at_ms": status.get("last_started_at_ms"),
                    "last_finished_at_ms": status.get("last_finished_at_ms"),
                    "last_result_json": (
                        Jsonb(status["last_result"]) if status.get("last_result") is not None else None
                    ),
                    "last_error": status.get("last_error"),
                    "deadline_at_ms": status.get("deadline_at_ms"),
                    "queue_depth": status.get("queue_depth"),
                    "oldest_due_at_ms": status.get("oldest_due_at_ms"),
                    "quarantine_count": int(status.get("quarantine_count") or 0),
                },
            )

    def read_current(self, *, now_ms: int, stale_after_ms: int = _STALE_AFTER_MS) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              unit_name, runtime_id::text AS runtime_id, runtime_version,
              effective_status, heartbeat_at_ms, last_started_at_ms,
              last_finished_at_ms, last_result_json, last_error,
              deadline_at_ms, queue_depth, oldest_due_at_ms, quarantine_count
            FROM worker_runtime_status
            ORDER BY unit_name
            """
        ).fetchall()
        by_name = {str(row["unit_name"]): row for row in rows}
        return {
            name: _serving_status(
                by_name.get(name),
                now_ms=int(now_ms),
                stale_after_ms=int(stale_after_ms),
            )
            for name in worker_names()
        }


def _serving_status(
    row: Mapping[str, Any] | None,
    *,
    now_ms: int,
    stale_after_ms: int,
) -> dict[str, Any]:
    if row is None:
        return _unavailable_status("worker_status_missing")
    stale = now_ms - int(row["heartbeat_at_ms"]) > stale_after_ms
    if stale:
        return _unavailable_status(
            "worker_status_stale",
            runtime_id=str(row["runtime_id"]),
            runtime_version=str(row["runtime_version"]),
            heartbeat_at_ms=int(row["heartbeat_at_ms"]),
            last_started_at_ms=row.get("last_started_at_ms"),
            last_finished_at_ms=row.get("last_finished_at_ms"),
            last_result=row.get("last_result_json"),
            last_error=row.get("last_error"),
            deadline_at_ms=row.get("deadline_at_ms"),
            queue_depth=row.get("queue_depth"),
            oldest_due_at_ms=row.get("oldest_due_at_ms"),
            quarantine_count=int(row.get("quarantine_count") or 0),
        )
    effective_status = str(row["effective_status"])
    return {
        "enabled": effective_status != "disabled",
        "running": effective_status == "running",
        "effective_status": effective_status,
        "unavailable_reason": None,
        "runtime_id": str(row["runtime_id"]),
        "runtime_version": str(row["runtime_version"]),
        "heartbeat_at_ms": int(row["heartbeat_at_ms"]),
        "last_started_at_ms": row.get("last_started_at_ms"),
        "last_finished_at_ms": row.get("last_finished_at_ms"),
        "last_result": row.get("last_result_json"),
        "last_error": row.get("last_error"),
        "iteration_duration_p99_ms": None,
        "deadline_at_ms": row.get("deadline_at_ms"),
        "queue_depth": row.get("queue_depth"),
        "oldest_due_at_ms": row.get("oldest_due_at_ms"),
        "quarantine_count": int(row.get("quarantine_count") or 0),
    }


def _unavailable_status(
    reason: str,
    *,
    runtime_id: str | None = None,
    runtime_version: str | None = None,
    heartbeat_at_ms: int | None = None,
    last_started_at_ms: int | None = None,
    last_finished_at_ms: int | None = None,
    last_result: dict[str, Any] | None = None,
    last_error: str | None = None,
    deadline_at_ms: int | None = None,
    queue_depth: int | None = None,
    oldest_due_at_ms: int | None = None,
    quarantine_count: int = 0,
) -> dict[str, Any]:
    return {
        "enabled": True,
        "running": False,
        "effective_status": "unavailable",
        "unavailable_reason": reason,
        "runtime_id": runtime_id,
        "runtime_version": runtime_version,
        "heartbeat_at_ms": heartbeat_at_ms,
        "last_started_at_ms": last_started_at_ms,
        "last_finished_at_ms": last_finished_at_ms,
        "last_result": last_result,
        "last_error": last_error,
        "iteration_duration_p99_ms": None,
        "deadline_at_ms": deadline_at_ms,
        "queue_depth": queue_depth,
        "oldest_due_at_ms": oldest_due_at_ms,
        "quarantine_count": int(quarantine_count),
    }


def unavailable_worker_statuses(reason: str) -> dict[str, dict[str, Any]]:
    return {name: _unavailable_status(reason) for name in worker_names()}


__all__ = [
    "WorkerRuntimeStatusRepository",
    "unavailable_worker_statuses",
]
