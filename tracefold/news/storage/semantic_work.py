"""Semantic input revisions, leases, recovery and lineage read budgets.

Commands use the caller's existing transaction; no external I/O or independent commit.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from psycopg.errors import QueryCanceled

from ..updates.contracts import SemanticLease
from ..updates.identity import identity
from ..updates.judgment import error_code
from ..updates.projection import reading_views
from .errors import EventUpdateConflict, SemanticLeaseLost
from .semantic_input import frozen_input
from .semantic_jobs import SemanticJobDetail, SemanticJobs, semantic_job
from .semantic_rows import ANALYSES_SQL, SEMANTIC_JOBS_SQL, SEMANTIC_RESULTS_SQL
from .sql_values import _retry_delay
from .update_commit import lock_event

if TYPE_CHECKING:
    from .semantic_input import SemanticInputStorage


SEMANTIC_ATTEMPTS_MAX: Final = 3


SEMANTIC_RETRY_MS: Final = (15_000, 60_000, 300_000)


SEMANTIC_WAKE_STALE_MS: Final = 15_000


SEMANTIC_INPUT_TIMEOUT: Final = "news_semantic_input_timeout"


EXTRA_READ_OUTCOMES: Final = frozenset({"attached", "no_material", "unavailable_or_budget_exhausted"})


_WAKE_STATE_LIMIT: Final = 1_000


_RUNNABLE: Final = f"attempts < {SEMANTIC_ATTEMPTS_MAX} AND last_outcome IS DISTINCT FROM 'failed'"


SEMANTIC_WAKE_STATE_SQL: Final = f"""
    WITH pending AS MATERIALIZED (
      SELECT attempts, last_outcome, updated_at_ms FROM ({SEMANTIC_JOBS_SQL})
       WHERE done_revision IS NULL OR done_revision < wanted_revision
       ORDER BY next_attempt_at_ms, event_id
       LIMIT {_WAKE_STATE_LIMIT}
    )
    SELECT count(*) FILTER (WHERE {_RUNNABLE}) AS pending,
           min(updated_at_ms) FILTER (WHERE {_RUNNABLE}) AS oldest_pending_at_ms,
           count(*) FILTER (WHERE last_outcome = 'failed') AS expired
      FROM pending
"""  # noqa: S608 - code-owned integer constants only


SEMANTIC_STATUS_SQL: Final = f"""
    WITH outstanding AS MATERIALIZED (
      SELECT attempts, last_outcome, next_attempt_at_ms, leased_until_ms
        FROM ({SEMANTIC_JOBS_SQL})
       WHERE done_revision IS NULL OR done_revision < wanted_revision
       ORDER BY next_attempt_at_ms, event_id
       LIMIT {_WAKE_STATE_LIMIT}
    )
    SELECT
      (SELECT count(*) FROM ({SEMANTIC_RESULTS_SQL}) WHERE completed_at_ms >= %(since)s)
        AS semantic_observations_24h,
      (SELECT count(*) FROM ({ANALYSES_SQL}) WHERE adopted_at_ms >= %(since)s) AS semantic_adopted_24h,
      (SELECT count(*) FROM ({SEMANTIC_JOBS_SQL}) WHERE last_outcome = 'failed' AND updated_at_ms >= %(since)s)
        AS semantic_failed_24h,
      (SELECT count(*) FROM outstanding WHERE {_RUNNABLE}
         AND next_attempt_at_ms <= %(now)s AND (leased_until_ms IS NULL OR leased_until_ms <= %(now)s))
        AS semantic_pending,
      (SELECT count(*) FROM outstanding WHERE {_RUNNABLE}
         AND next_attempt_at_ms > %(now)s) AS semantic_deferred,
      (SELECT count(*) FROM outstanding WHERE leased_until_ms > %(now)s) AS semantic_in_progress,
      (SELECT count(*) FROM outstanding WHERE last_outcome = 'failed') AS semantic_failed_exhausted
"""  # noqa: S608 - code-owned integer constant only


SEMANTIC_FAILED_CODES_SQL: Final = f"""
    SELECT COALESCE(last_error_code, 'unknown') AS code, count(*) AS n
      FROM ({SEMANTIC_JOBS_SQL})
     WHERE last_outcome = 'failed' AND updated_at_ms >= %s
     GROUP BY 1
