from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StatusQueueSpec:
    table: str
    active_statuses: tuple[str, ...]
    due_statuses: tuple[str, ...]
    terminal_statuses: tuple[str, ...]
    due_column: str = "next_run_at_ms"
    running_statuses: tuple[str, ...] = ()
    failed_statuses: tuple[str, ...] = ()
    running_age_column: str = "updated_at_ms"


STATUS_QUEUE_SPECS = {
    "notification_deliveries": StatusQueueSpec(
        table="notification_deliveries",
        active_statuses=("pending", "failed", "running"),
        due_statuses=("pending", "failed"),
        running_statuses=("running",),
        failed_statuses=("failed",),
        terminal_statuses=("dead",),
    ),
    "event_anchor_backfill_jobs": StatusQueueSpec(
        table="event_anchor_backfill_jobs",
        active_statuses=("pending",),
        due_statuses=("pending",),
        terminal_statuses=("failed", "expired"),
    ),
    "news_story_analysis_attempts": StatusQueueSpec(
        table="news_story_analysis_attempts",
        active_statuses=("running", "failed"),
        due_statuses=("failed",),
        running_statuses=("running",),
        failed_statuses=("failed",),
        terminal_statuses=("available",),
        due_column="next_attempt_at_ms",
    ),
}

DIRTY_TARGET_TABLES = frozenset(
    {
        "asset_profile_refresh_targets",
        "token_discovery_dirty_lookup_keys",
        "token_image_source_dirty_targets",
        "token_profile_current_dirty_targets",
        "token_radar_dirty_targets",
    }
)

DIRTY_TARGET_WORKER_FILTERS: dict[tuple[str, str], tuple[str, str]] = {}

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def fetch_queue_table_health(
    conn: Any,
    table: str,
    *,
    now_ms: int,
    worker_name: str | None = None,
) -> dict[str, Any]:
    """Read one queue on demand for the authenticated ops CLI."""
    _validate_identifier(table)
    spec = STATUS_QUEUE_SPECS.get(table)
    if spec is not None:
        return _status_queue_health(conn, spec, now_ms=now_ms, worker_name=worker_name)
    if table in DIRTY_TARGET_TABLES:
        return _dirty_target_queue_health(conn, table, now_ms=now_ms, worker_name=worker_name)
    return _unavailable_health(table, "unknown", "unknown_queue_table", None)


