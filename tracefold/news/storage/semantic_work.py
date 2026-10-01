"""Semantic input revisions, leases, recovery and lineage read budgets.

Commands use the caller's existing transaction; no external I/O or independent commit.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from ..updates.contracts import SemanticLease
from ..updates.identity import identity
from ..updates.judgment import error_code
from ..updates.projection import reading_views
from .errors import EventUpdateConflict, SemanticLeaseLost
from .semantic_input import frozen_input
from .sql_values import _dumps, _retry_delay

if TYPE_CHECKING:
    from .semantic_input import SemanticInputStorage


SEMANTIC_ATTEMPTS_MAX: Final = 3


SEMANTIC_RETRY_MS: Final = (15_000, 60_000, 300_000)


SEMANTIC_WAKE_STALE_MS: Final = 15_000


EXTRA_READ_OUTCOMES: Final = frozenset({"attached", "no_material", "unavailable_or_budget_exhausted"})


_WAKE_STATE_LIMIT: Final = 1_000


_RUNNABLE: Final = f"attempts < {SEMANTIC_ATTEMPTS_MAX} AND last_outcome IS DISTINCT FROM 'failed'"


SEMANTIC_WAKE_STATE_SQL: Final = f"""
    WITH pending AS MATERIALIZED (
      SELECT attempts, last_outcome, updated_at_ms FROM news_semantic_work
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
        FROM news_semantic_work
       WHERE done_revision IS NULL OR done_revision < wanted_revision
       ORDER BY next_attempt_at_ms, event_id
       LIMIT {_WAKE_STATE_LIMIT}
    )
    SELECT
      (SELECT count(*) FROM news_semantic_observations WHERE completed_at_ms >= %(since)s)
        AS semantic_observations_24h,
      (SELECT count(*) FROM news_event_updates WHERE adopted_at_ms >= %(since)s) AS semantic_adopted_24h,
      (SELECT count(*) FROM news_semantic_work WHERE last_outcome = 'failed' AND updated_at_ms >= %(since)s)
        AS semantic_failed_24h,
      (SELECT count(*) FROM outstanding WHERE {_RUNNABLE}
         AND next_attempt_at_ms <= %(now)s AND (leased_until_ms IS NULL OR leased_until_ms <= %(now)s))
        AS semantic_pending,
      (SELECT count(*) FROM outstanding WHERE {_RUNNABLE}
         AND next_attempt_at_ms > %(now)s) AS semantic_deferred,
      (SELECT count(*) FROM outstanding WHERE leased_until_ms > %(now)s) AS semantic_in_progress,
      (SELECT count(*) FROM outstanding WHERE last_outcome = 'failed') AS semantic_failed_exhausted
"""  # noqa: S608 - code-owned integer constant only


SEMANTIC_FAILED_CODES_SQL: Final = """
    SELECT COALESCE(last_error_code, 'unknown') AS code, count(*) AS n
      FROM news_semantic_work
     WHERE last_outcome = 'failed' AND updated_at_ms >= %s
     GROUP BY 1
