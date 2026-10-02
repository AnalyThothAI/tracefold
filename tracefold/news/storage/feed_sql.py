"""Pure SQL statement builders shared by the News feed runtime and its query audit."""

from __future__ import annotations

from typing import Final

# S608 exemptions below compose only the module's fixed feed predicate list; all request values stay bound.
from ..models import ADMITTED_ADMISSIONS
from ..source_contracts import EVENT_KINDS

ITEM_RELATED_COUNT_SQL: Final = "SELECT count(DISTINCT event_id) AS n FROM news_event_members WHERE item_id=%s"
ITEM_RELATED_KEYS_SQL: Final = (
    "SELECT DISTINCT event_id FROM news_event_members "
    "WHERE item_id=%s AND (%s::text IS NULL OR event_id > %s) ORDER BY event_id LIMIT %s"
)
ITEM_RELATED_EVENTS_SQL: Final = """
 SELECT e.event_id,e.leader_item_id,e.focus_fact_text,e.focus_fact_method,
        (w.detail->>'wanted_revision')::integer AS wanted_revision,
        (w.detail->>'done_revision')::integer AS done_revision,
        w.detail->>'last_outcome' AS last_outcome,w.last_error_code,
        h.content_revision AS adopted_content_revision,n.state AS notification_state,
        d.plan->>'action' AS notification_action,
        (SELECT CASE q.state WHEN 'sending' THEN 'pending' WHEN 'terminal' THEN 'dead' ELSE q.state END
         FROM news_notifications q WHERE q.event_id=e.event_id AND q.kind='update'
          AND (q.state IN ('pending','dead','sending') OR (q.state='terminal' AND q.reserved_at_ms IS NOT NULL))
         ORDER BY q.updated_at_ms DESC,q.intent_id DESC LIMIT 1) AS intent_state,
        (SELECT count(*) FROM news_notifications sent
          WHERE sent.event_id=e.event_id AND sent.kind='update' AND sent.state='sent') AS sent_count,
        (SELECT array_agg(DISTINCT m.fact_text ORDER BY m.fact_text) FROM news_event_members m
          WHERE m.event_id=e.event_id AND m.item_id=%s) AS member_scopes,
        (SELECT array_agg(DISTINCT m.match_kind ORDER BY m.match_kind) FROM news_event_members m
          WHERE m.event_id=e.event_id AND m.item_id=%s) AS match_kinds
 FROM news_events e
 LEFT JOIN news_jobs w ON w.job_kind='semantic' AND w.subject_id=e.event_id
 LEFT JOIN news_analyses h ON h.analysis_id=e.current_analysis_id
 LEFT JOIN news_jobs n ON n.job_kind='notify' AND n.subject_id=e.event_id
 LEFT JOIN news_notifications d ON d.notification_id=n.detail->>'decision_ref' AND d.origin<>'legacy_delivery'
 WHERE e.event_id=ANY(%s) ORDER BY e.event_id
"""

