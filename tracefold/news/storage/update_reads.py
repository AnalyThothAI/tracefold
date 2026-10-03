"""Bounded Event-detail reads of the EventUpdate plane (#706): head, adopted revisions, work and intents.

Read-only. The writer of every table here is the EventUpdate store; these statements only serve the
Event detail and its timeline, and each is registered with the query audit under the constant it runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from ..notifications.novelty import ClaimLink
from ..update_view import claim_reason_zh, effective_notification
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
   SELECT a.update_ref,a.event_id,a.document FROM news_analyses a
    WHERE a.update_ref=ANY(%s) AND a.event_id<>%s AND a.adopted_at_ms IS NOT NULL
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
 j.updated_at_ms,j.detail,n.plan,n.origin,n.decided_at_ms,a.document AS decided_document FROM news_jobs j
 LEFT JOIN news_notifications n ON n.notification_id=j.detail->>'decision_ref'
 LEFT JOIN news_analyses a ON a.event_id=j.subject_id AND a.content_revision=j.detail->>'content_revision'
  AND a.adopted_at_ms IS NOT NULL
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


def duplicate_claims_sql(event_id_sql: str) -> str:
    """Restatement provenance stays attached to its adopted revision, even after new sources arrive.

    The caller supplies a code-owned SQL identifier, never request text. Both the bounded feed
    decoration and detail use the same indexed prior-update and claim-receipt lookups.
    """
    return f"""
 SELECT DISTINCT ON (change->>'current_ref') change->>'current_ref' AS claim_ref,
        change->>'previous_ref' AS previous_ref,prior.event_id,
        COALESCE(receipt.card->>'headline_zh',claim->>'statement') AS headline,
        claim->>'statement' AS statement,(claim->>'first_available_at_ms')::bigint AS first_available_at_ms,
        item.reporting_origin,receipt.settled_at_ms AS received_at_ms,
        original_head.document AS original_document,original_head.content_revision AS original_revision,
        work.state AS original_state,work.detail->>'content_revision' AS decided_revision,
        work.updated_at_ms AS decision_updated_at_ms,decision.decided_at_ms,decision.plan
 FROM news_analyses history
 CROSS JOIN LATERAL jsonb_array_elements(history.document->'changes') change
 LEFT JOIN news_analyses prior ON prior.update_ref=change->>'previous_content_ref'
  AND prior.adopted_at_ms IS NOT NULL
 LEFT JOIN LATERAL (
   SELECT value AS claim FROM jsonb_array_elements(prior.document->'claims')
   WHERE value->>'ref'=change->>'previous_ref' LIMIT 1
 ) previous ON true
 LEFT JOIN news_events original ON original.event_id=prior.event_id
 LEFT JOIN news_items item ON item.item_id=original.leader_item_id
 LEFT JOIN news_analyses original_head ON original_head.analysis_id=original.current_analysis_id
 LEFT JOIN news_jobs work ON work.job_kind='notify' AND work.subject_id=prior.event_id
 LEFT JOIN news_notifications decision ON decision.notification_id=work.detail->>'decision_ref'
 LEFT JOIN LATERAL (
   SELECT card,settled_at_ms FROM news_notifications
   WHERE kind='update' AND state='sent'
     AND claim_refs @> to_jsonb(ARRAY[change->>'previous_ref'])
   ORDER BY settled_at_ms DESC,intent_id LIMIT 1
 ) receipt ON true
 WHERE history.event_id={event_id_sql} AND history.adopted_at_ms IS NOT NULL
   AND change->>'kind'='restatement'
 ORDER BY change->>'current_ref',history.adopted_at_ms DESC,history.analysis_id DESC
 """  # noqa: S608 -- callers pass fixed column names or a bound placeholder.


EVENT_DUPLICATE_CLAIMS_SQL: Final = duplicate_claims_sql("%s")


def duplicate_view(row: Mapping[str, Any]) -> dict[str, Any]:
    work = effective_notification(
        {"content_revision": row.get("original_revision"), "document": row.get("original_document")},
        {
            "state": row["original_state"],
            "content_revision": row.get("decided_revision"),
            "plan": row.get("plan"),
            "decided_at_ms": row.get("decided_at_ms"),
        }
        if row.get("original_state")
        else None,
    )
    plan = (work or {}).get("plan") or {} if not (work or {}).get("projection_error") else {}
    decision = next(
        (entry for entry in plan.get("claim_decisions", ()) if entry.get("claim_ref") == row.get("previous_ref")), None
    )
    return {
        "claim_ref": row["claim_ref"],
        "event_id": row.get("event_id"),
        "headline": row.get("headline"),
        "first_available_at_ms": row.get("first_available_at_ms"),
        "reporting_origin": row.get("reporting_origin"),
        "received_at_ms": row.get("received_at_ms"),
        "reason_zh": claim_reason_zh(decision) if decision else "原条未记录可读取的推送判断",
        "decided_at_ms": row.get("decided_at_ms") if decision else None,
    }


def duplicate_claims(conn: Any, event_id: str) -> list[dict[str, Any]]:
    return [duplicate_view(row) for row in conn.execute(EVENT_DUPLICATE_CLAIMS_SQL, (event_id,)).fetchall()]


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
                "decided_document",
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
    "EVENT_DUPLICATE_CLAIMS_SQL",
    "EVENT_EARLIER_RECEIPTS_SQL",
    "EVENT_KNOWN_RECEIPTS_SQL",
    "EVENT_NOTIFICATION_WORK_SQL",
    "EVENT_SEMANTIC_OBSERVATIONS_SQL",
    "EVENT_SEMANTIC_WORK_SQL",
    "EVENT_UPDATE_HEAD_SQL",
    "EVENT_UPDATE_PREVIOUS_CLAIMS_SQL",
    "EVENT_UPDATE_REVISIONS_SQL",
    "attach_earlier_receipts",
    "duplicate_claims",
    "duplicate_claims_sql",
    "duplicate_view",
    "event_deliveries",
    "event_delivery_queue",
    "event_update_head",
    "event_update_revisions",
    "notification_work",
    "previous_claims",
    "semantic_observations",
    "semantic_work",
]
