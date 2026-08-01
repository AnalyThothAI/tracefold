from __future__ import annotations

from typing import Any

from tracefold.market.pricing.market_tick import EnrichedEventCapture
from tracefold.platform.postgres.queue_terminal import terminalize_source_row
from tracefold.platform.postgres.write_contract import expect_mutation_count, returning_mutation_count
from tracefold.platform.validation import require_nonnegative_int, require_positive_int

PENDING_CAPTURE_REASON = "pending_backfill"


class EventAnchorBackfillJobRepository:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def enqueue_for_capture(self, capture: EnrichedEventCapture, *, active_window_ms: int) -> None:
        if capture.capture_method != "unavailable" or capture.capture_reason != PENDING_CAPTURE_REASON:
            return
        self._conn.execute(
            """
            INSERT INTO event_anchor_backfill_jobs(
                event_id,
                intent_id,
                resolution_id,
                target_type,
                target_id,
                t_event_ms,
                status,
                next_run_at_ms,
                active_until_ms,
                attempt_count,
                last_reason,
                created_at_ms,
                updated_at_ms
            )
            VALUES (
                %(event_id)s,
                %(intent_id)s,
                %(resolution_id)s,
                %(target_type)s,
                %(target_id)s,
                %(t_event_ms)s,
                %(status)s,
                %(next_run_at_ms)s,
                %(active_until_ms)s,
                0,
                NULL,
                %(created_at_ms)s,
                %(updated_at_ms)s
            )
            ON CONFLICT(event_id, intent_id) DO NOTHING
            """,
            {
                "event_id": capture.event_id,
                "intent_id": capture.intent_id,
                "resolution_id": capture.resolution_id,
                "target_type": capture.target_type,
                "target_id": capture.target_id,
                "t_event_ms": capture.t_event_ms,
                "status": "pending",
                "next_run_at_ms": capture.created_at_ms,
                "active_until_ms": capture.created_at_ms
                + require_positive_int(
                    active_window_ms,
                    error_code="event_anchor_active_window_ms_required",
                ),
                "created_at_ms": capture.created_at_ms,
                "updated_at_ms": capture.created_at_ms,
            },
        )

    def claim_due(
        self,
        *,
        limit: int,
        now_ms: int,
        min_age_ms: int,
        lease_owner: str,
        lease_ms: int,
    ) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            """
            WITH due AS (
              SELECT event_id, intent_id
              FROM event_anchor_backfill_jobs
              WHERE status = 'pending'
                AND next_run_at_ms <= %(now_ms)s
                AND created_at_ms <= %(ready_before_ms)s
                AND active_until_ms >= %(now_ms)s
              ORDER BY next_run_at_ms ASC, created_at_ms ASC, event_id ASC, intent_id ASC
              LIMIT %(limit)s
              FOR UPDATE SKIP LOCKED
            )
            UPDATE event_anchor_backfill_jobs AS jobs
            SET status = 'running',
                attempt_count = attempt_count + 1,
                lease_owner = %(lease_owner)s,
                leased_until_ms = %(leased_until_ms)s,
                updated_at_ms = %(now_ms)s
            FROM due
            WHERE jobs.event_id = due.event_id
              AND jobs.intent_id = due.intent_id
            RETURNING jobs.*
            """,
            {
                "now_ms": int(now_ms),
                "ready_before_ms": int(now_ms)
                - require_nonnegative_int(min_age_ms, error_code="event_anchor_min_age_ms_required"),
                "lease_owner": _required_text(lease_owner, "lease_owner"),
                "leased_until_ms": int(now_ms)
                + require_positive_int(lease_ms, error_code="event_anchor_lease_ms_required"),
                "limit": require_positive_int(limit, error_code="event_anchor_limit_required"),
            },
        )
        return _returning_rows(cursor)

    def release_prework(
        self,
        *,
        event_id: str,
        intent_id: str,
        lease_owner: str,
        attempt_count: int,
    ) -> bool:
        row = self._conn.execute(
            """
            UPDATE event_anchor_backfill_jobs
               SET status = 'pending',
                   lease_owner = NULL,
                   leased_until_ms = NULL,
                   attempt_count = attempt_count - 1
             WHERE event_id = %s
               AND intent_id = %s
               AND status = 'running'
               AND lease_owner = %s
               AND attempt_count = %s
               AND attempt_count > 0
            RETURNING event_id
            """,
            (event_id, intent_id, lease_owner, int(attempt_count)),
        ).fetchone()
        return row is not None

    def expire_stale(
        self,
        *,
        limit: int,
        now_ms: int,
        max_attempts: int,
        retry_backoff_ms: int,
    ) -> dict[str, Any]:
        parsed_limit = require_positive_int(limit, error_code="event_anchor_limit_required")
        required_max_attempts = require_positive_int(
            max_attempts,
            error_code="event_anchor_max_attempts_required",
        )
        required_retry_backoff_ms = require_positive_int(
            retry_backoff_ms,
            error_code="event_anchor_retry_backoff_ms_required",
        )
        expired_rows = self._terminalize_expired_jobs(limit=parsed_limit, now_ms=now_ms)
        remaining = max(0, parsed_limit - len(expired_rows))
        rescheduled_rows = self._reschedule_stale_running_jobs(
            limit=remaining,
            now_ms=now_ms,
            max_attempts=required_max_attempts,
            retry_backoff_ms=required_retry_backoff_ms,
        )
        remaining = max(0, remaining - len(rescheduled_rows))
        failed_rows = self._fail_exhausted_stale_running_jobs(
            limit=remaining,
            now_ms=now_ms,
            max_attempts=required_max_attempts,
        )
        for row in expired_rows:
            _terminalize_event_anchor_row(
                self._conn,
                row,
                status="expired",
                reason="backfill_expired",
                now_ms=now_ms,
            )
        for row in failed_rows:
            _terminalize_event_anchor_row(
                self._conn,
                row,
                status="failed",
                reason="backfill_expired",
                now_ms=now_ms,
            )
        terminal_rows = expired_rows + failed_rows
        return {
            "expired": len(expired_rows),
            "failed": len(failed_rows),
            "rescheduled": len(rescheduled_rows),
            "terminal_rows": terminal_rows,
        }

    def mark_done(
        self,
        *,
        event_id: str,
        intent_id: str,
        now_ms: int,
        lease_owner: str,
        attempt_count: int,
    ) -> bool:
        cursor = self._conn.execute(
            """
            UPDATE event_anchor_backfill_jobs
            SET status = 'done',
                lease_owner = NULL,
                leased_until_ms = NULL,
                updated_at_ms = %(now_ms)s
            WHERE event_id = %(event_id)s
              AND intent_id = %(intent_id)s
              AND status = 'running'
              AND lease_owner = %(lease_owner)s
              AND attempt_count = %(attempt_count)s
            RETURNING event_id, intent_id
            """,
            {
                "event_id": event_id,
                "intent_id": intent_id,
                "now_ms": int(now_ms),
                "lease_owner": _required_text(lease_owner, "lease_owner"),
                "attempt_count": int(attempt_count),
            },
        )
        row = cursor.fetchone()
        return returning_mutation_count(cursor, row, error_code="event_anchor_job_repository_rowcount_invalid") == 1

    def mark_terminal(
        self,
        *,
        event_id: str,
        intent_id: str,
        status: str,
        reason: str,
        now_ms: int,
        lease_owner: str,
        attempt_count: int,
    ) -> bool:
        if status not in {"expired", "failed"}:
            raise ValueError("terminal event-anchor job status must be expired or failed")
        cursor = self._conn.execute(
            """
            UPDATE event_anchor_backfill_jobs
            SET status = %(status)s,
                last_reason = %(reason)s,
                lease_owner = NULL,
                leased_until_ms = NULL,
                updated_at_ms = %(now_ms)s
            WHERE event_id = %(event_id)s
              AND intent_id = %(intent_id)s
              AND status = 'running'
              AND lease_owner = %(lease_owner)s
              AND attempt_count = %(attempt_count)s
            RETURNING *
            """,
            {
                "event_id": event_id,
                "intent_id": intent_id,
                "status": status,
                "reason": reason,
                "now_ms": int(now_ms),
                "lease_owner": _required_text(lease_owner, "lease_owner"),
                "attempt_count": int(attempt_count),
            },
        )
        row = cursor.fetchone()
        if returning_mutation_count(cursor, row, error_code="event_anchor_job_repository_rowcount_invalid") == 0:
            return False
        source_row = dict(row)
        _terminalize_event_anchor_row(self._conn, source_row, status=status, reason=reason, now_ms=now_ms)
        return True

    def retry_terminal_job_from_snapshot(
        self,
        source_row: dict[str, Any],
        *,
        now_ms: int,
        reason: str,
    ) -> dict[str, Any] | None:
        event_id = str(source_row.get("event_id") or "")
        intent_id = str(source_row.get("intent_id") or "")
        if not event_id or not intent_id:
            raise ValueError("event_anchor_terminal_source_required")
        created_at_ms = _optional_int(source_row.get("created_at_ms"))
        active_until_ms = _optional_int(source_row.get("active_until_ms"))
        if created_at_ms is None or active_until_ms is None:
            raise ValueError("event_anchor_terminal_active_window_required")
        retry_active_until_ms = int(now_ms) + require_positive_int(
            active_until_ms - created_at_ms,
            error_code="event_anchor_terminal_active_window_required",
        )
        cursor = self._conn.execute(
            """
            UPDATE event_anchor_backfill_jobs
            SET status = 'pending',
                attempt_count = 0,
                last_reason = %(reason)s,
                next_run_at_ms = %(now_ms)s,
                active_until_ms = GREATEST(active_until_ms, %(active_until_ms)s),
                lease_owner = NULL,
                leased_until_ms = NULL,
                updated_at_ms = %(now_ms)s
            WHERE event_id = %(event_id)s
              AND intent_id = %(intent_id)s
              AND status IN ('failed', 'expired')
            RETURNING *
            """,
            {
                "event_id": event_id,
                "intent_id": intent_id,
                "reason": f"terminal_retry:{reason}"[:1000],
                "now_ms": int(now_ms),
                "active_until_ms": retry_active_until_ms,
            },
        )
        row = cursor.fetchone()
        if returning_mutation_count(cursor, row, error_code="event_anchor_job_repository_rowcount_invalid") == 0:
            return None
        return dict(row) if row is not None else None

    def reconcile_ready_historical_jobs(
        self,
        *,
        limit: int,
        now_ms: int,
        execute: bool,
    ) -> dict[str, Any]:
        parsed_limit = require_positive_int(limit, error_code="event_anchor_limit_required")
        explain = [
            _explain_text(row)
            for row in self._conn.execute(
                """
                EXPLAIN (FORMAT TEXT)
                SELECT job.event_id, job.intent_id
                FROM event_anchor_backfill_jobs job
                JOIN enriched_events anchor
                  ON anchor.event_id = job.event_id
                 AND anchor.intent_id = job.intent_id
                WHERE job.status = 'pending'
                  AND anchor.capture_method <> 'unavailable'
                  AND anchor.tick_id IS NOT NULL
                  AND anchor.tick_lag_ms IS NOT NULL
                ORDER BY job.created_at_ms ASC, job.event_id ASC, job.intent_id ASC
                LIMIT %(limit)s
                """,
                {"limit": parsed_limit},
            ).fetchall()
        ]
        candidates_before = self._ready_historical_job_count(limit=parsed_limit)
        updated_rows: list[dict[str, Any]] = []
        if execute and candidates_before:
            cursor = self._conn.execute(
                """
                WITH ready AS (
                  SELECT job.event_id, job.intent_id
                  FROM event_anchor_backfill_jobs job
                  JOIN enriched_events anchor
                    ON anchor.event_id = job.event_id
                   AND anchor.intent_id = job.intent_id
                  WHERE job.status = 'pending'
                    AND anchor.capture_method <> 'unavailable'
                    AND anchor.tick_id IS NOT NULL
                    AND anchor.tick_lag_ms IS NOT NULL
                  ORDER BY job.created_at_ms ASC, job.event_id ASC, job.intent_id ASC
                  LIMIT %(limit)s
                )
                UPDATE event_anchor_backfill_jobs job
                SET status = 'done',
                    last_reason = 'historical_ready_reconcile',
                    updated_at_ms = %(now_ms)s
                FROM ready
                WHERE job.event_id = ready.event_id
                  AND job.intent_id = ready.intent_id
                RETURNING job.event_id, job.intent_id
                """,
                {"limit": parsed_limit, "now_ms": int(now_ms)},
            )
            updated_rows = _returning_rows(cursor)
        updated_count = len(updated_rows)
        remaining = self._ready_historical_job_count(limit=parsed_limit)
        return {
            "mode": "execute" if execute else "dry_run",
            "execute": bool(execute),
            "limit": parsed_limit,
            "ready_pending_count": candidates_before,
            "updated_count": updated_count,
            "remaining_ready_pending_count": remaining,
            "explain": explain,
            "updated": updated_rows[:20],
        }

    def _ready_historical_job_count(self, *, limit: int) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM (
              SELECT 1
              FROM event_anchor_backfill_jobs job
              JOIN enriched_events anchor
                ON anchor.event_id = job.event_id
               AND anchor.intent_id = job.intent_id
              WHERE job.status = 'pending'
                AND anchor.capture_method <> 'unavailable'
                AND anchor.tick_id IS NOT NULL
                AND anchor.tick_lag_ms IS NOT NULL
              LIMIT %(limit)s
            ) ready
            """,
            {"limit": require_positive_int(limit, error_code="event_anchor_limit_required")},
        ).fetchone()
        return int((row or {}).get("count") or 0)

    def _terminalize_expired_jobs(self, *, limit: int, now_ms: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        cursor = self._conn.execute(
            """
            WITH due AS (
              SELECT event_id, intent_id
              FROM event_anchor_backfill_jobs
              WHERE (
                  status = 'pending'
                  AND active_until_ms < %(now_ms)s
                )
                 OR (
                  status = 'running'
                  AND COALESCE(leased_until_ms, 0) <= %(now_ms)s
                  AND active_until_ms < %(now_ms)s
                )
              ORDER BY active_until_ms ASC, updated_at_ms ASC, event_id ASC, intent_id ASC
              LIMIT %(limit)s
              FOR UPDATE SKIP LOCKED
            )
            UPDATE event_anchor_backfill_jobs AS jobs
            SET status = 'expired',
                last_reason = 'backfill_expired',
                lease_owner = NULL,
                leased_until_ms = NULL,
                updated_at_ms = %(now_ms)s
            FROM due
            WHERE jobs.event_id = due.event_id
              AND jobs.intent_id = due.intent_id
            RETURNING jobs.*
            """,
            {"now_ms": int(now_ms), "limit": require_positive_int(limit, error_code="event_anchor_limit_required")},
        )
        return _returning_rows(cursor)

    def _reschedule_stale_running_jobs(
        self,
        *,
        limit: int,
        now_ms: int,
        max_attempts: int,
        retry_backoff_ms: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        cursor = self._conn.execute(
            """
            WITH due AS (
              SELECT event_id, intent_id
              FROM event_anchor_backfill_jobs
              WHERE status = 'running'
                AND COALESCE(leased_until_ms, 0) <= %(now_ms)s
                AND active_until_ms >= %(now_ms)s
                AND attempt_count < %(max_attempts)s
              ORDER BY leased_until_ms ASC NULLS FIRST, updated_at_ms ASC, event_id ASC, intent_id ASC
              LIMIT %(limit)s
              FOR UPDATE SKIP LOCKED
            )
            UPDATE event_anchor_backfill_jobs AS jobs
            SET status = 'pending',
                last_reason = 'lease_expired',
                next_run_at_ms = LEAST(active_until_ms, %(next_run_at_ms)s),
                lease_owner = NULL,
                leased_until_ms = NULL,
                updated_at_ms = %(now_ms)s
            FROM due
            WHERE jobs.event_id = due.event_id
              AND jobs.intent_id = due.intent_id
            RETURNING jobs.*
            """,
            {
                "now_ms": int(now_ms),
                "next_run_at_ms": int(now_ms)
                + require_positive_int(
                    retry_backoff_ms,
                    error_code="event_anchor_retry_backoff_ms_required",
                ),
                "max_attempts": require_positive_int(
                    max_attempts,
                    error_code="event_anchor_max_attempts_required",
                ),
                "limit": require_positive_int(limit, error_code="event_anchor_limit_required"),
            },
        )
        return _returning_rows(cursor)

    def _fail_exhausted_stale_running_jobs(
        self,
        *,
        limit: int,
        now_ms: int,
        max_attempts: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        cursor = self._conn.execute(
            """
            WITH due AS (
              SELECT event_id, intent_id
              FROM event_anchor_backfill_jobs
              WHERE status = 'running'
                AND COALESCE(leased_until_ms, 0) <= %(now_ms)s
                AND active_until_ms >= %(now_ms)s
                AND attempt_count >= %(max_attempts)s
              ORDER BY leased_until_ms ASC NULLS FIRST, updated_at_ms ASC, event_id ASC, intent_id ASC
              LIMIT %(limit)s
              FOR UPDATE SKIP LOCKED
            )
            UPDATE event_anchor_backfill_jobs AS jobs
            SET status = 'failed',
                last_reason = 'backfill_expired',
                lease_owner = NULL,
                leased_until_ms = NULL,
                updated_at_ms = %(now_ms)s
            FROM due
            WHERE jobs.event_id = due.event_id
              AND jobs.intent_id = due.intent_id
            RETURNING jobs.*
            """,
            {
                "now_ms": int(now_ms),
                "max_attempts": require_positive_int(
                    max_attempts,
                    error_code="event_anchor_max_attempts_required",
                ),
                "limit": require_positive_int(limit, error_code="event_anchor_limit_required"),
            },
        )
        return _returning_rows(cursor)

    def reschedule(
        self,
        *,
        event_id: str,
        intent_id: str,
        reason: str,
        now_ms: int,
        next_run_at_ms: int,
        lease_owner: str,
        attempt_count: int,
    ) -> bool:
        cursor = self._conn.execute(
            """
            UPDATE event_anchor_backfill_jobs
            SET status = 'pending',
                last_reason = %(reason)s,
                next_run_at_ms = %(next_run_at_ms)s,
                lease_owner = NULL,
                leased_until_ms = NULL,
                updated_at_ms = %(now_ms)s
            WHERE event_id = %(event_id)s
              AND intent_id = %(intent_id)s
              AND status = 'running'
              AND lease_owner = %(lease_owner)s
              AND attempt_count = %(attempt_count)s
            RETURNING event_id, intent_id
            """,
            {
                "event_id": event_id,
                "intent_id": intent_id,
                "reason": reason,
                "now_ms": int(now_ms),
                "next_run_at_ms": int(next_run_at_ms),
                "lease_owner": _required_text(lease_owner, "lease_owner"),
                "attempt_count": int(attempt_count),
            },
        )
        row = cursor.fetchone()
        return returning_mutation_count(cursor, row, error_code="event_anchor_job_repository_rowcount_invalid") == 1


def _returning_rows(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    expect_mutation_count(cursor, expected=len(rows), error_code="event_anchor_job_repository_rowcount_invalid")
    return [dict(row) for row in rows]


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _terminalize_event_anchor_row(
    conn: Any,
    row: dict[str, Any],
    *,
    status: str,
    reason: str,
    now_ms: int,
) -> None:
    terminalize_source_row(
        conn,
        owner_key="event_anchor_backfill",
        source_table="event_anchor_backfill_jobs",
        target_key=f"{row.get('event_id')}:{row.get('intent_id')}",
        source_row=row,
        final_status=status,
        final_reason=reason,
        now_ms=now_ms,
        first_seen_at_ms=_optional_int(row.get("created_at_ms")),
        last_attempted_at_ms=now_ms,
    )


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name}_required")
    return text


def _explain_text(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("QUERY PLAN") or row.get("QUERY_PLAN") or next(iter(row.values()), ""))
    try:
        return str(row[0])
    except Exception:
        return str(row)