def _status_queue_health(
    conn: Any,
    spec: StatusQueueSpec,
    *,
    now_ms: int,
    worker_name: str | None,
) -> dict[str, Any]:
    table = _validate_identifier(spec.table)
    try:
        counts = _status_counts(conn, spec)
        running_filter = _status_filter("status", spec.running_statuses)
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total_count,
              COUNT(*) FILTER (WHERE {_status_filter("status", spec.active_statuses)}) AS active_count,
              COUNT(*) FILTER (
                WHERE {_status_filter("status", spec.due_statuses)}
                  AND {_validate_identifier(spec.due_column)} <= %(now_ms)s
              ) AS due_count,
              COUNT(*) FILTER (WHERE {running_filter}) AS running_count,
              COUNT(*) FILTER (WHERE {_status_filter("status", spec.failed_statuses)}) AS failed_count,
              COUNT(*) FILTER (WHERE {_status_filter("status", spec.terminal_statuses)}) AS source_terminal_count,
              MIN({_validate_identifier(spec.due_column)}) FILTER (
                WHERE {_status_filter("status", spec.due_statuses)}
                  AND {_validate_identifier(spec.due_column)} <= %(now_ms)s
              ) AS oldest_due_at_ms,
              MIN({_validate_identifier(spec.running_age_column)}) FILTER (
                WHERE {running_filter}
              ) AS oldest_running_at_ms,
              MAX(attempt_count) AS max_attempt_count
            FROM {table}
            WHERE {_status_filter("status", _all_statuses(spec))}
            """,
            {"now_ms": int(now_ms)},
        ).fetchone()
        return _table_health(
            table=table,
            kind="status_queue",
            counts_by_status=counts,
            metrics=_row_dict(row),
            terminal_metrics=_terminal_metrics(conn, table, worker_name=worker_name),
            now_ms=now_ms,
        )
    except Exception as exc:
        return _unavailable_health(table, "status_queue", "queue_query_failed", exc)


def _dirty_target_queue_health(
    conn: Any,
    table: str,
    *,
    now_ms: int,
    worker_name: str | None,
) -> dict[str, Any]:
    table = _validate_identifier(table)
    try:
        worker_filter, worker_params = _worker_filter(table, worker_name)
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total_count,
              COUNT(*) AS active_count,
              COUNT(*) FILTER (
                WHERE due_at_ms <= %(now_ms)s
                  AND (leased_until_ms IS NULL OR leased_until_ms <= %(now_ms)s)
              ) AS due_count,
              COUNT(*) FILTER (WHERE leased_until_ms > %(now_ms)s) AS running_count,
              COUNT(*) FILTER (WHERE last_error IS NOT NULL AND last_error <> '') AS failed_count,
              0 AS source_terminal_count,
              MIN(due_at_ms) FILTER (
                WHERE due_at_ms <= %(now_ms)s
                  AND (leased_until_ms IS NULL OR leased_until_ms <= %(now_ms)s)
              ) AS oldest_due_at_ms,
              MIN(updated_at_ms) FILTER (WHERE leased_until_ms > %(now_ms)s) AS oldest_running_at_ms,
              MAX(attempt_count) AS max_attempt_count
            FROM {table}
            WHERE (
              due_at_ms <= %(now_ms)s
              OR leased_until_ms > %(now_ms)s
              OR (last_error IS NOT NULL AND last_error <> '')
            )
            {worker_filter}
            """,
            {"now_ms": int(now_ms), **worker_params},
        ).fetchone()
        return _table_health(
            table=table,
            kind="dirty_target",
            counts_by_status={},
            metrics=_row_dict(row),
            terminal_metrics=_terminal_metrics(conn, table, worker_name=worker_name),
            now_ms=now_ms,
        )
    except Exception as exc:
        return _unavailable_health(table, "dirty_target", "queue_query_failed", exc)


def _status_counts(conn: Any, spec: StatusQueueSpec) -> dict[str, int]:
    rows = conn.execute(
        f"""
        SELECT status, COUNT(*) AS count
        FROM {_validate_identifier(spec.table)}
        WHERE {_status_filter("status", _all_statuses(spec))}
        GROUP BY status
        """
    ).fetchall()
    return {str(_row_dict(row)["status"]): int(_row_dict(row)["count"]) for row in rows}


def _terminal_metrics(conn: Any, source_table: str, *, worker_name: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"source_table": source_table}
    worker_filter = ""
    if worker_name is not None:
        params["worker_name"] = worker_name
        worker_filter = "AND worker_name = %(worker_name)s"
    metrics = _row_dict(
        conn.execute(
            f"""
            SELECT
              COUNT(*) AS terminal_count,
              COUNT(*) FILTER (WHERE operator_action IS NULL) AS unresolved_terminal_count
            FROM worker_queue_terminal_events
            WHERE source_table = %(source_table)s
              {worker_filter}
            """,
            params,
        ).fetchone()
    )
    rows = conn.execute(
        f"""
        SELECT final_reason_bucket, COUNT(*) AS count
        FROM worker_queue_terminal_events
        WHERE source_table = %(source_table)s
          AND operator_action IS NULL
          {worker_filter}
        GROUP BY final_reason_bucket
        ORDER BY count DESC, final_reason_bucket ASC
        """,
        params,
    ).fetchall()
    metrics["reason_buckets"] = {
        str(_row_dict(row).get("final_reason_bucket") or "other"): int(_row_dict(row)["count"]) for row in rows
    }
    return metrics


