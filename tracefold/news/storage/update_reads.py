"""Bounded Event-detail reads of the EventUpdate plane (#706): head, adopted revisions, work and intents.

Read-only. The writer of every table here is the EventUpdate store; these statements only serve the
Event detail and its timeline, and each is registered with the query audit under the constant it runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from ..notifications.novelty import ClaimLink
from .notification_view import pending_notification, receipt_notification
from .semantic_jobs import semantic_job

EVENT_UPDATE_HEAD_SQL: Final = """
 SELECT a.event_id,a.content_revision,a.input_revision,a.update_ref,a.adopted_at_ms,
        a.previous_content_revision,
        CASE WHEN a.origin='semantic' THEN a.analysis_id END AS observation_result_id,
        CASE WHEN a.origin='scope_repair' THEN a.analysis_id END AS scope_repair_id,a.document
 FROM news_events e JOIN news_analyses a ON a.analysis_id=e.current_analysis_id WHERE e.event_id=%s
"""
EVENT_UPDATE_REVISIONS_SQL: Final = """
 SELECT a.content_revision,a.input_revision,a.previous_content_revision,a.adopted_at_ms,
        CASE WHEN a.origin='semantic' THEN a.analysis_id END AS observation_result_id,
        CASE WHEN a.origin='scope_repair' THEN a.analysis_id END AS scope_repair_id,
        jsonb_path_query_array(a.document,'$.changes[*].kind') AS change_kinds,
        jsonb_array_length(a.document->'claims') AS claim_n
 FROM news_analyses a WHERE a.event_id=%s AND a.adopted_at_ms IS NOT NULL
 ORDER BY a.adopted_at_ms,a.content_revision LIMIT 50
"""
EVENT_UPDATE_PREVIOUS_CLAIMS_SQL: Final = """
 SELECT prior.update_ref,prior.event_id,claim->>'ref' AS claim_ref,claim->>'statement' AS statement
 FROM (
   SELECT update_ref,event_id,document FROM news_analyses
    WHERE event_id=%s AND update_ref=ANY(%s) AND adopted_at_ms IS NOT NULL
   UNION ALL
   SELECT a.update_ref,a.event_id,a.document FROM news_events e
    JOIN news_analyses a ON a.analysis_id=e.current_analysis_id
    WHERE a.update_ref=ANY(%s) AND e.event_id<>%s
 ) prior CROSS JOIN LATERAL jsonb_array_elements(prior.document->'claims') claim
"""
EVENT_SEMANTIC_WORK_SQL: Final = """
 SELECT subject_id,state,attempts,next_attempt_at_ms,lease_until_ms,last_error_code,detail,updated_at_ms
 FROM news_jobs WHERE job_kind='semantic' AND subject_id=%s
"""
EVENT_SEMANTIC_OBSERVATIONS_SQL: Final = """
 SELECT analysis_id AS result_id,input_revision,program_identity,completed_at_ms,
        content_revision AS adopted_content_revision FROM news_analyses
 WHERE origin='semantic' AND event_id=%s ORDER BY completed_at_ms DESC,analysis_id DESC LIMIT 20
"""
EVENT_NOTIFICATION_WORK_SQL: Final = """
 SELECT j.subject_id,j.state,j.attempts,j.last_error_code,j.next_attempt_at_ms,
 j.updated_at_ms,j.detail,n.plan,n.origin,n.decided_at_ms FROM news_jobs j
 LEFT JOIN news_notifications n ON n.notification_id=j.detail->>'decision_ref'
 WHERE j.job_kind='notify' AND j.subject_id=%s
"""
EVENT_DELIVERIES_SQL: Final = """
 SELECT intent_id,kind,state,card,receipt,error_code,attempted_at_ms,settled_at_ms,created_at_ms,edit_state,
 pending_card,edit_error_code,edit_attempted_at_ms,edit_settled_at_ms,content_revision,claim_refs,plan_key,
 history_context->'timings' AS timings,plan->'timings' AS plan_timings
 FROM news_notifications WHERE kind='update' AND event_id=%s
  AND state IN ('sending','sent','ambiguous','terminal') ORDER BY created_at_ms,intent_id
"""
EVENT_DELIVERY_QUEUE_SQL: Final = """
 SELECT intent_id,kind,state,attempts,error_code,next_attempt_at_ms,content_revision,claim_refs,plan_key,
 reserved_at_ms,card,settled_at_ms FROM news_notifications WHERE kind='update' AND event_id=%s
  AND (state IN ('pending','dead','sending') OR (state='terminal' AND reserved_at_ms IS NOT NULL))
 ORDER BY reserved_at_ms,intent_id
"""

EVENT_EARLIER_RECEIPTS_SQL: Final = """
 SELECT intent_id,event_id,card,settled_at_ms FROM news_notifications
 WHERE kind='update' AND state='sent' AND intent_id=ANY(%s)
   AND settled_at_ms<=%s
"""
EVENT_KNOWN_RECEIPTS_SQL: Final = """
 SELECT wanted.claim_ref,receipt.intent_id,receipt.event_id,receipt.card,receipt.settled_at_ms
 FROM unnest(%s::text[]) wanted(claim_ref)
 CROSS JOIN LATERAL (
   SELECT intent_id,event_id,card,settled_at_ms FROM news_notifications
   WHERE kind='update' AND state='sent'
     AND claim_refs @> to_jsonb(ARRAY[wanted.claim_ref]) AND settled_at_ms<=%s
   ORDER BY settled_at_ms DESC,intent_id LIMIT 1
 ) receipt