ADMITTED_SQL: Final = ", ".join(f"'{value}'" for value in sorted(ADMITTED_ADMISSIONS))
# Reader cards are EventUpdate intents.
READER_DELIVERY_KINDS_SQL: Final = "('update')"
# Feed task tabs mirror `outcome.event_outcome` over the feed's joined rows. Keeping these predicates
# beside both statement builders makes the page and count query share one definition.
# The EventUpdate path (#706), after the ledger and the Gate: semantic work still owed a revision, or an
# adopted head whose notification is undecided, deferred or decided to notify.
_SEMANTIC_OWED_SQL: Final = "sw.state IN ('pending','failed')"
_UPDATE_PENDING_SQL: Final = (
    f"({_SEMANTIC_OWED_SQL} AND sw.state='pending')"
    f" OR (NOT COALESCE({_SEMANTIC_OWED_SQL}, false) AND h.event_id IS NOT NULL"
    f" AND ((nw.subject_id IS NULL AND q.state='pending')"
    " OR nw.state='pending'))"
    f" OR (NOT COALESCE({_SEMANTIC_OWED_SQL}, false) AND nw.state='done' AND d.state='sending')"
    f" OR (NOT COALESCE({_SEMANTIC_OWED_SQL}, false) AND nw.state='done'"
    " AND nd.plan->>'action'='notify' AND q.state='pending'"
    " AND q.content_revision=(nw.detail->>'content_revision'))"
)
_PENDING_CORE_SQL: Final = f"e.admission IN ({ADMITTED_SQL}) AND COALESCE(({_UPDATE_PENDING_SQL}), false)"
_PUSHED_CORE_SQL: Final = (
    f"e.admission IN ({ADMITTED_SQL}) AND COALESCE(d.state='sent', false)"
    f" AND NOT COALESCE({_SEMANTIC_OWED_SQL}, false)"
    " AND (nw.subject_id IS NULL OR (nw.state='done' AND COALESCE(nd.plan->>'action','') <> 'no_notification'))"
    " AND NOT COALESCE(q.state='pending'"
    " AND (nw.subject_id IS NULL OR q.content_revision=(nw.detail->>'content_revision')), false)"
)
OUTCOME_GROUP_SQL: Final = {
    "pushed": _PUSHED_CORE_SQL,
    "pending": _PENDING_CORE_SQL,
    "held": f"NOT ({_PENDING_CORE_SQL}) AND NOT ({_PUSHED_CORE_SQL})",
}
# The News feed contains editorial Events. Market observations are stored as
# facts beside their Item and read through `/api/news/market`.
EVENT_KIND_SQL: Final = ", ".join(f"'{value}'" for value in EVENT_KINDS)
EDITORIAL_EVENT_SQL: Final = f"e.event_kind IN ({EVENT_KIND_SQL})"
# Current adopted update citations and topics own these two filters. A source-only Event cannot
# claim a taxonomy or an authority it has not adopted.
SOURCE_AUTHORITY_PREDICATE: Final = (
    "EXISTS (SELECT 1 FROM jsonb_array_elements(u.document -> 'evidence') cited"
    " WHERE cited #>> '{source,source_authority}' = ANY(%s))"
)
SUBJECT_CODE_PREDICATE: Final = "u.document -> 'topics' ?| %s"
ASSET_SEARCH_PREDICATE: Final = (
    "EXISTS (SELECT 1 FROM news_event_assets a WHERE a.event_id = e.event_id AND a.symbol = ANY(%s))"
)
TEXT_SEARCH_PREDICATE: Final = "e.search_doc @@ websearch_to_tsquery('simple', %s)"
CURRENT_EVENT_CARD_SQL: Final = """
    SELECT e.event_id, e.leader_item_id, e.dedupe_family, e.comparison_fingerprint,
           e.comparison_title, e.leader_title, e.opened_at_ms, e.last_member_at_ms,
           e.expires_at_ms, e.member_count, e.admission, e.queue_priority, e.provider_score_max,
           e.engine_type, e.asset_class, e.grounded_assets, e.watchlist_hits, e.macro_lexicon,
           e.storyline_key, e.context_line, e.search_doc, e.published_at_ms,
           e.ingest_mode, e.trace_id, e.created_at_ms, e.updated_at_ms, e.focus_fact_id,
           e.focus_fact_text, e.focus_fact_context, e.focus_fact_method, e.focus_span_start,
           e.focus_span_end, e.event_kind,
           i.description AS leader_description, i.canonical_url AS leader_url, i.reporting_origin,
           i.provider_metadata, i.provenance, i.published_at_ms AS leader_published_at_ms,
           i.raw_first_line
      FROM news_events e
      JOIN news_items i ON i.item_id = e.leader_item_id
     WHERE e.event_id = %s
"""
# The public Event read uses the same editorial predicate as the feed.
EDITORIAL_EVENT_CARD_SQL: Final = f"{CURRENT_EVENT_CARD_SQL.rstrip()}\n       AND {EDITORIAL_EVENT_SQL}\n"
EVENT_MEMBERS_SQL: Final = """
            SELECT m.item_id, m.joined_at_ms, m.match_kind, m.jaccard_estimate, i.title, i.canonical_url,
                   i.reporting_origin, i.published_at_ms, i.provenance, i.description, m.fact_id, m.fact_text
              FROM news_event_members m JOIN news_items i ON i.item_id = m.item_id
             WHERE m.event_id = %s ORDER BY m.joined_at_ms, m.item_id
"""
STATUS_SOURCE_CONTRACTS_SQL: Final = """
    SELECT e.event_kind, count(*) AS received,
           count(*) FILTER (WHERE h.event_id IS NOT NULL) AS adopted
      FROM news_events e
      LEFT JOIN news_analyses h ON h.analysis_id=e.current_analysis_id
     WHERE e.opened_at_ms >= %s
       AND e.evidence_version IS NOT NULL
     GROUP BY e.event_kind
"""

