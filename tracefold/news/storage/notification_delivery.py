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
        is kept for the same identity) and leaves notification pending. An existing ledger row is never
        touched. The send's timings are kept beside its reader-history context.
        """

        identity_row = self.conn.execute(
            "SELECT event_id FROM news_delivery_queue WHERE intent_id=%s AND kind='update'", (intent_id,)
        ).fetchone()
        if identity_row is None:
            return "lease_lost"
        lock_event(self.conn, str(identity_row["event_id"]))
        queued = self.conn.execute(
            """
            SELECT event_id, state, lease_token, frozen_card, content_revision, claim_refs, plan_key,
                   decision_ref, card_copy_input_digest, card_copy_document
              FROM news_delivery_queue WHERE intent_id = %s AND kind = 'update' FOR UPDATE
            """,
            (intent_id,),
        ).fetchone()
        if queued is None or queued["state"] != "pending" or queued["lease_token"] != lease_token:
            return "lease_lost"
        if queued["decision_ref"] != plan.record_ref:
            raise EventUpdateConflict("news_intent_decision_mismatch")
        frozen = queued["frozen_card"]
        if frozen is None or FrozenCard.model_validate(frozen) != card:
            raise EventUpdateConflict("news_intent_card_not_frozen")
        if self.conn.execute("SELECT 1 FROM news_deliveries WHERE intent_id = %s", (intent_id,)).fetchone():
            return "already_settled"
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
            SELECT 1 FROM news_deliveries
             WHERE event_id = %s AND kind = 'update' AND state IN ('sending', 'ambiguous')
               AND claim_refs ?| %s::text[]
             LIMIT 1
            """,
            (event_id, list(card.claim_refs)),
        ).fetchone()
        if head_changed or reader_changed or overlap is not None:
            self.conn.execute(
                """
                UPDATE news_delivery_queue SET lease_token = NULL, next_attempt_at_ms = %s, updated_at_ms = %s
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
            INSERT INTO news_deliveries (
              intent_id, event_id, kind, state, card, attempted_at_ms, created_at_ms,
              content_revision, claim_refs, body, payload_sha256, plan_key, decision_ref, history_context,
              card_copy_input_digest, card_copy_document, sent_claims
            )
            SELECT %(intent)s, e.event_id, 'update', 'sending', %(card)s::jsonb, %(now)s, %(now)s,
                   %(revision)s, %(claim_refs)s::jsonb, %(body)s, %(sha)s, %(key)s, %(decision)s,
                   jsonb_build_object(
                     'event_id', e.event_id,
                     'intent_id', %(intent)s::text,
                     'headline_zh', %(headline)s::text,
                     'comparison_title', e.comparison_title,
                     'comparison_fingerprint', e.comparison_fingerprint,
                     'dedupe_family', e.dedupe_family,
                     'storyline_key', e.storyline_key,
                     'canonical_assets', canonical.symbols,
                     'timings', %(timings)s::jsonb),
                   %(copy_digest)s, %(copy_document)s::jsonb, frozen.claims
              FROM news_events e CROSS JOIN canonical CROSS JOIN frozen
             WHERE e.event_id = %(event)s
            ON CONFLICT (intent_id) DO NOTHING
            RETURNING state
            """,
            {
                "intent": intent_id,
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
        return "begun" if inserted is not None else "already_settled"

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
        """Record the actual outcome of one frozen send or verify an identical prior settlement.

        Only a `sending` row with this exact payload is settled. Sent keeps the body, digest, provider
        message id and the provider's own receipt (what an in-place edit is later fenced by); a provider
        that answers with no message id is recorded with none. Ambiguous is held, and its claims count as
        possibly sent. A retryable not-sent never reached a reader, so its `sending` row is removed and the
        identity is released for the same payload under the queue's attempt bound and the provider's own
        `Retry-After`; the last one ends the unsent intent and fails the work. A refused one is terminal.
        """

        identity_row = self.conn.execute(
            """SELECT event_id FROM news_deliveries WHERE intent_id=%s
               UNION ALL SELECT event_id FROM news_delivery_queue WHERE intent_id=%s LIMIT 1""",
            (intent_id, intent_id),
        ).fetchone()
        if identity_row is None:
            return "conflict"
        lock_event(self.conn, str(identity_row["event_id"]))
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
        receipt = {
            "channel": NEWS_CHANNEL,
            "payload_sha256": payload_sha256,
            "provider_message_id": provider_message_id,
            "pushed_at_ms": int(settled_at_ms),
            # The provider's own push stamp and target identity fence enrichment edits.
            **dict(provider_receipt or {}),
        }
        ledger = self.conn.execute(
            """
            SELECT event_id, state, payload_sha256, content_revision, decision_ref, settlement FROM news_deliveries
             WHERE intent_id = %s FOR UPDATE
            """,
            (intent_id,),
        ).fetchone()
        if ledger is None:
            previous = self.conn.execute(
                "SELECT last_settlement FROM news_delivery_queue WHERE intent_id=%s FOR UPDATE", (intent_id,)
            ).fetchone()
            return "already_settled" if previous and previous["last_settlement"] == expected else "conflict"
        if ledger["payload_sha256"] != payload_sha256:
            return "conflict"
        if ledger["state"] != "sending":
            return "already_settled" if ledger["settlement"] == expected else "conflict"
        event_id = str(ledger["event_id"])
        content_revision = str(ledger["content_revision"])
        decision_ref = ledger["decision_ref"]
        queued = self.conn.execute(
            "SELECT attempts, lease_token, last_settlement FROM news_delivery_queue WHERE intent_id = %s FOR UPDATE",
            (intent_id,),
        ).fetchone()
        if queued is None or queued["lease_token"] != lease_token:
            previous = None if queued is None else queued["last_settlement"]
            return "already_settled" if previous == expected else "conflict"
        now_ms = int(settled_at_ms)
        if state == "sent":
            self.conn.execute(
                """
                UPDATE news_deliveries SET state = 'sent', receipt = %s::jsonb, settlement = %s::jsonb,
                       error_code = NULL, settled_at_ms = %s
                 WHERE intent_id = %s
                """,
                (_dumps(receipt), _dumps(expected), now_ms, intent_id),
            )
            self.conn.execute("DELETE FROM news_delivery_queue WHERE intent_id = %s", (intent_id,))
            self.work.intent_ended(event_id, content_revision, decision_ref, now_ms=now_ms)
            return "sent"
        if state == "ambiguous":
            self.conn.execute(
                """
                UPDATE news_deliveries SET state = 'ambiguous', error_code = %s,
                       settlement = %s::jsonb, settled_at_ms = %s
                 WHERE intent_id = %s
                """,
                (error_code or "send_outcome_ambiguous", _dumps(expected), now_ms, intent_id),
            )
            self.conn.execute("DELETE FROM news_delivery_queue WHERE intent_id = %s", (intent_id,))
            self.work.intent_ended(event_id, content_revision, decision_ref, now_ms=now_ms)
            return "ambiguous"
        code = error_code or "send_not_sent"
        if retryable:
            self.conn.execute("DELETE FROM news_deliveries WHERE intent_id = %s AND state = 'sending'", (intent_id,))
            self.work.fail_unsent_intent(
                intent_id,
                error_code=code,
                retryable=True,
                retry_after_ms=retry_after_ms,
                now_ms=now_ms,
                settlement=expected,
            )
            return "not_sent"
        self.conn.execute(
            """
            UPDATE news_deliveries SET state = 'terminal', error_code = %s,
                   settlement = %s::jsonb, settled_at_ms = %s
             WHERE intent_id = %s
            """,
            (code, _dumps(expected), now_ms, intent_id),
        )
        self.conn.execute(
            """
            UPDATE news_delivery_queue
               SET state = 'dead', attempts = LEAST(attempts + 1, %s), lease_token = NULL,
                   error_code = %s, settled_at_ms = %s, updated_at_ms = %s,
                   last_settlement = %s::jsonb
             WHERE intent_id = %s
            """,
            (INTENT_ATTEMPTS_MAX, code, now_ms, now_ms, _dumps(expected), intent_id),
        )
        self.work.intent_ended(event_id, content_revision, decision_ref, now_ms=now_ms)
        return "terminal"

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
            SELECT intent_id, event_id FROM news_deliveries
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
                UPDATE news_deliveries SET state = 'ambiguous', error_code = 'ambiguous_after_crash', settled_at_ms = %s
                 WHERE intent_id = %s AND state = 'sending'
                RETURNING event_id, content_revision, decision_ref
                """,
                (int(now_ms), candidate["intent_id"]),
            ).fetchone()
            if row is None:
                continue
            self.conn.execute("DELETE FROM news_delivery_queue WHERE intent_id = %s", (candidate["intent_id"],))
            self.work.intent_ended(
                str(row["event_id"]), str(row["content_revision"]), row["decision_ref"], now_ms=now_ms
            )
            settled += 1
        return settled
