"""Fenced first-send permission and actual provider outcome settlement.

Commands use the caller's existing transaction; no external I/O or independent commit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from ..notifications.contracts import NEWS_CHANNEL, FrozenCard, NotificationPlan
from ..notifications.ports import BeginSendStatus
from .errors import EventUpdateConflict
from .notification_context import NotificationContextStorage
from .notification_work import INTENT_ATTEMPTS_MAX, IntentOutcome, NotificationWorkStorage
from .sql_values import _dumps
from .update_commit import lock_event

SENDING_ORPHAN_MS: Final = 60_000


ORPHAN_SEND_BATCH_MAX: Final = 50


class NotificationDeliveryStorage:
    def __init__(self, conn: Any, *, context: NotificationContextStorage, work: NotificationWorkStorage) -> None:
        self.conn = conn
        self.context = context
        self.work = work

    def begin_intent_send(
        self,
        *,
        intent_id: str,
        lease_token: str,
        plan: NotificationPlan,
        card: FrozenCard,
        now_ms: int,
        timings_json: str | None = None,
    ) -> BeginSendStatus:
        """Recheck head, reader revision, lease and in-flight overlap, then freeze `sending`.

        A changed head, related reader or overlapping send releases the unsent reservation (its frozen card
        is kept for the same identity) and leaves notification pending. The immutable judgment stays in
        the same row. The send's timings are kept beside its reader-history context.
        """

        identity_row = self.conn.execute(
            "SELECT event_id FROM news_notifications WHERE intent_id=%s AND kind='update'", (intent_id,)
        ).fetchone()
        if identity_row is None:
            return "lease_lost"
        lock_event(self.conn, str(identity_row["event_id"]))
        queued = self.conn.execute(
            """
            SELECT event_id, state, lease_token, lease_until_ms, card, content_revision, claim_refs, plan_key,
                   notification_id AS decision_ref, card_copy_input_digest, card_copy_document
              FROM news_notifications WHERE intent_id = %s AND kind = 'update' FOR UPDATE
            """,
            (intent_id,),
        ).fetchone()
        if queued is not None and queued["state"] in ("sending", "sent", "ambiguous", "terminal"):
            return "already_settled"
        if (
            queued is None
            or queued["state"] != "pending"
            or queued["lease_token"] != lease_token
            or queued["lease_until_ms"] is None
            or queued["lease_until_ms"] <= now_ms
        ):
            return "lease_lost"
        if queued["decision_ref"] != plan.record_ref:
            raise EventUpdateConflict("news_intent_decision_mismatch")
        frozen = queued["card"]
        if frozen is None or FrozenCard.model_validate(frozen) != card:
            raise EventUpdateConflict("news_intent_card_not_frozen")
        event_id = str(queued["event_id"])
        head = self.conn.execute(
            "SELECT update_ref FROM news_event_update_heads WHERE event_id = %s", (event_id,)
        ).fetchone()
        head_changed = head is None or head["update_ref"] != plan.update_ref
        reader_changed = (
            not head_changed and self.context.current_reader_revision(event_id, now_ms=now_ms) != plan.reader_revision
        )
        overlap = self.conn.execute(
            """
            SELECT 1 FROM news_notifications
             WHERE event_id = %s AND kind = 'update' AND state IN ('sending', 'ambiguous')
               AND claim_refs ?| %s::text[]
             LIMIT 1
            """,
            (event_id, list(card.claim_refs)),
        ).fetchone()
        if head_changed or reader_changed or overlap is not None:
            self.conn.execute(
                """
                UPDATE news_notifications SET lease_token = NULL, lease_until_ms=NULL, next_attempt_at_ms = %s,
        updated_at_ms = %s
                 WHERE intent_id = %s
                """,
                (int(now_ms), int(now_ms), intent_id),
            )
            self.work.pend_notification(
                event_id, expected_content_revision=str(queued["content_revision"]), next_at_ms=now_ms, now_ms=now_ms
            )
            return "head_changed" if head_changed else "reader_changed" if reader_changed else "overlap"
        inserted = self.conn.execute(
            """
            WITH frozen AS (
              SELECT COALESCE(jsonb_agg(claim), '[]'::jsonb) AS claims
                FROM news_event_updates u
                CROSS JOIN LATERAL jsonb_array_elements(u.document -> 'claims') claim
               WHERE u.event_id = %(event)s AND u.content_revision = %(revision)s
                 AND claim ->> 'ref' = ANY(%(refs)s)
            ), selected AS (
              SELECT DISTINCT upper(asset ->> 'symbol') AS symbol
                FROM news_event_updates u
                CROSS JOIN LATERAL jsonb_array_elements(u.document -> 'claims') claim
                CROSS JOIN LATERAL jsonb_array_elements(claim #> '{fields,assets}') asset
               WHERE u.event_id = %(event)s AND u.content_revision = %(revision)s
                 AND claim ->> 'ref' = ANY(%(refs)s) AND asset ->> 'role' = 'primary'
            ), canonical AS (
              SELECT COALESCE(jsonb_agg(symbol ORDER BY symbol), '[]'::jsonb) AS symbols
                FROM (SELECT DISTINCT COALESCE(a.base_symbol, s.symbol) AS symbol
                        FROM selected s LEFT JOIN news_symbol_aliases a ON a.alias = s.symbol) resolved
            )
            UPDATE news_notifications n SET state='sending',attempted_at_ms=%(now)s,updated_at_ms=%(now)s,
                   history_context=jsonb_build_object(
                     'event_id',e.event_id,'intent_id',%(intent)s::text,'headline_zh',%(headline)s::text,
                     'comparison_title',e.comparison_title,'comparison_fingerprint',e.comparison_fingerprint,
                     'dedupe_family',e.dedupe_family,'storyline_key',e.storyline_key,
                     'canonical_assets',canonical.symbols,'timings',%(timings)s::jsonb),
                   sent_claims=frozen.claims
              FROM news_events e CROSS JOIN canonical CROSS JOIN frozen
             WHERE e.event_id=%(event)s AND n.intent_id=%(intent)s AND n.state='pending'
               AND n.lease_token=%(lease)s AND n.lease_until_ms>%(now)s
            RETURNING n.state
            """,
            {
                "intent": intent_id,
                "lease": lease_token,
                "event": event_id,
                "revision": queued["content_revision"],
                "refs": list(card.claim_refs),
                "card": _dumps(card.model_dump(mode="json")),
                "now": int(now_ms),
                "claim_refs": _dumps(list(queued["claim_refs"])),
                "body": card.body,
                "sha": card.payload_sha256,
                "key": bool(queued["plan_key"]),
                "decision": queued["decision_ref"],
                "headline": card.headline_zh,
                "timings": timings_json,
                "copy_digest": queued["card_copy_input_digest"],
                "copy_document": _dumps(queued["card_copy_document"]),
            },
        ).fetchone()
        return "begun" if inserted is not None else "lease_lost"

    def settle_intent_send(
        self,
        *,
        intent_id: str,
        lease_token: str,
        payload_sha256: str,
        state: IntentOutcome,
        provider_message_id: str | None,
        error_code: str | None,
        retryable: bool,
        retry_after_ms: int | None,
        settled_at_ms: int,
        provider_receipt: Mapping[str, Any] | None = None,
    ) -> str:
        identity = self.conn.execute(
            "SELECT event_id FROM news_notifications WHERE intent_id=%s", (intent_id,)
        ).fetchone()
        if identity is None:
            return "conflict"
        lock_event(self.conn, str(identity["event_id"]))
        expected = {
            "lease_token": lease_token,
            "payload_sha256": payload_sha256,
            "state": state,
            "error_code": error_code,
            "retryable": retryable,
            "retry_after_ms": retry_after_ms,
            "provider_message_id": provider_message_id,
            "provider_receipt": dict(provider_receipt or {}),
        }
        row = self.conn.execute(
            "SELECT * FROM news_notifications WHERE intent_id=%s FOR UPDATE", (intent_id,)
        ).fetchone()
        if row is None or row["card"] is None or row["card"].get("payload_sha256") != payload_sha256:
            return "conflict"
        if row["state"] != "sending" or row["lease_token"] != lease_token:
            return "already_settled" if row["settlement"] == expected else "conflict"
        event_id = str(row["event_id"])
        revision = str(row["content_revision"])
        decision_ref = str(row["notification_id"])
        now_ms = int(settled_at_ms)
        if state == "not_sent" and retryable:
            self.work.fail_unsent_intent(
                intent_id,
                error_code=error_code or "send_not_sent",
                retryable=True,
                retry_after_ms=retry_after_ms,
                now_ms=now_ms,
                lease_token=lease_token,
                settlement=expected,
            )
            return "not_sent"
        final_state = "terminal" if state == "not_sent" else state
        receipt = None
        if state == "sent":
            receipt = {
                "channel": NEWS_CHANNEL,
                "payload_sha256": payload_sha256,
                "provider_message_id": provider_message_id,
                "pushed_at_ms": now_ms,
                **dict(provider_receipt or {}),
            }
        code = (
            None
            if state == "sent"
            else error_code or ("send_outcome_ambiguous" if state == "ambiguous" else "send_not_sent")
        )
        self.conn.execute(
            """UPDATE news_notifications SET state=%s,receipt=%s::jsonb,settlement=%s::jsonb,
               error_code=%s,settled_at_ms=%s,updated_at_ms=%s,lease_token=NULL,lease_until_ms=NULL,
               attempts=CASE WHEN %s='terminal' THEN LEAST(attempts+1,%s) ELSE attempts END
               WHERE intent_id=%s AND state='sending' AND lease_token=%s""",
            (
                final_state,
                None if receipt is None else _dumps(receipt),
                _dumps(expected),
                code,
                now_ms,
                now_ms,
                final_state,
                INTENT_ATTEMPTS_MAX,
                intent_id,
                lease_token,
            ),
        )
        self.work.intent_ended(event_id, revision, decision_ref, now_ms=now_ms)
        return final_state

    def terminalize_interrupted_deliveries(
        self, *, now_ms: int, exclude_intent_ids: Sequence[str] = (), limit: int = ORPHAN_SEND_BATCH_MAX
    ) -> int:
        """Hold ambiguous every `sending` row whose owner is gone, and complete the plan it was sent for.

        An owner outlives neither its provider call nor its bounded settlement, so a row older than
        `SENDING_ORPHAN_MS` has none -- except the sends this process says it still holds. Run at start and
        periodically, so an orphan never outlives a restart or keeps its claims waiting.
        """

        rows = self.conn.execute(
            """
            SELECT intent_id, event_id FROM news_notifications
             WHERE kind = 'update' AND state = 'sending' AND attempted_at_ms < %s
               AND NOT (intent_id = ANY(%s::text[]))
             ORDER BY event_id, intent_id
             LIMIT %s
            """,
            (int(now_ms) - SENDING_ORPHAN_MS, list(exclude_intent_ids), int(limit)),
        ).fetchall()
        settled = 0
        for candidate in rows:
            lock_event(self.conn, str(candidate["event_id"]))
            row = self.conn.execute(
                """
                UPDATE news_notifications SET state = 'ambiguous', error_code = 'ambiguous_after_crash',
        settled_at_ms = %s,lease_token=NULL,lease_until_ms=NULL
                 WHERE intent_id = %s AND state = 'sending'
                RETURNING event_id, content_revision, notification_id AS decision_ref
                """,
                (int(now_ms), candidate["intent_id"]),
            ).fetchone()
            if row is None:
                continue
            self.work.intent_ended(
                str(row["event_id"]), str(row["content_revision"]), row["decision_ref"], now_ms=now_ms
            )
            settled += 1
        return settled