STATUS_PIPELINE_SQL: Final = """
    WITH event_counts AS (
      SELECT count(*) FILTER (WHERE opened_at_ms >= %s) AS events_1h,
             count(*) AS events_24h,
             count(*) FILTER (WHERE admission='candidate') AS candidates_24h
        FROM news_events current_event
       WHERE current_event.opened_at_ms >= %s
         AND current_event.evidence_version IS NOT NULL
    ), decision_counts AS (
      SELECT count(*) AS decisions_24h,
             count(*) FILTER (WHERE plan->>'action'='notify') AS selected_24h
        FROM news_notifications
       WHERE kind='update' AND origin IN ('editorial_v1','reader_v2') AND decided_at_ms >= %s
    )
    SELECT event_counts.*,decision_counts.* FROM event_counts CROSS JOIN decision_counts
"""

STATUS_DELIVERY_SQL: Final = f"""
    WITH terminal AS NOT MATERIALIZED (
      SELECT event_id,error_code,settled_at_ms FROM news_notifications
       WHERE kind='update' AND state IN ('terminal','dead')
    )
    SELECT
      (SELECT count(*) FROM news_notifications d
         JOIN news_events e ON e.event_id = d.event_id
        WHERE d.kind='update' AND d.state = 'sent' AND d.settled_at_ms >= %s) AS sent_24h,
      (SELECT count(*) FROM news_notifications d
         JOIN news_events e ON e.event_id = d.event_id
        WHERE d.kind='update' AND d.state = 'sent' AND d.settled_at_ms >= %s) AS sent_1h,
      (SELECT count(*) FROM terminal d
         JOIN news_events e ON e.event_id = d.event_id
        WHERE d.settled_at_ms >= %s) AS terminal_24h,
      (SELECT d.error_code FROM terminal d
         JOIN news_events e ON e.event_id = d.event_id
        ORDER BY d.settled_at_ms DESC NULLS LAST LIMIT 1) AS last_error_code,
      (SELECT percentile_cont(0.5)
         WITHIN GROUP (ORDER BY (d.settled_at_ms - i.observed_at_ms)::double precision)
         FROM news_notifications d JOIN news_events e ON e.event_id = d.event_id
         JOIN news_items i ON i.item_id = e.leader_item_id
        WHERE d.kind='update' AND d.state = 'sent' AND d.kind IN {READER_DELIVERY_KINDS_SQL} AND d.settled_at_ms >= %s
          -- The first EventUpdate card this reader received.
          AND NOT EXISTS (
            SELECT 1 FROM news_notifications earlier
             WHERE earlier.event_id = d.event_id AND earlier.kind IN {READER_DELIVERY_KINDS_SQL}
               AND earlier.state = 'sent'
               AND (earlier.settled_at_ms, earlier.intent_id) < (d.settled_at_ms, d.intent_id)
          )) AS e2e_p50_ms,
      (SELECT percentile_cont(0.95)
         WITHIN GROUP (ORDER BY (d.settled_at_ms - i.observed_at_ms)::double precision)
         FROM news_notifications d JOIN news_events e ON e.event_id = d.event_id
         JOIN news_items i ON i.item_id = e.leader_item_id
        WHERE d.kind='update' AND d.state = 'sent' AND d.kind IN {READER_DELIVERY_KINDS_SQL} AND d.settled_at_ms >= %s
          -- The first EventUpdate card this reader received.
          AND NOT EXISTS (
            SELECT 1 FROM news_notifications earlier
             WHERE earlier.event_id = d.event_id AND earlier.kind IN {READER_DELIVERY_KINDS_SQL}
               AND earlier.state = 'sent'
               AND (earlier.settled_at_ms, earlier.intent_id) < (d.settled_at_ms, d.intent_id)
          )) AS e2e_p95_ms
"""  # noqa: S608

