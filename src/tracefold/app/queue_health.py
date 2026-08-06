from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tracefold.platform.postgres.projection_frontier import FRONTIER_SPECS, FrontierSpec


@dataclass(frozen=True, slots=True)
class StatusQueueSpec:
    owner_key: str
    table: str
    waiting_statuses: tuple[str, ...]
    running_statuses: tuple[str, ...]
    retry_statuses: tuple[str, ...]
    terminal_statuses: tuple[str, ...]
    due_column: str
    lease_column: str
    running_age_column: str = "updated_at_ms"


@dataclass(frozen=True, slots=True)
class DirtyTargetQueueSpec:
    owner_key: str
    table: str
    terminal_column: str | None = None


_STATUS_QUEUE_SPECS = {
    "event_anchor_backfill_jobs": StatusQueueSpec(
        owner_key="event_anchor_backfill",
        table="event_anchor_backfill_jobs",
        waiting_statuses=("pending",),
        running_statuses=("running",),
        retry_statuses=(),
        terminal_statuses=("failed", "expired"),
        due_column="next_run_at_ms",
        lease_column="leased_until_ms",
    ),
    "news_brief_runs": StatusQueueSpec(
        owner_key="news_brief",
        table="news_brief_runs",
        waiting_statuses=("retryable",),
        running_statuses=("running",),
        retry_statuses=("retryable",),
        terminal_statuses=("failed",),
        due_column="next_due_at_ms",
        lease_column="lease_expires_at_ms",
        running_age_column="heartbeat_at_ms",
    ),
    "macro_document_analysis_jobs": StatusQueueSpec(
        owner_key="macro_document_analysis",
        table="macro_document_analysis_jobs",
        waiting_statuses=("pending", "retryable"),
        running_statuses=("claimed",),
        retry_statuses=("retryable",),
        terminal_statuses=("failed",),
        due_column="next_due_at_ms",
        lease_column="leased_until_ms",
    ),
}

_DIRTY_TARGET_QUEUE_SPECS = {
    "asset_profile_refresh_targets": DirtyTargetQueueSpec(
        owner_key="asset_profile_refresh",
        table="asset_profile_refresh_targets",
        terminal_column="terminal_reason",
    ),
    "token_discovery_dirty_lookup_keys": DirtyTargetQueueSpec(
        owner_key="resolution_refresh",
        table="token_discovery_dirty_lookup_keys",
    ),
    "token_image_source_dirty_targets": DirtyTargetQueueSpec(
        owner_key="token_image_mirror",
        table="token_image_source_dirty_targets",
    ),
}

_FRONTIER_SPECS = {spec.table: spec for spec in FRONTIER_SPECS}
_QUEUE_OWNER_BY_TABLE = {
    **{table: spec.owner_key for table, spec in _STATUS_QUEUE_SPECS.items()},
    **{table: spec.owner_key for table, spec in _DIRTY_TARGET_QUEUE_SPECS.items()},
    **{table: f"{spec.domain}_projection" for table, spec in _FRONTIER_SPECS.items()},
}

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def queue_tables_for_owner(owner_key: str | None) -> tuple[str, ...]:
    return tuple(
        sorted(
            table
            for table, declared_owner in _QUEUE_OWNER_BY_TABLE.items()
            if owner_key is None or declared_owner == owner_key
        )
    )


def fetch_queue_table_health(
    conn: Any,
    table: str,
    *,
    now_ms: int,
) -> dict[str, Any]:
    """Read one declared durable queue without acquiring the maintenance gate."""

    _validate_identifier(table)
    status_spec = _STATUS_QUEUE_SPECS.get(table)
    if status_spec is not None:
        return _status_queue_health(conn, status_spec, now_ms=now_ms)
    dirty_spec = _DIRTY_TARGET_QUEUE_SPECS.get(table)
    if dirty_spec is not None:
        return _dirty_target_queue_health(conn, dirty_spec, now_ms=now_ms)
    frontier_spec = _FRONTIER_SPECS.get(table)
    if frontier_spec is not None:
        return _frontier_queue_health(conn, frontier_spec, now_ms=now_ms)
    return _unavailable_health(table, "unknown", "unknown_queue_table", None)