def _table_health(
    *,
    table: str,
    kind: str,
    counts_by_status: dict[str, int],
    metrics: Mapping[str, Any],
    terminal_metrics: Mapping[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    queue_depth = _required_count(metrics, "active_count")
    due_count = _required_count(metrics, "due_count")
    running_count = _required_count(metrics, "running_count")
    failed_count = _required_count(metrics, "failed_count")
    source_terminal_count = _required_count(metrics, "source_terminal_count")
    terminal_count = _required_count(terminal_metrics, "terminal_count")
    unresolved_terminal_count = _required_count(terminal_metrics, "unresolved_terminal_count")
    blocked_count = source_terminal_count + unresolved_terminal_count
    status = _status(
        queue_depth=queue_depth,
        due_count=due_count,
        running_count=running_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
    )
    return {
        "table": table,
        "kind": kind,
        "available": True,
        "status": status,
        "reason": _reason(status, due_count=due_count),
        "counts_by_status": counts_by_status,
        "total_count": _required_count(metrics, "total_count"),
        "queue_depth": queue_depth,
        "due_count": due_count,
        "running_count": running_count,
        "failed_count": failed_count,
        "blocked_count": blocked_count,
        "source_terminal_count": source_terminal_count,
        "terminal_count": terminal_count,
        "unresolved_terminal_count": unresolved_terminal_count,
        "reason_buckets": dict(terminal_metrics.get("reason_buckets") or {}),
        "oldest_due_age_ms": _age_ms(now_ms, metrics.get("oldest_due_at_ms")),
        "oldest_running_age_ms": _age_ms(now_ms, metrics.get("oldest_running_at_ms")),
        "max_attempt_count": _optional_count(metrics, "max_attempt_count"),
    }


def _unavailable_health(table: str, kind: str, error_code: str, exc: Exception | None) -> dict[str, Any]:
    return {
        "table": table,
        "kind": kind,
        "available": False,
        "status": "unavailable",
        "reason": error_code,
        "error_code": error_code,
        "adapter_error_kind": type(exc).__name__ if exc is not None else None,
        "adapter_error": str(exc) if exc is not None else None,
    }


def _all_statuses(spec: StatusQueueSpec) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *spec.active_statuses,
                *spec.due_statuses,
                *spec.running_statuses,
                *spec.failed_statuses,
                *spec.terminal_statuses,
            )
        )
    )


def _worker_filter(table: str, worker_name: str | None) -> tuple[str, dict[str, str]]:
    spec = DIRTY_TARGET_WORKER_FILTERS.get((table, worker_name or ""))
    if spec is None:
        return "", {}
    column, value = spec
    return f"AND {_validate_identifier(column)} = %(worker_filter_value)s", {"worker_filter_value": value}


def _status_filter(column: str, statuses: tuple[str, ...]) -> str:
    if not statuses:
        return "FALSE"
    values = ", ".join("'" + value.replace("'", "''") + "'" for value in statuses)
    return f"{_validate_identifier(column)} IN ({values})"


def _validate_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"unsafe SQL identifier: {value}")
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise TypeError("queue_health_row_mapping_required")
    return dict(row)


def _required_count(row: Mapping[str, Any], key: str) -> int:
    if key not in row or isinstance(row[key], bool) or not isinstance(row[key], int) or row[key] < 0:
        raise ValueError(f"queue_health_count_required:{key}")
    return int(row[key])


def _optional_count(row: Mapping[str, Any], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"queue_health_count_required:{key}")
    return int(value)


def _age_ms(now_ms: int, started_at_ms: int | None) -> int | None:
    if started_at_ms is None:
        return None
    return max(0, int(now_ms) - int(started_at_ms))


def _status(*, queue_depth: int, due_count: int, running_count: int, failed_count: int, blocked_count: int) -> str:
    if blocked_count:
        return "blocked"
    if failed_count:
        return "degraded"
    if queue_depth or due_count or running_count:
        return "ok"
    return "idle"


def _reason(status: str, *, due_count: int) -> str:
    if status == "blocked":
        return "blocked_work_present"
    if status == "degraded":
        return "retryable_failures_present"
    if due_count:
        return "due_work_present"
    if status == "idle":
        return "no_active_work"
    return "fresh_work"