STATUS_FUNNEL_DECISIONS_SQL: Final = """
    SELECT plan->>'action' AS action,count(*) AS n
      FROM news_notifications
     WHERE kind='update' AND origin IN ('editorial_v1','reader_v2') AND decided_at_ms >= %s
     GROUP BY 1
"""

_JUDGED_SQL: Final = "current_event.current_analysis_id IS NOT NULL"
STATUS_FUNNEL_TOTALS_SQL: Final = f"""
    SELECT count(*) AS events,
           count(*) FILTER (WHERE admission IN ({ADMITTED_SQL})) AS admitted,
           count(*) FILTER (
             WHERE admission IN ({ADMITTED_SQL}) AND ({_JUDGED_SQL})
           ) AS adopted,
           count(*) FILTER (
             WHERE admission IN ({ADMITTED_SQL}) AND ({_JUDGED_SQL})
               AND EXISTS (
                 SELECT 1 FROM news_notifications d
                  WHERE d.event_id = current_event.event_id AND d.kind IN {READER_DELIVERY_KINDS_SQL}
                    AND d.state = 'sent'
               )
           ) AS delivered
      FROM news_events current_event WHERE current_event.opened_at_ms >= %s
       AND current_event.evidence_version IS NOT NULL
"""  # noqa: S608


# The joins the page and the count query share, after the Event, its leader Item and its current Evidence:
# the EventUpdate plane and the reader deliveries. Every
# EventUpdate join is on a primary key. `d` is the Event's representative ledger row -- the current
# revision's latest attempt, else its latest sent card, else its latest attempt -- over the
# `(event_id, kind)` index, so an earlier revision's receipt never hides what happened to the current one
# (#742 R1); `q` is its latest intent still owed with no ledger row.
_READER_DELIVERY_ORDER_SQL: Final = (
    "(dl.content_revision = current_head.content_revision) DESC, (dl.state = 'sent') DESC,"
    " dl.created_at_ms DESC, dl.intent_id DESC"
)
_FEED_PAGE_DELIVERY_SQL: Final = f"""
 LEFT JOIN LATERAL (
   SELECT dl.kind,dl.state,dl.settled_at_ms,
          CASE WHEN dl.state='sending' THEN NULL ELSE dl.error_code END AS error_code,
          dl.card,dl.plan_key,dl.content_revision,dl.card->>'payload_sha256' AS payload_sha256
   FROM news_notifications dl WHERE dl.event_id=e.event_id AND dl.kind='update'
    AND dl.state IN ('sending','sent','ambiguous','terminal')
   ORDER BY {_READER_DELIVERY_ORDER_SQL.replace("current_head.content_revision", "h.content_revision")} LIMIT 1
 ) d ON true
"""  # noqa: S608 -- code-owned ordering.
_FEED_COUNTS_DELIVERY_SQL: Final = f"""
 LEFT JOIN (
   SELECT DISTINCT ON (dl.event_id) dl.event_id,dl.state FROM news_notifications dl
   JOIN news_events de ON de.event_id=dl.event_id
   LEFT JOIN news_analyses current_head ON current_head.analysis_id=de.current_analysis_id
   WHERE dl.kind='update' AND dl.state IN ('sending','sent','ambiguous','terminal')
   ORDER BY dl.event_id,{_READER_DELIVERY_ORDER_SQL}
 ) d ON d.event_id=e.event_id
"""  # noqa: S608 -- code-owned ordering.