def _status_queue_health(
    conn: Any,
    spec: StatusQueueSpec,
    *,
    now_ms: int,
) -> dict[str, Any]:
    table = _validate_identifier(spec.table)
    due_column = _validate_identifier(spec.due_column)
    lease_column = _validate_identifier(spec.lease_column)
    running_age_column = _validate_identifier(spec.running_age_column)
    waiting_filter = _status_filter("status", spec.waiting_statuses)
    running_filter = _status_filter("status", spec.running_statuses)
    active_filter = _status_filter("status", (*spec.waiting_statuses, *spec.running_statuses))
    retry_filter = _status_filter("status", spec.retry_statuses)
    terminal_filter = _status_filter("status", spec.terminal_statuses)
    all_statuses = _all_statuses(spec)
    try:
        counts = _status_counts(conn, table, all_statuses)
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total_count,
              COUNT(*) FILTER (WHERE {active_filter}) AS active_count,
              COUNT(*) FILTER (
                WHERE ({waiting_filter} AND {due_column} <= %(now_ms)s)
                   OR ({running_filter} AND COALESCE({lease_column}, 0) <= %(now_ms)s)
              ) AS due_count,
              COUNT(*) FILTER (
                WHERE {running_filter} AND {lease_column} > %(now_ms)s
              ) AS running_count,
              COUNT(*) FILTER (WHERE {retry_filter}) AS failed_count,
              COUNT(*) FILTER (WHERE {terminal_filter}) AS source_terminal_count,
              MIN(
                CASE
                  WHEN {waiting_filter} AND {due_column} <= %(now_ms)s
                    THEN {due_column}
                  WHEN {running_filter} AND COALESCE({lease_column}, 0) <= %(now_ms)s
                    THEN COALESCE({lease_column}, 0)
                  ELSE NULL
                END
              ) AS oldest_due_at_ms,
              MIN({running_age_column}) FILTER (
                WHERE {running_filter} AND {lease_column} > %(now_ms)s
              ) AS oldest_running_at_ms,
              MAX(attempt_count) AS max_attempt_count
            FROM {table}
            WHERE {_status_filter("status", all_statuses)}
            """,
            {"now_ms": int(now_ms)},
        ).fetchone()
        return _table_health(
            owner_key=spec.owner_key,
            table=table,
            kind="status_queue",
            counts_by_status=counts,
            metrics=_row_dict(row),
            terminal_metrics=_terminal_metrics(conn, table, owner_key=spec.owner_key),
            now_ms=now_ms,
        )
    except Exception as exc:
        return _unavailable_health(table, "status_queue", "queue_query_failed", exc, owner_key=spec.owner_key)


def _dirty_target_queue_health(
    conn: Any,
    spec: DirtyTargetQueueSpec,
    *,
    now_ms: int,
) -> dict[str, Any]:
    table = _validate_identifier(spec.table)
    active_filter = "TRUE"
    source_terminal_filter = "FALSE"
    if spec.terminal_column is not None:
        terminal_column = _validate_identifier(spec.terminal_column)
        active_filter = f"{terminal_column} IS NULL"
        source_terminal_filter = f"{terminal_column} IS NOT NULL"
    try:
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total_count,
              COUNT(*) FILTER (WHERE {active_filter}) AS active_count,
              COUNT(*) FILTER (
                WHERE {active_filter}
                  AND due_at_ms <= %(now_ms)s
                  AND (leased_until_ms IS NULL OR leased_until_ms <= %(now_ms)s)
              ) AS due_count,
              COUNT(*) FILTER (
                WHERE {active_filter} AND leased_until_ms > %(now_ms)s
              ) AS running_count,
              COUNT(*) FILTER (
                WHERE {active_filter} AND COALESCE(last_error, '') <> ''
              ) AS failed_count,
              COUNT(*) FILTER (WHERE {source_terminal_filter}) AS source_terminal_count,
              MIN(due_at_ms) FILTER (
                WHERE {active_filter}
                  AND due_at_ms <= %(now_ms)s
                  AND (leased_until_ms IS NULL OR leased_until_ms <= %(now_ms)s)
              ) AS oldest_due_at_ms,
              MIN(updated_at_ms) FILTER (
                WHERE {active_filter} AND leased_until_ms > %(now_ms)s
              ) AS oldest_running_at_ms,
              MAX(attempt_count) FILTER (WHERE {active_filter}) AS max_attempt_count
            FROM {table}
            """,
            {"now_ms": int(now_ms)},
        ).fetchone()
        return _table_health(
            owner_key=spec.owner_key,
            table=table,
            kind="dirty_target",
            counts_by_status={},
            metrics=_row_dict(row),
            terminal_metrics=_terminal_metrics(conn, table, owner_key=spec.owner_key),
            now_ms=now_ms,
        )
    except Exception as exc:
        return _unavailable_health(table, "dirty_target", "queue_query_failed", exc, owner_key=spec.owner_key)


