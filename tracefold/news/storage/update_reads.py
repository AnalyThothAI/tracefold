"""Bounded Event-detail reads of the EventUpdate plane (#706): head, adopted revisions, work and intents.

Read-only. The writer of every table here is the EventUpdate store; these statements only serve the
Event detail and its timeline, and each is registered with the query audit under the constant it runs.
"""

from __future__ import annotations

from typing import Any, Final

# The adopted head with its insert-only document. One row or none: the head is the Event's CAS target.
EVENT_UPDATE_HEAD_SQL: Final = """
    SELECT h.event_id, h.content_revision, h.input_revision, h.update_ref, h.adopted_at_ms,
           u.previous_content_revision, u.observation_result_id, u.document
      FROM news_event_update_heads h
      JOIN news_event_updates u ON u.event_id = h.event_id AND u.content_revision = h.content_revision
     WHERE h.event_id = %s
"""
# Every adopted revision of one Event, without the documents: the timeline needs the clock, the input
# revision and which change kinds each adoption introduced.
EVENT_UPDATE_REVISIONS_SQL: Final = """
    SELECT u.content_revision, u.input_revision, u.previous_content_revision, u.adopted_at_ms,
           u.observation_result_id,
           jsonb_path_query_array(u.document, '$.changes[*].kind') AS change_kinds,
           jsonb_array_length(u.document -> 'claims') AS claim_n
      FROM news_event_updates u
     WHERE u.event_id = %s
     ORDER BY u.adopted_at_ms, u.content_revision
     LIMIT 50
"""
# The prior claims a head's changes point at. A change names its previous content by update ref, which
# is either an earlier revision of this Event or the current head of a related Event. A related Event
# whose head has since moved on is not searched: its old statement reads as unknown.
EVENT_UPDATE_PREVIOUS_CLAIMS_SQL: Final = """
    SELECT prior.update_ref, prior.event_id, claim ->> 'ref' AS claim_ref, claim ->> 'statement' AS statement
      FROM (
        SELECT public.news_identity('update', jsonb_build_array(u.event_id, u.content_revision)) AS update_ref,
               u.event_id, u.document
          FROM news_event_updates u
         WHERE u.event_id = %s
           AND public.news_identity('update', jsonb_build_array(u.event_id, u.content_revision)) = ANY(%s)
        UNION ALL
        SELECT h.update_ref, u.event_id, u.document
          FROM news_event_update_heads h
          JOIN news_event_updates u ON u.event_id = h.event_id AND u.content_revision = h.content_revision
         WHERE h.update_ref = ANY(%s) AND h.event_id <> %s
      ) prior
      CROSS JOIN LATERAL jsonb_array_elements(prior.document -> 'claims') AS claim
"""
EVENT_SEMANTIC_WORK_SQL: Final = """
    SELECT event_id, wanted_revision, done_revision, attempts, next_attempt_at_ms, published_at_ms,
           last_outcome, last_error_code, extra_read_state, updated_at_ms
      FROM news_semantic_work
     WHERE event_id = %s
"""
# The most recent observations of one Event and the revision each was adopted as, if any.
EVENT_SEMANTIC_OBSERVATIONS_SQL: Final = """
    SELECT o.result_id, o.input_revision, o.program_identity, o.completed_at_ms,
           adopted.content_revision AS adopted_content_revision
      FROM news_semantic_observations o
      LEFT JOIN news_event_updates adopted ON adopted.observation_result_id = o.result_id
     WHERE o.event_id = %s
     ORDER BY o.completed_at_ms DESC, o.result_id DESC
     LIMIT 20
"""
EVENT_NOTIFICATION_WORK_SQL: Final = """
    SELECT event_id, channel, content_revision, state, plan, reader_revision, attempts,
           next_attempt_at_ms, updated_at_ms
      FROM news_notification_work
     WHERE event_id = %s AND channel = 'news'
"""
# Every ledger row of one Event, legacy cards and update intents alike.
EVENT_DELIVERIES_SQL: Final = """
    SELECT intent_id, kind, state, card, receipt, error_code, attempted_at_ms, settled_at_ms,
           created_at_ms, edit_state, pending_card, edit_error_code, edit_attempted_at_ms,
           edit_settled_at_ms, content_revision, claim_refs, body, payload_sha256, plan_key
      FROM news_deliveries
     WHERE event_id = %s
     ORDER BY created_at_ms, intent_id
"""
# Work still owed for one Event. A row leaves this table when its ledger row settles, so what remains
# is either pending or dead.
EVENT_DELIVERY_QUEUE_SQL: Final = """
    SELECT intent_id, kind, state, attempts, error_code, enqueued_at_ms, next_attempt_at_ms,
           settled_at_ms, content_revision, claim_refs, plan_key
      FROM news_delivery_queue
     WHERE event_id = %s
     ORDER BY enqueued_at_ms, intent_id
"""


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
    return dict(row) if row else None


def semantic_observations(conn: Any, event_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(EVENT_SEMANTIC_OBSERVATIONS_SQL, (event_id,)).fetchall()]


def notification_work(conn: Any, event_id: str) -> dict[str, Any] | None:
    row = conn.execute(EVENT_NOTIFICATION_WORK_SQL, (event_id,)).fetchone()
    return dict(row) if row else None


def event_deliveries(conn: Any, event_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(EVENT_DELIVERIES_SQL, (event_id,)).fetchall()]


def event_delivery_queue(conn: Any, event_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(EVENT_DELIVERY_QUEUE_SQL, (event_id,)).fetchall()]


__all__ = [
    "EVENT_DELIVERIES_SQL",
    "EVENT_DELIVERY_QUEUE_SQL",
    "EVENT_NOTIFICATION_WORK_SQL",
    "EVENT_SEMANTIC_OBSERVATIONS_SQL",
    "EVENT_SEMANTIC_WORK_SQL",
    "EVENT_UPDATE_HEAD_SQL",
    "EVENT_UPDATE_PREVIOUS_CLAIMS_SQL",
    "EVENT_UPDATE_REVISIONS_SQL",
    "event_deliveries",
    "event_delivery_queue",
    "event_update_head",
    "event_update_revisions",
    "notification_work",
    "previous_claims",
    "semantic_observations",
    "semantic_work",
]
