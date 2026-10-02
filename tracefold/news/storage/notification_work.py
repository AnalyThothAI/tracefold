"""Immutable notification decisions, intent ownership and bounded work recovery.

Commands use the caller's existing transaction; no external I/O or independent commit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from ..notifications.contracts import NEWS_CHANNEL, NOTIFICATION_ATTEMPTS_MAX, NotificationPlan
from .errors import EventUpdateConflict, IntentLeaseLost
from .notification_context import NotificationContextStorage
from .notification_jobs import NotificationJobDetail
from .reader_check import ReaderCheck, reader_unchanged
from .sql_values import _dumps
from .update_commit import lock_event

NOTIFICATION_RETRY_MS: Final = (30_000, 120_000)


NOTIFICATION_WAIT_MS: Final = 30_000


INTENT_ATTEMPTS_MAX: Final = 3


INTENT_RETRY_MS: Final = (30_000, 120_000, 600_000)


INTENT_LEASE_MS: Final = 120_000


IntentOutcome = Literal["sent", "not_sent", "ambiguous"]


class NotificationWorkStorage:
    def __init__(self, conn: Any, *, context: NotificationContextStorage) -> None:
        self.conn = conn
        self.context = context

    def _record_decision(
        self,
        event_id: str,
        plan: NotificationPlan,
        plan_json: str,
        *,
        now_ms: int,
        recall_diagnostics: Mapping[str, Mapping[str, Any]] | None,
    ) -> NotificationPlan:
        # Validate the same immutable judgment that is persisted.
        if NotificationPlan.model_validate_json(plan_json) != plan:
            raise EventUpdateConflict("news_notification_plan_mismatch")
        self.conn.execute(
            """INSERT INTO news_notifications
                 (notification_id,kind,origin,event_id,update_ref,input_digest,input_snapshot,plan,
                  decided_at_ms,state,created_at_ms,updated_at_ms)
               VALUES (%s,'update','reader_v2',%s,%s,%s,%s::jsonb,%s::jsonb,%s,'decided',%s,%s)
               ON CONFLICT DO NOTHING""",
            (
                plan.record_ref,
                event_id,
                plan.update_ref,
                plan.input_digest,
                _dumps(
                    {
                        "reader_identity": plan.reader_identity,
                        "compared_receipts": [r.model_dump(mode="json") for r in plan.compared_receipts],
                        "recall": {} if recall_diagnostics is None else dict(recall_diagnostics),
                    }
                ),
                plan_json,
                int(now_ms),
                int(now_ms),
                int(now_ms),
            ),
        )
        stored = self.conn.execute(
            "SELECT notification_id,plan FROM news_notifications WHERE notification_id=%s",
            (plan.record_ref,),
        ).fetchone()
        if stored is None:
            raise EventUpdateConflict("news_notification_decision_missing")
        return NotificationPlan.model_validate(stored["plan"]).model_copy(
            update={"reader_revision": plan.reader_revision, "decision_ref": str(stored["notification_id"])}
        )

    def record_notification_plan(
        self,
        *,
        plan: NotificationPlan,
        plan_json: str,
        lease_token: str,
        now_ms: int,
        check: ReaderCheck,
        lease_ms: int = INTENT_LEASE_MS,
        recall_diagnostics: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        head = self.conn.execute(
            "SELECT event_id,content_revision FROM news_analyses WHERE update_ref=%s AND adopted_at_ms IS NOT NULL",
            (plan.update_ref,),
        ).fetchone()
        if head is None:
            return {"status": "head_changed"}
        event_id = str(head["event_id"])
        lock_event(self.conn, event_id)
        head = self.conn.execute(
            "SELECT a.content_revision FROM news_events e JOIN news_analyses a ON "
            "a.analysis_id=e.current_analysis_id WHERE e.event_id=%s AND a.update_ref=%s",
            (event_id, plan.update_ref),
        ).fetchone()
        work = self.conn.execute(
            "SELECT state,attempts,detail FROM news_jobs WHERE job_kind='notify' AND subject_id=%s"
            " FOR UPDATE SKIP LOCKED",
            (event_id,),
        ).fetchone()
        if head is None or work is None or work["detail"]["content_revision"] != head["content_revision"]:
            return {"status": "head_changed"}
        if work["state"] != "pending":
            return {"status": "already_settled"}
        plan = self._record_decision(event_id, plan, plan_json, now_ms=now_ms, recall_diagnostics=recall_diagnostics)
        if (
            check.event_id != event_id
            or check.revision != plan.reader_revision
            or not reader_unchanged(self.conn, check)
        ):
            return {"status": "reader_changed"}
        intent_id = plan.intent_id if plan.action == "notify" else None
        others = self.conn.execute(
            """SELECT notification_id,intent_id,lease_token,lease_until_ms FROM news_notifications
               WHERE event_id=%s AND kind='update' AND state='pending'
                 AND intent_id IS DISTINCT FROM %s FOR UPDATE""",
            (event_id, intent_id),
        ).fetchall()
        leased = [
            int(r["lease_until_ms"])
            for r in others
            if r["lease_token"] is not None and int(r["lease_until_ms"]) > now_ms
        ]
        if leased:
            self._postpone_work(event_id, next_at_ms=min(leased))
            return {"status": "overlap"}
        for row in others:
            self._clear_reservation(str(row["notification_id"]), now_ms=now_ms)
        attempts = int(work["attempts"])
        result = {"status": "committed", "plan": plan.model_dump(mode="json"), "intent_id": None, "frozen_card": None}
        if plan.action == "no_notification":
            self._settle_work(event_id, plan, state="done", attempts=0, next_at_ms=now_ms, now_ms=now_ms)
            return result
        if plan.action == "unresolved":
            self._wait_work(event_id, plan, attempts=attempts, now_ms=now_ms)
            return result
        existing = self.conn.execute(
            "SELECT * FROM news_notifications WHERE intent_id=%s FOR UPDATE",
            (intent_id,),
        ).fetchone()
        if existing and existing["state"] in ("sending", "sent", "ambiguous", "terminal"):
            if existing["state"] == "sending":
                self._wait_work(event_id, plan, attempts=attempts, now_ms=now_ms)
            else:
                self._complete_plan(event_id, plan, attempts=attempts, now_ms=now_ms)
            return {"status": "already_settled"}
        if existing and existing["state"] == "dead":
            self._settle_work(
                event_id,
                plan,
                state="failed",
                attempts=attempts,
                next_at_ms=now_ms,
                now_ms=now_ms,
                error_code=str(existing["error_code"] or "news_delivery_intent_dead"),
            )
            return {"status": "already_settled"}
        if existing and existing["lease_token"] is not None and int(existing["lease_until_ms"]) > now_ms:
            return {"status": "overlap"}
        frozen = None if existing is None else existing["card"]
        if existing and existing["notification_id"] != plan.record_ref:
            self._clear_reservation(str(existing["notification_id"]), now_ms=now_ms)
        self.conn.execute(
            """UPDATE news_notifications SET state='pending',intent_id=%s,content_revision=%s,
                 claim_refs=%s::jsonb,plan_key=%s,lease_token=%s,lease_until_ms=%s,next_attempt_at_ms=%s,
                 reserved_at_ms=COALESCE(reserved_at_ms,%s),last_attempt_at_ms=%s,updated_at_ms=%s,
                 attempts=%s,card=%s::jsonb,card_copy_input_digest=%s,card_copy_document=%s::jsonb,
                 settlement=%s::jsonb,error_code=%s
               WHERE notification_id=%s AND state IN ('decided','pending')""",
            (
                intent_id,
                head["content_revision"],
                _dumps(list(plan.selected_claim_refs)),
                plan.key,
                lease_token,
                now_ms + lease_ms,
                now_ms + lease_ms,
                now_ms if existing is None else existing["reserved_at_ms"],
                now_ms,
                now_ms,
                0 if existing is None else existing["attempts"],
                None if frozen is None else _dumps(frozen),
                None if existing is None else existing["card_copy_input_digest"],
                None
                if existing is None or existing["card_copy_document"] is None
                else _dumps(existing["card_copy_document"]),
                None if existing is None or existing["settlement"] is None else _dumps(existing["settlement"]),
                None if existing is None else existing["error_code"],
                plan.record_ref,
            ),
        )
        self._settle_work(
            event_id, plan, state="pending", attempts=attempts, next_at_ms=now_ms + lease_ms, now_ms=now_ms
        )
        result.update(intent_id=intent_id, frozen_card=frozen)
        return result

    def _clear_reservation(self, notification_id: str, *, now_ms: int) -> None:
        self.conn.execute(
            """UPDATE news_notifications SET state='decided',intent_id=NULL,content_revision=NULL,
                 claim_refs=NULL,plan_key=NULL,attempts=0,next_attempt_at_ms=NULL,lease_token=NULL,lease_until_ms=NULL,
                 card=NULL,card_copy_input_digest=NULL,card_copy_document=NULL,settlement=NULL,error_code=NULL,
                 reserved_at_ms=NULL,last_attempt_at_ms=NULL,updated_at_ms=%s
               WHERE notification_id=%s AND state='pending'""",
            (now_ms, notification_id),
        )

    def _complete_plan(self, event_id: str, plan: NotificationPlan, *, attempts: int, now_ms: int) -> None:
        # Deferred claims keep the marker waiting for a later turn; otherwise this head is planned.
        if plan.deferred_claim_refs:
            self._wait_work(event_id, plan, attempts=attempts, now_ms=now_ms)
        else:
            self._settle_work(event_id, plan, state="done", attempts=0, next_at_ms=now_ms, now_ms=now_ms)

    def intent_ended(
        self,
        event_id: str,
        content_revision: str,
        decision_ref: str | None,
        *,
        now_ms: int,
        error_code: str | None = None,
    ) -> None:
        """An intent reached its end: it completes -- or, with an error code, fails -- the plan it was sent for.

        Only the work's current decision is completed by its own intent. Any other intent ending wakes the
        Event's pending work instead, which may be a newer head waiting for exactly this send to finish.
        """

        work = self.conn.execute(
            """SELECT attempts,state,detail->>'content_revision' AS content_revision,
                      detail->>'decision_ref' AS decision_ref,
                      (SELECT plan FROM news_notifications WHERE notification_id=detail->>'decision_ref') AS plan
               FROM news_jobs WHERE job_kind='notify' AND subject_id=%s FOR UPDATE""",
            (event_id,),
        ).fetchone()
        if work is None or work["state"] != "pending":
            return
        if work["content_revision"] != content_revision or work["plan"] is None or work["decision_ref"] != decision_ref:
            self._wake_work(event_id, now_ms=now_ms)
            return
        plan = NotificationPlan.model_validate(work["plan"]).model_copy(
            update={"decision_ref": str(work["decision_ref"])}
        )
        if error_code is None:
            self._complete_plan(event_id, plan, attempts=int(work["attempts"]), now_ms=now_ms)
        else:
            self._settle_work(
                event_id,
                plan,
                state="failed",
                attempts=int(work["attempts"]),
                next_at_ms=now_ms,
                now_ms=now_ms,
                error_code=error_code,
            )

    def _wait_work(self, event_id: str, plan: NotificationPlan, *, attempts: int, now_ms: int) -> None:
        """Wait for this Event's send, a linked send, or unavailable reader evidence without an attempt.

        Polling observes send settlement; reconciliation holds a lost owner's send ambiguous within
        `SENDING_ORPHAN_MS`. Policy stops waiting for reader evidence ten minutes after adoption.
        """

        self._settle_work(
            event_id,
            plan,
            state="pending",
            attempts=attempts,
            next_at_ms=int(now_ms) + NOTIFICATION_WAIT_MS,
            now_ms=now_ms,
        )

    def _settle_work(
        self,
        event_id: str,
        plan: NotificationPlan,
        *,
        state: str,
        attempts: int,
        next_at_ms: int,
        now_ms: int,
        error_code: str | None = None,
    ) -> None:
        detail = NotificationJobDetail(
            content_revision=self.conn.execute(
                "SELECT detail->>'content_revision' AS revision FROM news_jobs WHERE job_kind='notify' "
                " AND subject_id=%s",
                (event_id,),
            ).fetchone()["revision"],
            reader_revision=plan.reader_revision,
            decision_ref=plan.record_ref,
        )
        self.conn.execute(
            """UPDATE news_jobs SET state=%s,detail=%s::jsonb,attempts=%s,
                 last_error_code=CASE WHEN %s='done' THEN NULL ELSE COALESCE(%s,last_error_code) END,
                 next_attempt_at_ms=%s,updated_at_ms=%s WHERE job_kind='notify' AND subject_id=%s""",
            (state, detail.model_dump_json(), attempts, state, error_code, next_at_ms, now_ms, event_id),
        )

    def _postpone_work(self, event_id: str, *, next_at_ms: int) -> None:
        """Move the Event's pending work to `next_at_ms` without touching its plan, attempts or CAS stamp."""

        self.conn.execute(
            "UPDATE news_jobs SET next_attempt_at_ms = %s "
            "WHERE subject_id = %s AND job_kind = CASE WHEN %s='news' THEN 'notify' END AND state = 'pending'",
            (int(next_at_ms), event_id, NEWS_CHANNEL),
        )

    def _wake_work(self, event_id: str, *, now_ms: int) -> None:
        """Make the Event's pending work due now, if it was waiting; nothing else about it changes."""

        self.conn.execute(
            "UPDATE news_jobs SET next_attempt_at_ms = %s "
            "WHERE subject_id = %s AND job_kind = CASE WHEN %s='news' THEN 'notify' END "
            " AND state = 'pending' AND next_attempt_at_ms > %s",
            (int(now_ms), event_id, NEWS_CHANNEL, int(now_ms)),
        )

    def pend_notification(self, event_id: str, *, expected_content_revision: str, next_at_ms: int, now_ms: int) -> None:
        """An intent of this revision is owed again at `next_at_ms`; a newer head's work is woken now."""

        updated = self.conn.execute(
            """
            UPDATE news_jobs SET state = 'pending', next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE subject_id = %s AND job_kind = CASE WHEN %s='news' THEN 'notify' END AND
        detail->>'content_revision' = %s AND state <> 'failed'
            """,
            (int(next_at_ms), int(now_ms), event_id, NEWS_CHANNEL, expected_content_revision),
        )
        if not updated.rowcount:
            self._wake_work(event_id, now_ms=now_ms)

    def lookup_card_copy(self, *, input_digest: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT card_copy_document FROM news_notifications WHERE kind='update'
               AND card_copy_input_digest=%s AND card_copy_document IS NOT NULL
               ORDER BY COALESCE(attempted_at_ms,updated_at_ms) DESC LIMIT 1""",
            (input_digest,),
        ).fetchone()
        return None if row is None else dict(row["card_copy_document"])

    def save_intent_card(
        self, *, intent_id: str, lease_token: str, card_json: str, copy_json: str, input_digest: str, now_ms: int
    ) -> dict[str, Any]:
        """Fenced insert-only frozen payload: an existing frozen card wins and is returned."""

        self.conn.execute(
            """
            UPDATE news_notifications
               SET card = %s::jsonb, card_copy_document=%s::jsonb,
                   card_copy_input_digest=%s, updated_at_ms = %s
             WHERE intent_id = %s AND kind = 'update' AND state = 'pending'
               AND lease_token = %s AND card IS NULL
            """,
            (card_json, copy_json, input_digest, int(now_ms), intent_id, lease_token),
        )
        row = self.conn.execute(
            "SELECT lease_token, state, card FROM news_notifications WHERE intent_id = %s", (intent_id,)
        ).fetchone()
        if row is None or row["state"] != "pending" or row["lease_token"] != lease_token or row["card"] is None:
            raise IntentLeaseLost("news_intent_lease_lost")
        return dict(row["card"])

    def release_unsent_intent(self, *, intent_id: str, lease_token: str, now_ms: int) -> bool:
        identity_row = self.conn.execute(
            "SELECT event_id FROM news_notifications WHERE intent_id=%s AND kind='update'", (intent_id,)
        ).fetchone()
        if identity_row is None:
            return False
        lock_event(self.conn, str(identity_row["event_id"]))
        row = self.conn.execute(
            """UPDATE news_notifications SET lease_token=NULL, lease_until_ms=NULL,
        next_attempt_at_ms=%s, updated_at_ms=%s
                WHERE intent_id=%s AND kind='update' AND state='pending' AND lease_token=%s
                RETURNING event_id,content_revision""",
            (int(now_ms), int(now_ms), intent_id, lease_token),
        ).fetchone()
        if row is None:
            return False
        self.pend_notification(
            str(row["event_id"]),
            expected_content_revision=str(row["content_revision"]),
            next_at_ms=now_ms,
            now_ms=now_ms,
        )
        return True

    def fail_unsent_intent(
        self,
        intent_id: str,
        *,
        error_code: str,
        retryable: bool,
        retry_after_ms: int | None,
        now_ms: int,
        lease_token: str | None = None,
        settlement: Mapping[str, Any] | None = None,
    ) -> bool:
        """One failure of an owned intent that never reached a reader: back off, or end it and fail the work."""

        row = self.conn.execute(
            """
            UPDATE news_notifications
               SET attempts = CASE WHEN %(retryable)s THEN LEAST(attempts + 1, %(max)s) ELSE %(max)s END,
                   state = CASE WHEN %(retryable)s AND attempts + 1 < %(max)s THEN 'pending' ELSE 'dead' END,
                   settled_at_ms = CASE WHEN %(retryable)s AND attempts + 1 < %(max)s
                                        THEN NULL ELSE %(now)s::bigint END,
                   next_attempt_at_ms = %(now)s::bigint + GREATEST(
                     (%(delays)s::bigint[])[GREATEST(1, LEAST(attempts + 1, %(delay_n)s))], %(retry_after)s::bigint),
                   lease_token = NULL, lease_until_ms=NULL, error_code = %(code)s, updated_at_ms = %(now)s,
                   settlement = COALESCE(%(settlement)s::jsonb, settlement)
             WHERE intent_id = %(intent)s AND kind = 'update'
               AND (state='pending' OR (state='sending' AND %(settlement)s::jsonb IS NOT NULL))
               AND (%(lease)s::text IS NULL OR lease_token = %(lease)s)
            RETURNING event_id, state, next_attempt_at_ms, content_revision, notification_id AS decision_ref
            """,
            {
                "retryable": bool(retryable),
                "max": INTENT_ATTEMPTS_MAX,
                "now": int(now_ms),
                "delays": list(INTENT_RETRY_MS),
                "delay_n": len(INTENT_RETRY_MS),
                "retry_after": int(retry_after_ms or 0),
                "code": error_code,
                "settlement": None if settlement is None else _dumps(settlement),
                "intent": intent_id,
                "lease": lease_token,
            },
        ).fetchone()
        if row is None:
            return False
        if row["state"] == "pending":
            self.pend_notification(
                str(row["event_id"]),
                expected_content_revision=str(row["content_revision"]),
                next_at_ms=int(row["next_attempt_at_ms"]),
                now_ms=now_ms,
            )
        else:
            self.intent_ended(
                str(row["event_id"]),
                str(row["content_revision"]),
                row["decision_ref"],
                now_ms=now_ms,
                error_code=error_code,
            )
        return True

    def record_unsent_intent_failure(
        self,
        *,
        intent_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        retry_after_ms: int | None,
        now_ms: int,
    ) -> bool:
        """A card failure or a proven unsent preflight: fenced by the lease, never through a `sending` row."""

        identity_row = self.conn.execute(
            "SELECT event_id FROM news_notifications WHERE intent_id=%s AND kind='update'", (intent_id,)
        ).fetchone()
        if identity_row is None:
            return False
        lock_event(self.conn, str(identity_row["event_id"]))
        return self.fail_unsent_intent(
            intent_id,
            error_code=error_code,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
            now_ms=now_ms,
            lease_token=lease_token,
        )

    def defer_notification_work(
        self,
        *,
        event_id: str,
        channel: str,
        expected_content_revision: str | None,
        expected_work_updated_at_ms: int | None = None,
        error_code: str,
        now_ms: int,
    ) -> bool:
        """Spend one attempt of the failed snapshot's work; the last one fails it. A superseded turn is a no-op."""

        lock_event(self.conn, event_id)
        cursor = self.conn.execute(
            """
            UPDATE news_jobs
               SET attempts = LEAST(attempts + 1, %(max)s),
                   state = CASE WHEN attempts + 1 >= %(max)s THEN 'failed' ELSE 'pending' END,
                   last_error_code = %(code)s,
                   next_attempt_at_ms = CASE WHEN attempts + 1 >= %(max)s THEN next_attempt_at_ms
                     ELSE %(now)s::bigint + (%(delays)s::bigint[])[attempts + 1] END,
                   updated_at_ms = GREATEST(%(now)s, updated_at_ms + 1)
             WHERE subject_id = %(event)s AND job_kind = CASE WHEN %(channel)s='news' THEN 'notify' END AND
        state = 'pending'
               AND (%(revision)s::text IS NULL OR detail->>'content_revision' = %(revision)s)
               AND (%(updated)s::bigint IS NULL OR updated_at_ms = %(updated)s)
            """,
            {
                "max": NOTIFICATION_ATTEMPTS_MAX,
                "code": error_code,
                "now": int(now_ms),
                "delays": list(NOTIFICATION_RETRY_MS),
                "event": event_id,
                "channel": channel,
                "revision": expected_content_revision,
                "updated": expected_work_updated_at_ms,
            },
        )
        return bool(cursor.rowcount)

    def postpone_notification_work(
        self, *, event_id: str, channel: str, expected_content_revision: str | None, now_ms: int
    ) -> bool:
        """Put pending work off by one wait without spending an attempt or moving its CAS stamp."""

        lock_event(self.conn, event_id)
        cursor = self.conn.execute(
            """
            UPDATE news_jobs SET next_attempt_at_ms = GREATEST(next_attempt_at_ms, %s)
             WHERE subject_id = %s AND job_kind = CASE WHEN %s='news' THEN 'notify' END AND state = 'pending'
               AND (%s::text IS NULL OR detail->>'content_revision' = %s)
            """,
            (
                int(now_ms) + NOTIFICATION_WAIT_MS,
                event_id,
                channel,
                expected_content_revision,
                expected_content_revision,
            ),
        )
        return bool(cursor.rowcount)

    def pending_notification_event_ids(self, *, channel: str, now_ms: int, limit: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT subject_id AS event_id FROM news_jobs
             WHERE job_kind = CASE WHEN %s='news' THEN 'notify' END AND state = 'pending' AND next_attempt_at_ms <= %s
             ORDER BY next_attempt_at_ms, subject_id
             LIMIT %s
            """,
            (channel, int(now_ms), int(limit)),
        ).fetchall()
        return [str(row["event_id"]) for row in rows]

    def retry_failed_revision(self, *, event_id: str, revision: str, now_ms: int) -> bool:
        """Reopen failed work and unsent intents of exactly this adopted revision."""

        lock_event(self.conn, event_id)
        cursor = self.conn.execute(
            """
            UPDATE news_jobs
               SET state = 'pending', attempts = 0, next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE subject_id = %s AND job_kind = CASE WHEN %s='news' THEN 'notify' END AND
        detail->>'content_revision' = %s AND state = 'failed'
            """,
            (int(now_ms), int(now_ms), event_id, NEWS_CHANNEL, revision),
        )
        if not cursor.rowcount:
            return False
        self.conn.execute(
            """
            UPDATE news_notifications q
               SET state = 'pending', attempts = 0, lease_token = NULL, lease_until_ms=NULL, last_attempt_at_ms = NULL,
                   settled_at_ms = NULL, next_attempt_at_ms = %s, updated_at_ms = %s
             WHERE q.event_id = %s AND q.content_revision = %s AND q.kind = 'update' AND q.state = 'dead'
            """,
            (int(now_ms), int(now_ms), event_id, revision),
        )
        return True