def _feed_joins_sql(*, bulk_deliveries: bool = False) -> str:
    delivery_join = _FEED_COUNTS_DELIVERY_SQL if bulk_deliveries else _FEED_PAGE_DELIVERY_SQL
    pending_join = f"""
      LEFT JOIN {"(" if bulk_deliveries else "LATERAL ("}
        SELECT {"DISTINCT ON (owed.event_id)" if bulk_deliveries else ""}
               owed.event_id,owed.state,owed.error_code,owed.content_revision,owed.card IS NOT NULL AS frozen_card
        FROM news_notifications owed WHERE {"" if bulk_deliveries else "owed.event_id=e.event_id AND "}
         owed.kind='update' AND owed.state IN ('pending','dead')
        ORDER BY {"owed.event_id," if bulk_deliveries else ""} owed.reserved_at_ms DESC,owed.intent_id DESC
        {"" if bulk_deliveries else "LIMIT 1"}
      ) q ON {"q.event_id=e.event_id" if bulk_deliveries else "true"}
    """  # noqa: S608 -- fixed alternatives.
    return f"""
      JOIN news_items i ON i.item_id=e.leader_item_id
      LEFT JOIN news_jobs sw ON sw.job_kind='semantic' AND sw.subject_id=e.event_id
      LEFT JOIN news_analyses h ON h.analysis_id=e.current_analysis_id
      LEFT JOIN LATERAL (SELECT h.document WHERE h.adopted_at_ms IS NOT NULL) u ON true
      LEFT JOIN news_jobs nw ON nw.job_kind='notify' AND nw.subject_id=e.event_id
      LEFT JOIN news_notifications nd ON nd.notification_id=nw.detail->>'decision_ref' AND nd.origin<>'legacy_delivery'
      {delivery_join}
      {pending_join}
    """