"""

log = logging.getLogger("tracefold.news")


class SemanticWorkStorage:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def request_semantic_revision(self, *, event_id: str, lineage_id: str, now_ms: int) -> int:
        """Want one more semantic revision of this Event, in the transaction that appended its evidence.

        The revision counter belongs to semantic work, so an optional read's revision and a new organic
        evidence snapshot can never collide. A new organic revision starts `lineage_id` afresh: its
        attempts, broker wake, one-read budget and attached material are reset.
        """

        row = self.conn.execute(
            """
            INSERT INTO news_semantic_work (
              event_id, wanted_revision, lineage_id, attempts, next_attempt_at_ms, updated_at_ms
            ) VALUES (%s, 1, %s, 0, %s, %s)
            ON CONFLICT (event_id) DO UPDATE SET
              wanted_revision = news_semantic_work.wanted_revision + 1,
              lineage_id = EXCLUDED.lineage_id,
              attempts = 0,
              next_attempt_at_ms = EXCLUDED.next_attempt_at_ms,
              published_at_ms = NULL,
              last_outcome = NULL,
              last_error_code = NULL,
              extra_read_state = NULL,
              extra_read_target_ref = NULL,
              attached_evidence = NULL,
              focus_claim_refs = NULL,
              reanalysis_read_ref = NULL,
              reanalysis_reason = NULL,
              reanalysis_head_ref = NULL,
              updated_at_ms = EXCLUDED.updated_at_ms
            RETURNING wanted_revision
            """,
            (event_id, lineage_id, int(now_ms), int(now_ms)),
        ).fetchone()
        return int(row["wanted_revision"])

    def mark_semantic_work_published(self, *, event_id: str, revision: int, now_ms: int) -> bool:
        """Record the broker wake of one wanted revision; a newer revision keeps its own unwoken marker."""

        cursor = self.conn.execute(
            """
            UPDATE news_semantic_work SET published_at_ms = %s
             WHERE event_id = %s AND wanted_revision = %s
            """,
            (int(now_ms), event_id, int(revision)),
        )
        return bool(cursor.rowcount)

    def claim_semantic_work(
        self, *, event_id: str, lease_token: str, now_ms: int, lease_ms: int, input: SemanticInputStorage
    ) -> SemanticLease | None:
        """Lease due pending work, spending one attempt of its wanted revision.

        The frozen input is read in the same transaction. When the stored material cannot form one (a missing
        body, a changed reanalysis scope, an undecodable head or source), only this revision fails, visibly and
        with its code; the consumer and every other Event keep running.
        """

        row = self.conn.execute(
            f"""
            UPDATE news_semantic_work
               SET attempts = attempts + 1, lease_token = %s, leased_until_ms = %s, updated_at_ms = %s
             WHERE event_id = %s
               AND (done_revision IS NULL OR done_revision < wanted_revision)
               AND {_RUNNABLE}
               AND next_attempt_at_ms <= %s
               AND (leased_until_ms IS NULL OR leased_until_ms <= %s)
            RETURNING event_id, wanted_revision, lineage_id, lease_token, attempts
            """,  # noqa: S608 - code-owned predicate only
            (lease_token, int(now_ms) + int(lease_ms), int(now_ms), event_id, int(now_ms), int(now_ms)),
        ).fetchone()
        if row is None:
            return None
        try:
            source = frozen_input(event_id, input.semantic_input_material(event_id, now_ms=now_ms))
        except (LookupError, ValueError) as exc:
            # EventUpdateConflict and pydantic's ValidationError are ValueErrors.
            code = error_code(exc, default="news_semantic_input_invalid")
            log.warning("news semantic input failed event_id=%s code=%s", event_id, code)
            self.conn.execute(
                "UPDATE news_semantic_work SET lease_token=NULL, leased_until_ms=NULL, last_outcome='failed',"
                " last_error_code=%s, updated_at_ms=%s WHERE event_id=%s",
                (code, int(now_ms), event_id),
            )
            return None
        # The task reads this attempt is given: a crashed final attempt is quarantined by exactly these.
        self.conn.execute(
            "UPDATE news_semantic_work SET attempt_read_refs=%s WHERE event_id=%s",
            ([view.read_ref for view in reading_views(source)], event_id),
        )
        return SemanticLease(source=source, lease_token=str(row["lease_token"]), attempts=int(row["attempts"]))

    def require_semantic_owner(self, lease: SemanticLease, *, now_ms: int) -> Mapping[str, Any]:
        row = self.conn.execute(
            "SELECT wanted_revision, attempts FROM news_semantic_work "
            "WHERE event_id=%s AND lease_token=%s AND leased_until_ms>%s FOR UPDATE",
            (lease.event_id, lease.lease_token, int(now_ms)),
        ).fetchone()
        if row is None:
            raise SemanticLeaseLost("news_semantic_lease_lost")
        return dict(row)

    def defer_semantic_event(self, *, lease: SemanticLease, reason: str, now_ms: int, retry_after_ms: int = 0) -> bool:
        """Settle only this input's retry budget; newer evidence remains due."""
        return self._end_semantic_attempt(
            lease, reason=reason, now_ms=now_ms, retry_after_ms=retry_after_ms, failed=False
        )

    def fail_semantic_event(self, *, lease: SemanticLease, error_code: str, now_ms: int) -> bool:
        return self._end_semantic_attempt(lease, reason=error_code, now_ms=now_ms, failed=True)

    def _end_semantic_attempt(
        self, lease: SemanticLease, *, reason: str, now_ms: int, failed: bool, retry_after_ms: int = 0
    ) -> bool:
        """Settle one attempt. A revision that ends failed keeps its real attempt count and quarantines the
        task reads it was given: later revisions read only newer material, and the failed reads stay listed
        for an exact reanalysis."""

        try:
            row = self.require_semantic_owner(lease, now_ms=now_ms)
        except SemanticLeaseLost:
            return False
        attempts = int(row["attempts"])
        failed = failed or attempts >= SEMANTIC_ATTEMPTS_MAX
        quarantined = [view.read_ref for view in reading_views(lease.source)] if failed else []
        newer = int(row["wanted_revision"]) > lease.wanted_revision
        if newer:
            self.conn.execute(
                "UPDATE news_semantic_work SET lease_token=NULL, leased_until_ms=NULL,"
                " failed_read_refs=ARRAY(SELECT DISTINCT ref FROM unnest(failed_read_refs || %s::text[]) AS ref)"
                " WHERE event_id=%s",
                (quarantined, lease.event_id),
            )
        else:
            self.conn.execute(
                "UPDATE news_semantic_work SET lease_token=NULL, leased_until_ms=NULL, last_outcome=%s,"
                " last_error_code=%s, next_attempt_at_ms=%s, updated_at_ms=%s,"
                " failed_read_refs=ARRAY(SELECT DISTINCT ref FROM unnest(failed_read_refs || %s::text[]) AS ref)"
                " WHERE event_id=%s",
                (
                    "failed" if failed else reason,
                    reason,
                    int(now_ms) + max(retry_after_ms, _retry_delay(SEMANTIC_RETRY_MS, attempts)),
                    int(now_ms),
                    quarantined,
                    lease.event_id,
                ),
            )
        return True

    def finish_semantic_work(self, *, work_id: str, lease: SemanticLease, reason: str, now_ms: int) -> bool:
        row = self.require_semantic_owner(lease, now_ms=now_ms)
        observed = self._observed_work(work_id)
        if observed["event_id"] != lease.event_id or observed["input_revision"] != lease.wanted_revision:
            raise EventUpdateConflict("news_semantic_observation_lease_mismatch")
        current = int(row["wanted_revision"]) == lease.wanted_revision
        cursor = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET done_revision = GREATEST(COALESCE(done_revision, 0), %s),
                   processed_read_refs = ARRAY(
                       SELECT DISTINCT ref FROM unnest(processed_read_refs || %s::text[]) AS ref
                   ),
                   failed_read_refs = ARRAY(
                       SELECT ref FROM unnest(failed_read_refs) AS ref WHERE ref <> ALL(%s::text[])
                   ),
                   reanalysis_read_ref = CASE WHEN %s THEN NULL ELSE reanalysis_read_ref END,
                   reanalysis_reason = CASE WHEN %s THEN NULL ELSE reanalysis_reason END,
                   reanalysis_head_ref = CASE WHEN %s THEN NULL ELSE reanalysis_head_ref END,
                   attempts = CASE WHEN %s THEN 0 ELSE attempts END,
                   lease_token = NULL, leased_until_ms = NULL,
                   last_outcome = CASE WHEN %s THEN %s ELSE last_outcome END,
                   last_error_code = CASE WHEN %s THEN NULL ELSE last_error_code END,
                   next_attempt_at_ms = CASE WHEN %s THEN %s ELSE next_attempt_at_ms END,
                   updated_at_ms = %s
             WHERE event_id = %s
            """,
            (
                observed["input_revision"],
                list(observed["read_refs"]),
                list(observed["read_refs"]),
                current,
                current,
                current,
                current,
                current,
                reason,
                current,
                current,
                int(now_ms),
                int(now_ms),
                lease.event_id,
            ),
        )
        return bool(cursor.rowcount)

    def _observed_work(self, work_id: str) -> Mapping[str, Any]:
        # The port addresses work by its code-owned identity; its Event and input revision are the ones
        # the observation of that work recorded. Every service path saves one before finishing.
        rows = self.conn.execute(
            """
            SELECT event_id, input_revision, read_refs
              FROM news_semantic_observations WHERE work_id = %s
            """,
            (work_id,),
        ).fetchall()
        if not rows or len({str(row["event_id"]) for row in rows}) != 1:
            raise LookupError("news_semantic_work_unknown")
        return {
            "event_id": str(rows[0]["event_id"]),
            "input_revision": max(int(row["input_revision"]) for row in rows),
            "read_refs": tuple({ref for row in rows for ref in row["read_refs"]}),
        }

    def terminalize_exhausted_semantic_work(self, *, now_ms: int, limit: int) -> int:
        """The Janitor settles a crashed final attempt only after its lease expires.

        Like any failed revision, it quarantines the task reads that attempt was given, and only those: a member
        that joined after the attempt froze its input stays unread and is read by the next revision.
        """

        cursor = self.conn.execute(
            """
            WITH expired AS (
              SELECT event_id FROM news_semantic_work
               WHERE (done_revision IS NULL OR done_revision < wanted_revision)
                 AND attempts >= %s AND last_outcome IS DISTINCT FROM 'failed'
                 AND (leased_until_ms IS NULL OR leased_until_ms <= %s)
               ORDER BY updated_at_ms, event_id LIMIT %s FOR UPDATE SKIP LOCKED
            )
            UPDATE news_semantic_work w
               SET last_outcome = 'failed', last_error_code = 'news_semantic_attempts_exhausted_after_lease',
                   failed_read_refs = ARRAY(
                       SELECT DISTINCT ref FROM unnest(w.failed_read_refs || w.attempt_read_refs) AS ref
                   ),
                   lease_token = NULL, leased_until_ms = NULL, updated_at_ms = %s
              FROM expired WHERE w.event_id = expired.event_id
            """,
            (SEMANTIC_ATTEMPTS_MAX, int(now_ms), int(limit), int(now_ms)),
        )
        return int(cursor.rowcount)

    def pending_semantic_event_ids(self, *, now_ms: int, limit: int) -> list[str]:
        rows = self.conn.execute(
            f"""
            SELECT event_id FROM news_semantic_work
             WHERE (done_revision IS NULL OR done_revision < wanted_revision)
               AND {_RUNNABLE}
               AND next_attempt_at_ms <= %s
               AND (leased_until_ms IS NULL OR leased_until_ms <= %s)
               AND (published_at_ms IS NULL OR published_at_ms <= %s)
             ORDER BY next_attempt_at_ms, event_id
             LIMIT %s
            """,  # noqa: S608 - code-owned predicate only
            (int(now_ms), int(now_ms), int(now_ms) - SEMANTIC_WAKE_STALE_MS, int(limit)),
        ).fetchall()
        return [str(row["event_id"]) for row in rows]

    def semantic_work(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM news_semantic_work WHERE event_id = %s", (event_id,)).fetchone()
        return None if row is None else dict(row)

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
        """Open one system revision for one inspected task view, under version CAS.

        The inspected revision must be settled: done, or failed. Reanalysing a failed revision reads exactly
        the named (possibly quarantined) task view again; the rest of the quarantine stays in place.
        """

        if not reason.strip() or not read_ref:
            raise ValueError("news_reanalysis_target_or_reason_missing")
        row = self.conn.execute(
            "SELECT wanted_revision, done_revision, leased_until_ms, last_outcome FROM news_semantic_work "
            "WHERE event_id=%s FOR UPDATE",
            (event_id,),
        ).fetchone()
        if row is None:
            raise LookupError("news_reanalysis_event_work_missing")
        settled = row["done_revision"] == expected_wanted_revision or row["last_outcome"] == "failed"
        if int(row["wanted_revision"]) != expected_wanted_revision or not settled:
            raise EventUpdateConflict("news_reanalysis_wanted_revision_changed_or_incomplete")
        if row["leased_until_ms"] is not None and int(row["leased_until_ms"]) > now_ms:
            raise EventUpdateConflict("news_reanalysis_lease_active")
        current_head = self.conn.execute(
            "SELECT content_revision FROM news_event_update_heads WHERE event_id=%s", (event_id,)
        ).fetchone()
        head_revision = None if current_head is None else str(current_head["content_revision"])
        if head_revision != expected_head_revision:
            raise EventUpdateConflict("news_reanalysis_head_changed")
        listing = self.reanalysis_scope_list(event_id=event_id, now_ms=now_ms, input=input)
        if read_ref not in {entry["read_ref"] for entry in listing["scopes"]}:
            raise EventUpdateConflict("news_reanalysis_read_scope_changed")
        next_revision = expected_wanted_revision + 1
        self.conn.execute(
            """
            UPDATE news_semantic_work
               SET wanted_revision=%s, lineage_id=%s, attempts=0, next_attempt_at_ms=%s,
                   published_at_ms=NULL, last_outcome=NULL, last_error_code=NULL,
                   lease_token=NULL, leased_until_ms=NULL,
                   reanalysis_read_ref=%s, reanalysis_reason=%s, reanalysis_head_ref=%s,
                   updated_at_ms=%s
             WHERE event_id=%s
            """,
            (
                next_revision,
                identity("lineage_reanalysis", event_id, next_revision, read_ref),
                int(now_ms),
                read_ref,
                reason.strip(),
                None if head_revision is None else identity("update", event_id, head_revision),
                int(now_ms),
                event_id,
            ),
        )
        return next_revision

    def semantic_wake_route(self, event_id: str) -> dict[str, Any] | None:
        """What a broker wake of pending semantic work carries: its revision and its routing key parts."""

        row = self.conn.execute(
            """
            SELECT w.event_id, w.wanted_revision, e.dedupe_family, e.queue_priority, e.trace_id
              FROM news_semantic_work w JOIN news_events e ON e.event_id = w.event_id
             WHERE w.event_id = %s AND (w.done_revision IS NULL OR w.done_revision < w.wanted_revision)
            """,
            (event_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def semantic_status(self, *, now_ms: int) -> dict[str, Any]:
        """The semantic stage's last 24 h, for the status page's model health."""

        since = int(now_ms) - 24 * 3_600_000
        row = self.conn.execute(SEMANTIC_STATUS_SQL, {"since": since, "now": int(now_ms)}).fetchone()
        codes = self.conn.execute(SEMANTIC_FAILED_CODES_SQL, (since,)).fetchall()
        values = {key: int(value or 0) for key, value in dict(row or {}).items()}
        return {
            "semantic_observations_24h": values.get("semantic_observations_24h", 0),
            "semantic_adopted_24h": values.get("semantic_adopted_24h", 0),
            "semantic_failed_24h": values.get("semantic_failed_24h", 0),
            "semantic_pending": values.get("semantic_pending", 0),
            "semantic_deferred": values.get("semantic_deferred", 0),
            "semantic_in_progress": values.get("semantic_in_progress", 0),
            "semantic_failed_exhausted": values.get("semantic_failed_exhausted", 0),
            "semantic_failed_by_code_24h": {str(r["code"]): int(r["n"]) for r in codes},
        }

    def semantic_wake_state(self) -> dict[str, int | None]:
        """Bounded pending/exhausted semantic work for maintenance telemetry."""

        row = self.conn.execute(SEMANTIC_WAKE_STATE_SQL).fetchone()
        return {
            "pending": int(row["pending"] or 0) if row else 0,
            "oldest_pending_at_ms": None
            if row is None or row["oldest_pending_at_ms"] is None
            else int(row["oldest_pending_at_ms"]),
            "expired": int(row["expired"] or 0) if row else 0,
        }

    def reserve_extra_read(self, *, lineage_id: str, target_ref: str, now_ms: int) -> bool:
        row = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET extra_read_state = 'reserved', extra_read_target_ref = %s, updated_at_ms = %s
             WHERE lineage_id = %s AND extra_read_state IS NULL
            RETURNING event_id
            """,
            (target_ref, int(now_ms), lineage_id),
        ).fetchone()
        return row is not None

    def record_read_outcome(self, *, lineage_id: str, outcome: str, now_ms: int) -> bool:
        if outcome not in EXTRA_READ_OUTCOMES:
            raise ValueError("news_extra_read_outcome_invalid")
        cursor = self.conn.execute(
            "UPDATE news_semantic_work SET extra_read_state = %s, updated_at_ms = %s WHERE lineage_id = %s",
            (outcome, int(now_ms), lineage_id),
        )
        return bool(cursor.rowcount)

    def attach_extra_evidence(
        self,
        *,
        event_id: str,
        lineage_id: str,
        evidence_json: str,
        focus_claim_refs: Sequence[str],
        now_ms: int,
    ) -> int | None:
        """Want one more revision of the same lineage whose input is only the attached material."""

        row = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET wanted_revision = wanted_revision + 1,
                   attached_evidence = %s::jsonb, focus_claim_refs = %s::jsonb,
                   attempts = 0, next_attempt_at_ms = %s, published_at_ms = NULL, updated_at_ms = %s
             WHERE event_id = %s AND lineage_id = %s
            RETURNING wanted_revision
            """,
            (evidence_json, _dumps(sorted(set(focus_claim_refs))), int(now_ms), int(now_ms), event_id, lineage_id),
        ).fetchone()
        return None if row is None else int(row["wanted_revision"])

    def retry_failed_revision(self, *, event_id: str, revision: str, now_ms: int) -> bool:
        """Reopen only the requested visibly failed semantic input revision."""

        wanted = int(revision)
        if wanted < 1:
            raise ValueError("news_retry_work_revision_invalid")
        cursor = self.conn.execute(
            """
            UPDATE news_semantic_work
               SET attempts = 0, last_outcome = NULL, lease_token = NULL, leased_until_ms = NULL,
                   failed_read_refs = '{}', next_attempt_at_ms = %s, published_at_ms = NULL, updated_at_ms = %s
             WHERE event_id = %s AND wanted_revision = %s AND last_outcome = 'failed'
               AND (done_revision IS NULL OR done_revision < wanted_revision)
               AND (leased_until_ms IS NULL OR leased_until_ms <= %s)
            """,
            (int(now_ms), int(now_ms), event_id, wanted, int(now_ms)),
        )
        return bool(cursor.rowcount)