"""


def attach_earlier_receipts(conn: Any, work: dict[str, Any] | None, view: dict[str, Any] | None) -> None:
    """Resolve frozen anchors in a batch. Old known rows use only their persisted semantic path."""
    if work is None or view is None or view.get("plan") is None:
        return
    rows = view["plan"]["claim_decisions"]
    before = int(work.get("decided_at_ms") or work["updated_at_ms"])
    ids = sorted({row["earlier_intent_id"] for row in rows if row.get("earlier_intent_id")})
    receipts = conn.execute(EVENT_EARLIER_RECEIPTS_SQL, (ids, before)).fetchall() if ids else []
    by_id = {row["intent_id"]: row for row in receipts}
    targets = {}
    raw_plan = work.get("plan")
    # The projection already validated current or historical metadata. Reading known links must not
    # reinterpret old judgment evidence or make receipt attachment depend on the current question schema.
    for row in raw_plan.get("claim_decisions", ()) if isinstance(raw_plan, Mapping) else ():
        record = row.get("reader")
        if not isinstance(record, Mapping) or record.get("novelty") != "known":
            continue
        target = row["claim_ref"]
        for raw_link in record.get("link_path", ()):
            link = ClaimLink.model_validate(raw_link)
            target = link.previous_ref if target == link.current_ref else link.current_ref
        targets[row["claim_ref"]] = target
    known = (
        conn.execute(EVENT_KNOWN_RECEIPTS_SQL, (sorted(set(targets.values())), before)).fetchall() if targets else []
    )
    by_claim = {row["claim_ref"]: row for row in known}
    for decision in rows:
        receipt = by_id.get(decision.get("earlier_intent_id"))
        if receipt is None and not decision.get("earlier_intent_id"):
            receipt = by_claim.get(targets.get(decision["claim_ref"]))
        if receipt is None:
            continue
        card = receipt["card"] or {}
        decision["earlier_intent_id"] = receipt["intent_id"]
        decision["earlier"] = {
            "intent_id": receipt["intent_id"],
            "event_id": receipt["event_id"],
            "headline_zh": str(card.get("headline_zh") or ""),
            "body": str(card.get("body") or ""),
            "received_at_ms": int(receipt["settled_at_ms"]),
        }


def event_update_head(conn: Any, event_id: str) -> dict[str, Any] | None:
    row = conn.execute(EVENT_UPDATE_HEAD_SQL, (event_id,)).fetchone()
    return dict(row) if row else None


def event_update_revisions(conn: Any, event_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(EVENT_UPDATE_REVISIONS_SQL, (event_id,)).fetchall()]


def previous_claims(conn: Any, event_id: str, update_refs: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    if not update_refs:
        return {}
    rows = conn.execute(
        EVENT_UPDATE_PREVIOUS_CLAIMS_SQL,
        (event_id, update_refs, update_refs, event_id),
    ).fetchall()
    return {
        (str(row["update_ref"]), str(row["claim_ref"])): {
            "statement": row["statement"],
            "event_id": row["event_id"],
        }
        for row in rows
        if row["claim_ref"] and row["statement"]
    }


def semantic_work(conn: Any, event_id: str) -> dict[str, Any] | None:
    row = conn.execute(EVENT_SEMANTIC_WORK_SQL, (event_id,)).fetchone()
    native = semantic_job(row)
    if native is None:
        return None
    return {
        key: native[key]
        for key in (
            "event_id",
            "wanted_revision",
            "done_revision",
            "attempts",
            "next_attempt_at_ms",
            "published_at_ms",
            "last_outcome",
            "last_error_code",
            "extra_read_state",
            "updated_at_ms",
        )
    }


def semantic_observations(conn: Any, event_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(EVENT_SEMANTIC_OBSERVATIONS_SQL, (event_id,)).fetchall()]


def notification_work(conn: Any, event_id: str) -> dict[str, Any] | None:
    row = conn.execute(EVENT_NOTIFICATION_WORK_SQL, (event_id,)).fetchone()
    if row is None:
        return None
    return {
        "event_id": row["subject_id"],
        "channel": "news",
        **row["detail"],
        **{
            key: row[key]
            for key in (
                "state",
                "plan",
                "origin",
                "attempts",
                "last_error_code",
                "next_attempt_at_ms",
                "updated_at_ms",
                "decided_at_ms",
            )
        },
    }


def event_deliveries(conn: Any, event_id: str) -> list[dict[str, Any]]:
    return [receipt_notification(row) for row in conn.execute(EVENT_DELIVERIES_SQL, (event_id,)).fetchall()]


def event_delivery_queue(conn: Any, event_id: str) -> list[dict[str, Any]]:
    return [pending_notification(row) for row in conn.execute(EVENT_DELIVERY_QUEUE_SQL, (event_id,)).fetchall()]


__all__ = [
    "EVENT_DELIVERIES_SQL",
    "EVENT_DELIVERY_QUEUE_SQL",
    "EVENT_EARLIER_RECEIPTS_SQL",
    "EVENT_KNOWN_RECEIPTS_SQL",
    "EVENT_NOTIFICATION_WORK_SQL",
    "EVENT_SEMANTIC_OBSERVATIONS_SQL",
    "EVENT_SEMANTIC_WORK_SQL",
    "EVENT_UPDATE_HEAD_SQL",
    "EVENT_UPDATE_PREVIOUS_CLAIMS_SQL",
    "EVENT_UPDATE_REVISIONS_SQL",
    "attach_earlier_receipts",
    "event_deliveries",
    "event_delivery_queue",
    "event_update_head",
    "event_update_revisions",
    "notification_work",
    "previous_claims",
    "semantic_observations",
    "semantic_work",
]
