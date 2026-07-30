from __future__ import annotations

from typing import Any
from uuid import UUID

from tracefold.platform.postgres.projection_frontier import FRONTIER_SPECS


def recover_old_runtime_claims(
    db: Any,
    *,
    runtime_id: str,
    now_ms: int,
) -> dict[str, int]:
    """Release claims left by a prior steady runtime after singleton ownership."""

    current_runtime = UUID(str(runtime_id))
    recovered: dict[str, int] = {}
    with (
        db.worker_session(
            "runtime_claim_recovery",
            statement_timeout_seconds=3.0,
        ) as repos,
        repos.transaction(),
    ):
        for spec in FRONTIER_SPECS:
            cursor = repos.conn.execute(
                f"""
                UPDATE {spec.table}
                   SET status = 'dirty',
                       next_attempt_at_ms = NULL,
                       claimed_by = NULL,
                       claimed_until_ms = NULL,
                       last_error_code = NULL,
                       updated_at_ms = %(now_ms)s
                 WHERE status = 'running'
                   AND claimed_by IS DISTINCT FROM %(runtime_id)s
                """,
                {
                    "now_ms": int(now_ms),
                    "runtime_id": current_runtime,
                },
            )
            recovered[spec.table] = int(cursor.rowcount or 0)

        recovered["event_anchor_backfill_jobs"] = _count(
            repos.conn.execute(
                """
                UPDATE event_anchor_backfill_jobs
                   SET status = 'pending',
                       next_run_at_ms = LEAST(next_run_at_ms, %(now_ms)s),
                       leased_until_ms = NULL,
                       lease_owner = NULL,
                       attempt_count = GREATEST(0, attempt_count - 1),
                       updated_at_ms = %(now_ms)s
                 WHERE status = 'running'
                """,
                {"now_ms": int(now_ms)},
            )
        )
        for table in (
            "asset_profile_refresh_targets",
            "token_image_source_dirty_targets",
            "token_discovery_dirty_lookup_keys",
        ):
            recovered[table] = _count(
                repos.conn.execute(
                    f"""
                    UPDATE {table}
                       SET due_at_ms = LEAST(due_at_ms, %(now_ms)s),
                           leased_until_ms = NULL,
                           lease_owner = NULL,
                           attempt_count = GREATEST(0, attempt_count - 1),
                           updated_at_ms = %(now_ms)s
                     WHERE lease_owner IS NOT NULL
                    """,
                    {"now_ms": int(now_ms)},
                )
            )
        recovered["token_discovery_results"] = _count(
            repos.conn.execute(
                """
                UPDATE token_discovery_results
                   SET status = 'error',
                       next_refresh_at_ms = LEAST(next_refresh_at_ms, %(now_ms)s),
                       last_error = 'old_runtime_recovered',
                       updated_at_ms = %(now_ms)s
                 WHERE status = 'running'
                """,
                {"now_ms": int(now_ms)},
            )
        )
        recovered["macro_acquisition_targets"] = _count(
            repos.conn.execute(
                """
                UPDATE macro_acquisition_targets
                   SET status = CASE
                         WHEN clock_kind = 'backfill' THEN 'backfilling'
                         ELSE 'delayed'
                       END,
                       next_due_at_ms = LEAST(next_due_at_ms, %(now_ms)s),
                       leased_until_ms = NULL,
                       lease_owner = NULL,
                       attempt_count = GREATEST(0, attempt_count - 1),
                       last_error_code = NULL,
                       updated_at_ms = %(now_ms)s
                 WHERE status = 'claimed'
                """,
                {"now_ms": int(now_ms)},
            )
        )
        recovered["macro_document_analysis_jobs"] = _count(
            repos.conn.execute(
                """
                UPDATE macro_document_analysis_jobs
                   SET status = 'retryable',
                       next_due_at_ms = LEAST(next_due_at_ms, %(now_ms)s),
                       leased_until_ms = NULL,
                       lease_owner = NULL,
                       attempt_count = GREATEST(0, attempt_count - 1),
                       last_error_code = NULL,
                       updated_at_ms = %(now_ms)s
                 WHERE status = 'claimed'
                """,
                {"now_ms": int(now_ms)},
            )
        )
        recovered["macro_thesis_runs"] = _count(
            repos.conn.execute(
                """
                UPDATE macro_thesis_runs
                   SET status = 'retryable',
                       due_at_ms = LEAST(
                         due_at_ms,
                         GREATEST(cutoff_ms, %(now_ms)s)
                       ),
                       leased_until_ms = NULL,
                       lease_owner = NULL,
                       last_error_code = NULL,
                       last_error_message = NULL,
                       updated_at_ms = %(now_ms)s
                 WHERE status = 'running'
                """,
                {"now_ms": int(now_ms)},
            )
        )
        recovered["news_brief_runs"] = _count(
            repos.conn.execute(
                """
                UPDATE news_brief_runs
                   SET lease_expires_at_ms = LEAST(
                         lease_expires_at_ms,
                         %(now_ms)s
                       ),
                       heartbeat_at_ms = %(now_ms)s,
                       updated_at_ms = %(now_ms)s
                 WHERE status = 'running'
                """,
                {"now_ms": int(now_ms)},
            )
        )
    return recovered


def _count(cursor: Any) -> int:
    return int(cursor.rowcount or 0)


__all__ = ["recover_old_runtime_claims"]