def feed_page_sql(where_sql: str) -> str:
    """Build the production page statement from one already-bound predicate list.

    The query audit calls this same builder with representative AssetSearch and TextSearch predicates,
    so its plans cannot silently drift back to a simplified SQL sketch.
    """

    return f"""
        SELECT e.event_id, e.event_kind, e.leader_title,
               e.opened_at_ms, e.last_member_at_ms, e.member_count,
               e.admission, e.provider_score_max, e.engine_type, e.asset_class, e.grounded_assets,
               e.watchlist_hits, e.storyline_key, e.context_line, e.published_at_ms, e.ingest_mode,
               i.canonical_url AS leader_url, i.reporting_origin, i.provenance,
               sw.subject_id IS NOT NULL AS has_semantic_work,
               (sw.detail->>'wanted_revision')::integer AS semantic_wanted_revision,
               (sw.detail->>'done_revision')::integer AS semantic_done_revision,
               (sw.detail->>'last_outcome') AS semantic_last_outcome,
               sw.last_error_code AS semantic_last_error_code,
               h.content_revision AS update_content_revision, h.adopted_at_ms AS update_adopted_at_ms,
               jsonb_array_length(u.document -> 'claims') AS update_claim_n,
               (SELECT COALESCE(jsonb_agg(asset ORDER BY claim_position, asset_position), '[]'::jsonb)
                  FROM jsonb_array_elements(u.document -> 'claims') WITH ORDINALITY AS listed(claim, claim_position)
                  CROSS JOIN LATERAL jsonb_array_elements(claim -> 'fields' -> 'assets')
                    WITH ORDINALITY AS named(asset, asset_position)
                 WHERE asset ->> 'role' = 'primary'
                   AND NOT (COALESCE(u.document -> 'retired_claim_refs', '[]'::jsonb) ? (claim ->> 'ref'))
                   AND NOT (COALESCE(u.document -> 'superseded_claim_refs', '[]'::jsonb) ? (claim ->> 'ref'))
               ) AS update_primary_assets,
               -- Twin of `update_view.headline_claim_statement`: what a later revision changed, else the lead claim.
               COALESCE(
                 (SELECT claim ->> 'statement'
                    FROM jsonb_array_elements(u.document -> 'changes') WITH ORDINALITY AS changed(change, position)
                    JOIN jsonb_array_elements(u.document -> 'claims') AS listed(claim)
                      ON listed.claim ->> 'ref' = changed.change ->> 'current_ref'
                   WHERE u.document ->> 'previous_content_revision' IS NOT NULL
                     AND changed.change ->> 'kind' IN ('new_fact', 'parameter_change', 'phase_change', 'scope_change',
                                                      'correction', 'conflict', 'possible_new')
                     AND NOT (COALESCE(u.document -> 'retired_claim_refs', '[]'::jsonb) ? (claim ->> 'ref'))
                     AND NOT (COALESCE(u.document -> 'superseded_claim_refs', '[]'::jsonb) ? (claim ->> 'ref'))
                   ORDER BY changed.position LIMIT 1),
                 (SELECT claim ->> 'statement'
                    FROM jsonb_array_elements(u.document -> 'claims') WITH ORDINALITY AS listed(claim, position)
                   WHERE NOT (COALESCE(u.document -> 'retired_claim_refs', '[]'::jsonb) ? (claim ->> 'ref'))
                     AND NOT (COALESCE(u.document -> 'superseded_claim_refs', '[]'::jsonb) ? (claim ->> 'ref'))
                   ORDER BY position LIMIT 1)
               ) AS update_claim_headline,
               nw.state AS notification_state, nw.attempts AS notification_attempts,
               nw.last_error_code AS notification_last_error_code,
               (nw.detail->>'content_revision') AS notification_content_revision,
               nd.plan ->> 'action' AS notification_action,
               nd.plan -> 'claim_decisions' AS notification_claim_decisions,
               d.kind AS delivery_kind, d.state AS delivery_state, d.settled_at_ms AS delivered_at_ms,
               d.content_revision AS delivery_content_revision, d.payload_sha256 AS delivery_payload_sha256,
               d.error_code AS delivery_error_code, d.plan_key AS delivery_plan_key,
               CASE WHEN d.kind = 'update' AND d.state = 'sent' AND d.content_revision=h.content_revision
                    THEN NULLIF(btrim(d.card ->> 'headline_zh'), '') END AS sent_update_headline,
               q.state AS delivery_queue_state, q.error_code AS delivery_queue_error_code,
               q.content_revision AS delivery_queue_content_revision, q.frozen_card AS delivery_queue_frozen
          FROM news_events e
          {_feed_joins_sql()}
         WHERE {where_sql}
         ORDER BY e.opened_at_ms DESC, e.event_id DESC
         LIMIT %s
    """  # noqa: S608


def feed_counts_sql(where_sql: str) -> str:
    """Build the production first-page count statement for the same predicate list as the page."""

    return f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE {OUTCOME_GROUP_SQL["pushed"]}) AS pushed,
               count(*) FILTER (WHERE {OUTCOME_GROUP_SQL["held"]}) AS held,
               count(*) FILTER (WHERE {OUTCOME_GROUP_SQL["pending"]}) AS pending
          FROM news_events e
          {_feed_joins_sql(bulk_deliveries=True)}
         WHERE {where_sql}
    """  # noqa: S608


__all__ = [
    "ADMITTED_SQL",
    "ASSET_SEARCH_PREDICATE",
    "CURRENT_EVENT_CARD_SQL",
    "EDITORIAL_EVENT_CARD_SQL",
    "EDITORIAL_EVENT_SQL",
    "EVENT_KIND_SQL",
    "EVENT_MEMBERS_SQL",
    "OUTCOME_GROUP_SQL",
    "READER_DELIVERY_KINDS_SQL",
    "SOURCE_AUTHORITY_PREDICATE",
    "SUBJECT_CODE_PREDICATE",
    "TEXT_SEARCH_PREDICATE",
    "feed_counts_sql",
    "feed_page_sql",
]