"""  # noqa: S608 -- fixed SQL; bound values.

log = logging.getLogger("tracefold.news")


class SemanticWorkStorage:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.jobs = SemanticJobs(conn)

    def request_semantic_revision(self, *, event_id: str, lineage_id: str, now_ms: int) -> int:
        row = self.jobs.lock(event_id)
        if row is None:
            detail = SemanticJobDetail(wanted_revision=1, lineage_id=lineage_id)
            self.conn.execute(
                """INSERT INTO
 news_jobs(job_kind,subject_id,state,detail,next_attempt_at_ms,created_at_ms,updated_at_ms)
                   VALUES ('semantic',%s,'pending',%s::jsonb,%s,%s,%s)""",
                (event_id, detail.model_dump_json(), now_ms, now_ms, now_ms),
            )
            return 1
        row.update(
            wanted_revision=row["wanted_revision"] + 1,
            lineage_id=lineage_id,
            attempts=0,
            next_attempt_at_ms=now_ms,
            published_at_ms=None,
            last_outcome=None,
            last_error_code=None,
            extra_read_state=None,
            extra_read_target_ref=None,
            attached_evidence=None,
            focus_claim_refs=None,
            reanalysis_read_ref=None,
            reanalysis_reason=None,
            reanalysis_head_ref=None,
            updated_at_ms=now_ms,
        )
        self.jobs.save(row)
        return int(row["wanted_revision"])

    def mark_semantic_work_published(self, *, event_id: str, revision: int, now_ms: int) -> bool:
        row = self.jobs.lock(event_id)
        if row is None or row["wanted_revision"] != revision:
            return False
        row["published_at_ms"] = now_ms
        self.jobs.save(row)
        return True

    def claim_semantic_work(
        self, *, event_id: str, lease_token: str, now_ms: int, lease_ms: int, input: SemanticInputStorage
    ) -> SemanticLease | None:
        """Spend an attempt by a conditional UPDATE; input timeout rolls back only its savepoint."""
        lock_event(self.conn, event_id)
        row = semantic_job(
            self.conn.execute(
                """UPDATE news_jobs SET attempts=attempts+1,lease_token=%s,lease_until_ms=%s,updated_at_ms=%s
               WHERE job_kind='semantic' AND subject_id=%s AND state='pending' AND attempts<%s
                 AND next_attempt_at_ms<=%s AND (lease_until_ms IS NULL OR lease_until_ms<=%s)
               RETURNING *""",
                (lease_token, now_ms + lease_ms, now_ms, event_id, SEMANTIC_ATTEMPTS_MAX, now_ms, now_ms),
            ).fetchone()
        )
        if row is None:
            return None
        try:
            with self.conn.transaction():
                material = input.semantic_input_material(event_id, now_ms=now_ms)
            source = frozen_input(event_id, material)
        except QueryCanceled:
            self._input_timed_out(event_id, attempts=int(row["attempts"]), now_ms=now_ms)
            return None
        except (LookupError, ValueError) as exc:
            code = error_code(exc, default="news_semantic_input_invalid")
            log.warning("news semantic input failed event_id=%s code=%s", event_id, code)
            row.update(lease_token=None, leased_until_ms=None, last_outcome="failed", last_error_code=code)
            self.jobs.save(row)
            return None
        row["attempt_read_refs"] = [view.read_ref for view in reading_views(source)]
        self.jobs.save(row)
        return SemanticLease(source=source, lease_token=lease_token, attempts=int(row["attempts"]))

    def _input_timed_out(self, event_id: str, *, attempts: int, now_ms: int) -> None:
        row = self.jobs.lock(event_id)
        if row is None:
            raise SemanticLeaseLost("news_semantic_lease_lost")
        exhausted = attempts >= SEMANTIC_ATTEMPTS_MAX
        log.warning("news semantic input timed out event_id=%s attempts=%s exhausted=%s", event_id, attempts, exhausted)
        row.update(
            lease_token=None,
            leased_until_ms=None,
            last_outcome="failed" if exhausted else SEMANTIC_INPUT_TIMEOUT,
            last_error_code=SEMANTIC_INPUT_TIMEOUT,
            next_attempt_at_ms=now_ms + _retry_delay(SEMANTIC_RETRY_MS, attempts),
            attempt_read_refs=[],
            updated_at_ms=now_ms,
        )
        self.jobs.save(row)

    def require_semantic_owner(self, lease: SemanticLease, *, now_ms: int) -> dict[str, Any]:
        row = self.jobs.lock(lease.event_id)
        if row is None or row["lease_token"] != lease.lease_token or (row["leased_until_ms"] or 0) <= now_ms:
            raise SemanticLeaseLost("news_semantic_lease_lost")
        return row

    def defer_semantic_event(self, *, lease: SemanticLease, reason: str, now_ms: int, retry_after_ms: int = 0) -> bool:
        return self._end_semantic_attempt(
            lease, reason=reason, now_ms=now_ms, failed=False, retry_after_ms=retry_after_ms
        )

    def fail_semantic_event(self, *, lease: SemanticLease, error_code: str, now_ms: int) -> bool:
        return self._end_semantic_attempt(lease, reason=error_code, now_ms=now_ms, failed=True)

    def _end_semantic_attempt(
        self, lease: SemanticLease, *, reason: str, now_ms: int, failed: bool, retry_after_ms: int = 0
    ) -> bool:
        try:
            row = self.require_semantic_owner(lease, now_ms=now_ms)
        except SemanticLeaseLost:
            return False
        failed = failed or row["attempts"] >= SEMANTIC_ATTEMPTS_MAX
        if failed:
            row["failed_read_refs"] = sorted(
                set(row["failed_read_refs"]) | {v.read_ref for v in reading_views(lease.source)}
            )
        row.update(lease_token=None, leased_until_ms=None)
        if row["wanted_revision"] <= lease.wanted_revision:
            row.update(
                last_outcome="failed" if failed else reason,
                last_error_code=reason,
                next_attempt_at_ms=now_ms + max(retry_after_ms, _retry_delay(SEMANTIC_RETRY_MS, row["attempts"])),
                updated_at_ms=now_ms,
            )
        self.jobs.save(row)
        return True

    def finish_semantic_work(self, *, work_id: str, lease: SemanticLease, reason: str, now_ms: int) -> bool:
        row = self.require_semantic_owner(lease, now_ms=now_ms)
        observed = self._observed_work(work_id)
        if observed["event_id"] != lease.event_id or observed["input_revision"] != lease.wanted_revision:
            raise EventUpdateConflict("news_semantic_observation_lease_mismatch")
        row["done_revision"] = max(row["done_revision"] or 0, observed["input_revision"])
        row["processed_read_refs"] = sorted(set(row["processed_read_refs"]) | set(observed["read_refs"]))
        row["failed_read_refs"] = sorted(set(row["failed_read_refs"]) - set(observed["read_refs"]))
        if row["wanted_revision"] == lease.wanted_revision:
            row.update(
                reanalysis_read_ref=None,
                reanalysis_reason=None,
                reanalysis_head_ref=None,
                attempts=0,
                last_outcome=reason,
                last_error_code=None,
                next_attempt_at_ms=now_ms,
            )
        row.update(lease_token=None, leased_until_ms=None, updated_at_ms=now_ms)
        self.jobs.save(row)
        return True

    def _observed_work(self, work_id: str) -> Mapping[str, Any]:
        rows = self.conn.execute(
            "SELECT event_id,input_revision,read_refs FROM news_analyses WHERE work_id=%s", (work_id,)
        ).fetchall()
        if not rows or len({r["event_id"] for r in rows}) != 1:
            raise LookupError("news_semantic_work_unknown")
        return {
            "event_id": str(rows[0]["event_id"]),
            "input_revision": max(int(r["input_revision"]) for r in rows),
            "read_refs": tuple({ref for r in rows for ref in r["read_refs"]}),
        }

    def terminalize_exhausted_semantic_work(self, *, now_ms: int, limit: int) -> int:
        rows = self.conn.execute(
            """SELECT subject_id FROM news_jobs WHERE job_kind='semantic' AND state='pending' AND attempts>=%s
               AND (lease_until_ms IS NULL OR lease_until_ms<=%s) ORDER BY updated_at_ms,subject_id LIMIT %s""",
            (SEMANTIC_ATTEMPTS_MAX, now_ms, min(_WAKE_STATE_LIMIT, max(1, limit))),
        ).fetchall()
        changed = 0
        for candidate in rows:
            event_id = str(candidate["subject_id"])
            if (
                self.conn.execute(
                    "SELECT event_id FROM news_events WHERE event_id=%s FOR NO KEY UPDATE SKIP LOCKED", (event_id,)
                ).fetchone()
                is None
            ):
                continue
            row = semantic_job(
                self.conn.execute(
                    "SELECT * FROM news_jobs WHERE job_kind='semantic' AND subject_id=%s FOR UPDATE SKIP LOCKED",
                    (event_id,),
                ).fetchone()
            )
            if (
                row is None
                or row["state"] != "pending"
                or row["attempts"] < SEMANTIC_ATTEMPTS_MAX
                or (row["leased_until_ms"] or 0) > now_ms
            ):
                continue
            row.update(
                last_outcome="failed",
                last_error_code="news_semantic_attempts_exhausted_after_lease",
                failed_read_refs=sorted(set(row["failed_read_refs"]) | set(row["attempt_read_refs"])),
                lease_token=None,
                leased_until_ms=None,
                updated_at_ms=now_ms,
            )
            self.jobs.save(row)
            changed += 1
        return changed

    def pending_semantic_event_ids(self, *, now_ms: int, limit: int) -> list[str]:
        rows = self.conn.execute(
            """SELECT subject_id FROM news_jobs WHERE job_kind='semantic' AND state='pending' AND attempts<%s
               AND next_attempt_at_ms<=%s AND (lease_until_ms IS NULL OR lease_until_ms<=%s)
               AND ((detail->>'published_at_ms') IS NULL OR (detail->>'published_at_ms')::bigint<=%s)
               ORDER BY next_attempt_at_ms,subject_id LIMIT %s""",
            (SEMANTIC_ATTEMPTS_MAX, now_ms, now_ms, now_ms - SEMANTIC_WAKE_STALE_MS, limit),
        ).fetchall()
        return [str(row["subject_id"]) for row in rows]

    def semantic_work(self, event_id: str) -> dict[str, Any] | None:
        row = semantic_job(
            self.conn.execute(
                "SELECT * FROM news_jobs WHERE job_kind='semantic' AND subject_id=%s", (event_id,)
            ).fetchone()
        )
        if row is None:
            return None
        return {
            key: row[key]
            for key in (
                *SemanticJobDetail.model_fields,
                "event_id",
                "attempts",
                "next_attempt_at_ms",
                "lease_token",
                "leased_until_ms",
                "last_error_code",
                "updated_at_ms",
            )
        }

    def reanalysis_scope_list(self, *, event_id: str, now_ms: int, input: SemanticInputStorage) -> dict[str, Any]:
        """Inspect the exact current task reads without changing semantic work."""

        material = input.semantic_input_material(event_id, now_ms=now_ms)
        work = material.get("work")
        if work is None:
            raise LookupError("news_reanalysis_event_work_missing")
        if work.get("attached_evidence"):
            raise EventUpdateConflict("news_reanalysis_optional_read_pending")
        complete_work = {**work, "processed_read_refs": (), "failed_read_refs": (), "reanalysis_read_ref": None}
        source = frozen_input(event_id, {**material, "work": complete_work})
        completed = set(work.get("processed_read_refs") or ())
        failed = set(work.get("failed_read_refs") or ())
        head = material.get("head")
        return {
            "event_id": event_id,
            "wanted_revision": int(work["wanted_revision"]),
            "done_revision": work.get("done_revision"),
            "failed": work.get("last_outcome") == "failed",
            "last_error_code": work.get("last_error_code"),
            "head_revision": None if head is None else str(head["content_revision"]),
            "scopes": [
                {
                    "evidence_ref": view.evidence_ref,
                    "source_version": view.source_version,
                    "fact_ids": [
                        scope.fact_id for scope in source.extraction_scopes if scope.evidence_ref == view.evidence_ref
                    ],
                    "read_ref": view.read_ref,
                    "mode": view.mode,
                    "reason": view.reason,
                    "material_sha": view.material_sha,
                    "visible_chars": sum(len(span.text) for span in view.spans),
                    "completed": view.read_ref in completed,
                    "failed": view.read_ref in failed,
                }
                for view in reading_views(source)
            ],
        }

    def request_reanalysis(
        self,
        *,
        event_id: str,
        input: SemanticInputStorage,
        expected_wanted_revision: int,
        expected_head_revision: str | None,
        read_ref: str,
        reason: str,
        now_ms: int,
    ) -> int:
        if not reason.strip() or not read_ref:
            raise ValueError("news_reanalysis_target_or_reason_missing")
        row = self.jobs.lock(event_id)
        if row is None:
            raise LookupError("news_reanalysis_event_work_missing")
        settled = row["done_revision"] == expected_wanted_revision or row["last_outcome"] == "failed"
        if row["wanted_revision"] != expected_wanted_revision or not settled:
            raise EventUpdateConflict("news_reanalysis_wanted_revision_changed_or_incomplete")
        if (row["leased_until_ms"] or 0) > now_ms:
            raise EventUpdateConflict("news_reanalysis_lease_active")
        head = input.head_document(event_id)
        head_revision = None if head is None else str(head["content_revision"])
        if head_revision != expected_head_revision:
            raise EventUpdateConflict("news_reanalysis_head_changed")
        listing = self.reanalysis_scope_list(event_id=event_id, now_ms=now_ms, input=input)
        if read_ref not in {e["read_ref"] for e in listing["scopes"]}:
            raise EventUpdateConflict("news_reanalysis_read_scope_changed")
        next_revision = expected_wanted_revision + 1
        row.update(
            wanted_revision=next_revision,
            lineage_id=identity("lineage_reanalysis", event_id, next_revision, read_ref),
            attempts=0,
            next_attempt_at_ms=now_ms,
            published_at_ms=None,
            last_outcome=None,
            last_error_code=None,
            lease_token=None,
            leased_until_ms=None,
            reanalysis_read_ref=read_ref,
            reanalysis_reason=reason.strip(),
            reanalysis_head_ref=None if head_revision is None else identity("update", event_id, head_revision),
            updated_at_ms=now_ms,
        )
        self.jobs.save(row)
        return next_revision

    def semantic_wake_route(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT j.subject_id AS event_id,(j.detail->>'wanted_revision')::integer AS wanted_revision,
                      e.dedupe_family,e.queue_priority,e.trace_id
                 FROM news_jobs j JOIN news_events e ON e.event_id=j.subject_id
                WHERE j.job_kind='semantic' AND j.subject_id=%s AND j.state<>'done'""",
            (event_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def semantic_status(self, *, now_ms: int) -> dict[str, Any]:
        since = now_ms - 24 * 3_600_000
        row = self.conn.execute(SEMANTIC_STATUS_SQL, {"since": since, "now": now_ms}).fetchone()
        values = {key: int(value or 0) for key, value in dict(row or {}).items()}
        codes = self.conn.execute(SEMANTIC_FAILED_CODES_SQL, (since,)).fetchall()
        return {**values, "semantic_failed_by_code_24h": {str(r["code"]): int(r["n"]) for r in codes}}

    def semantic_wake_state(self) -> dict[str, int | None]:
        row = self.conn.execute(SEMANTIC_WAKE_STATE_SQL).fetchone()
        return {
            "pending": int(row["pending"] or 0) if row else 0,
            "expired": int(row["expired"] or 0) if row else 0,
            "oldest_pending_at_ms": None
            if row is None or row["oldest_pending_at_ms"] is None
            else int(row["oldest_pending_at_ms"]),
        }

    def _lineage_job(self, lineage_id: str) -> dict[str, Any] | None:
        candidate = self.conn.execute(
            "SELECT subject_id FROM news_jobs WHERE job_kind='semantic' AND detail->>'lineage_id'=%s", (lineage_id,)
        ).fetchone()
        row = None if candidate is None else self.jobs.lock(str(candidate["subject_id"]))
        return row if row is not None and row["lineage_id"] == lineage_id else None

    def reserve_extra_read(self, *, lineage_id: str, target_ref: str, now_ms: int) -> bool:
        row = self._lineage_job(lineage_id)
        if row is None or row["extra_read_state"] is not None:
            return False
        row.update(extra_read_state="reserved", extra_read_target_ref=target_ref, updated_at_ms=now_ms)
        self.jobs.save(row)
        return True

    def record_read_outcome(self, *, lineage_id: str, outcome: str, now_ms: int) -> bool:
        if outcome not in EXTRA_READ_OUTCOMES:
            raise ValueError("news_extra_read_outcome_invalid")
        row = self._lineage_job(lineage_id)
        if row is None:
            return False
        row.update(extra_read_state=outcome, updated_at_ms=now_ms)
        self.jobs.save(row)
        return True

    def attach_extra_evidence(
        self, *, event_id: str, lineage_id: str, evidence_json: str, focus_claim_refs: Sequence[str], now_ms: int
    ) -> int | None:
        import json

        row = self.jobs.lock(event_id)
        if row is None or row["lineage_id"] != lineage_id:
            return None
        row.update(
            wanted_revision=row["wanted_revision"] + 1,
            attached_evidence=json.loads(evidence_json),
            focus_claim_refs=sorted(set(focus_claim_refs)),
            attempts=0,
            next_attempt_at_ms=now_ms,
            published_at_ms=None,
            updated_at_ms=now_ms,
        )
        self.jobs.save(row)
        return int(row["wanted_revision"])

    def retry_failed_revision(self, *, event_id: str, revision: str, now_ms: int) -> bool:
        wanted = int(revision)
        if wanted < 1:
            raise ValueError("news_retry_work_revision_invalid")
        row = self.jobs.lock(event_id)
        if (
            row is None
            or row["wanted_revision"] != wanted
            or row["state"] != "failed"
            or (row["leased_until_ms"] or 0) > now_ms
        ):
            return False
        row.update(
            attempts=0,
            last_outcome=None,
            lease_token=None,
            leased_until_ms=None,
            failed_read_refs=[],
            next_attempt_at_ms=now_ms,
            published_at_ms=None,
            updated_at_ms=now_ms,
        )
        self.jobs.save(row)
        return True