def _frontier_queue_health(
    conn: Any,
    spec: FrontierSpec,
    *,
    now_ms: int,
) -> dict[str, Any]:
    table = _validate_identifier(spec.table)
    owner_key = f"{spec.domain}_projection"
    visible_statuses = ("dirty", "running", "retry_wait", "quarantined")
    try:
        counts = _status_counts(conn, table, visible_statuses)
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS total_count,
              COUNT(*) FILTER (
                WHERE status IN ('dirty', 'running', 'retry_wait')
              ) AS active_count,
              COUNT(*) FILTER (
                WHERE (
                  status = 'dirty'
                  AND COALESCE(next_attempt_at_ms, first_dirty_at_ms, deadline_at_ms) <= %(now_ms)s
                ) OR (
                  status = 'retry_wait'
                  AND COALESCE(next_attempt_at_ms, deadline_at_ms) <= %(now_ms)s
                ) OR (
                  status = 'running'
                  AND COALESCE(claimed_until_ms, 0) <= %(now_ms)s
                )
              ) AS due_count,
              COUNT(*) FILTER (
                WHERE status = 'running' AND claimed_until_ms > %(now_ms)s
              ) AS running_count,
              COUNT(*) FILTER (WHERE status = 'retry_wait') AS failed_count,
              COUNT(*) FILTER (WHERE status = 'quarantined') AS source_terminal_count,
              MIN(
                CASE
                  WHEN status = 'dirty'
                    AND COALESCE(next_attempt_at_ms, first_dirty_at_ms, deadline_at_ms) <= %(now_ms)s
                    THEN COALESCE(next_attempt_at_ms, first_dirty_at_ms, deadline_at_ms)
                  WHEN status = 'retry_wait'
                    AND COALESCE(next_attempt_at_ms, deadline_at_ms) <= %(now_ms)s
                    THEN COALESCE(next_attempt_at_ms, deadline_at_ms)
                  WHEN status = 'running'
                    AND COALESCE(claimed_until_ms, 0) <= %(now_ms)s
                    THEN COALESCE(claimed_until_ms, 0)
                  ELSE NULL
                END
              ) AS oldest_due_at_ms,
              MIN(updated_at_ms) FILTER (
                WHERE status = 'running' AND claimed_until_ms > %(now_ms)s
              ) AS oldest_running_at_ms,
              MAX(attempt_count) AS max_attempt_count
            FROM {table}
            WHERE status IN ('dirty', 'running', 'retry_wait', 'quarantined')
            """,
            {"now_ms": int(now_ms)},
        ).fetchone()
        return _table_health(
            owner_key=owner_key,
            table=table,
            kind="projection_frontier",
            counts_by_status=counts,
            metrics=_row_dict(row),
            terminal_metrics=_terminal_metrics(conn, table, owner_key=owner_key),
            now_ms=now_ms,
        )
    except Exception as exc:
        return _unavailable_health(table, "projection_frontier", "queue_query_failed", exc, owner_key=owner_key)


def _status_counts(conn: Any, table: str, statuses: tuple[str, ...]) -> dict[str, int]:
    rows = conn.execute(
        f"""
        SELECT status, COUNT(*) AS count
        FROM {_validate_identifier(table)}
        WHERE {_status_filter("status", statuses)}
        GROUP BY status
        """
    ).fetchall()
    return {str(_row_dict(row)["status"]): int(_row_dict(row)["count"]) for row in rows}


def _terminal_metrics(conn: Any, source_table: str, *, owner_key: str) -> dict[str, Any]:
    params = {"source_table": source_table, "owner_key": owner_key}
    metrics = _row_dict(
        conn.execute(
            """
            SELECT
              COUNT(*) AS terminal_count,
              COUNT(*) FILTER (WHERE operator_action IS NULL) AS unresolved_terminal_count
            FROM queue_terminal_events
            WHERE source_table = %(source_table)s
              AND owner_key = %(owner_key)s
            """,
            params,
        ).fetchone()
    )
    rows = conn.execute(
        """
        SELECT final_reason_bucket, COUNT(*) AS count
        FROM queue_terminal_events
        WHERE source_table = %(source_table)s
          AND owner_key = %(owner_key)s
          AND operator_action IS NULL
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
    owner_key: str,
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
    blocked_count = unresolved_terminal_count
    status = _status(
        queue_depth=queue_depth,
        due_count=due_count,
        running_count=running_count,
        failed_count=failed_count,
        blocked_count=blocked_count,
    )
    return {
        "owner": owner_key,
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


def _unavailable_health(
    table: str,
    kind: str,
    error_code: str,
    exc: Exception | None,
    *,
    owner_key: str | None = None,
) -> dict[str, Any]:
    return {
        "owner": owner_key,
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
                *spec.waiting_statuses,
                *spec.running_statuses,
                *spec.retry_statuses,
                *spec.terminal_statuses,
            )
        )
    )


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
    return "scheduled_work_present"


__all__ = ["fetch_queue_table_health", "queue_tables_for_owner"]
