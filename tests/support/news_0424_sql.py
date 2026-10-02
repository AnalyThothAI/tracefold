"""Read projections of the single analysis chain and semantic job ledger."""

from typing import Final

ANALYSES_SQL: Final = """
SELECT event_id,content_revision,input_revision,previous_content_revision,adopted_at_ms,update_ref,
       CASE WHEN origin='semantic' THEN analysis_id END AS observation_result_id,
       CASE WHEN origin='scope_repair' THEN analysis_id END AS scope_repair_id,document
  FROM news_analyses WHERE adopted_at_ms IS NOT NULL
"""
ANALYSIS_HEADS_SQL: Final = """
SELECT a.event_id,a.content_revision,a.input_revision,a.update_ref,a.adopted_at_ms
  FROM news_events e JOIN news_analyses a ON a.analysis_id=e.current_analysis_id
"""
SEMANTIC_RESULTS_SQL: Final = """
SELECT analysis_id AS result_id,work_id,event_id,input_revision,input_sha256,program_identity,
       completed_at_ms,understanding,read_refs,reanalysis_reason,reanalysis_head_ref,input_manifest
  FROM news_analyses WHERE origin='semantic'
"""
SEMANTIC_JOBS_SQL: Final = """
SELECT j.subject_id AS event_id,j.attempts,j.next_attempt_at_ms,j.lease_token,
       j.lease_until_ms AS leased_until_ms,j.last_error_code,j.updated_at_ms,
       d.wanted_revision,d.done_revision,d.lineage_id,d.published_at_ms,d.last_outcome,
       d.extra_read_state,d.extra_read_target_ref,d.attached_evidence,d.focus_claim_refs,
       d.processed_read_refs,d.failed_read_refs,d.attempt_read_refs,d.reanalysis_read_ref,
       d.reanalysis_reason,d.reanalysis_head_ref
  FROM news_jobs j CROSS JOIN LATERAL jsonb_to_record(j.detail) AS d(
    wanted_revision integer,done_revision integer,lineage_id text,published_at_ms bigint,last_outcome text,
    extra_read_state text,extra_read_target_ref text,attached_evidence jsonb,focus_claim_refs jsonb,
    processed_read_refs text[],failed_read_refs text[],attempt_read_refs text[],reanalysis_read_ref text,
    reanalysis_reason text,reanalysis_head_ref text)
 WHERE j.job_kind='semantic'
"""
ITEM_REVISIONS_SQL: Final = """
SELECT i.item_id,r.revision_sha256,r.content_sha256,r.previous_revision_sha256,r.revision_sequence,
       r.evidence_text,r.provider_params,r.reporting_origin,r.canonical_url,r.source_artifact_id,
       r.published_at_ms,r.received_at_ms
  FROM news_items i CROSS JOIN LATERAL jsonb_to_recordset(i.revisions) AS r(
  revision_sha256 text,content_sha256 text,previous_revision_sha256 text,revision_sequence bigint,
  evidence_text text,provider_params jsonb,reporting_origin text,canonical_url text,source_artifact_id text,
  published_at_ms bigint,received_at_ms bigint)
"""
EVIDENCE_VERSIONS_SQL: Final = """
SELECT e.event_id,v.evidence_version,v.evidence_sha256,v.focus_fact_id,v.created_at_ms,
       'observed'::text AS provenance,true AS release_eligible,
       jsonb_build_object('schema_version','news_event_evidence_v3') AS snapshot
  FROM news_events e CROSS JOIN LATERAL jsonb_to_recordset(e.evidence->'versions') AS v(
    evidence_version integer,evidence_sha256 text,focus_fact_id text,created_at_ms bigint)
"""
CLAIM_LINKS_SQL: Final = """
SELECT DISTINCT ON (a.update_ref,change->>'current_ref',change->>'previous_ref')
       a.update_ref,change->>'current_ref' AS current_ref,change->>'previous_ref' AS previous_ref,
       change->>'relation' AS relation,a.event_id AS current_event_id,p.event_id AS previous_event_id,
       a.adopted_at_ms AS asserted_at_ms
  FROM news_analyses a CROSS JOIN LATERAL jsonb_array_elements(a.document->'changes')
       WITH ORDINALITY AS changes(change,position)
  LEFT JOIN LATERAL (
    SELECT prior.event_id FROM news_analyses prior
     WHERE prior.adopted_at_ms<=a.adopted_at_ms
       AND EXISTS (SELECT 1 FROM jsonb_array_elements(prior.document->'claims') claim
                    WHERE claim->>'ref'=change->>'previous_ref')
     ORDER BY (prior.event_id=a.event_id) DESC,prior.adopted_at_ms DESC,prior.analysis_id LIMIT 1
  ) p ON true
 WHERE a.adopted_at_ms IS NOT NULL AND change->>'previous_ref' IS NOT NULL
   AND change->>'previous_ref'<>change->>'current_ref'
   AND change->>'relation' IN ('equivalent','adds_information','real_world_change','corrects','conflicts')
 ORDER BY a.update_ref,change->>'current_ref',change->>'previous_ref',position
"""

"""Public receipt and work projections over the shared notification tables.

These are read-only projections. Writers always target the base row and its state fence.
"""


NOTIFICATION_DECISIONS_SQL: Final = """
 SELECT notification_id AS decision_ref,event_id,update_ref,'news'::text AS channel,
        input_digest,input_snapshot,plan,origin,decided_at_ms AS created_at_ms
 FROM news_notifications WHERE kind='update' AND origin<>'legacy_delivery'
"""
UPDATE_RECEIPTS_SQL: Final = """
 SELECT CASE WHEN origin='legacy_delivery' THEN NULL ELSE notification_id END AS decision_ref,intent_id,event_id,
        kind,state,card,receipt,
        CASE WHEN state='sending' THEN NULL ELSE error_code END AS error_code,
        CASE WHEN state='sending' THEN NULL ELSE settlement END AS settlement,
        attempted_at_ms,settled_at_ms,created_at_ms,edit_state,pending_card,edit_error_code,
        edit_attempted_at_ms,edit_settled_at_ms,history_context,content_revision,claim_refs,
        card->>'body' AS body,card->>'payload_sha256' AS payload_sha256,plan_key,
        card_copy_input_digest,card_copy_document,sent_claims
 FROM news_notifications WHERE kind='update' AND state IN ('sending','sent','ambiguous','terminal')
"""
UPDATE_PENDING_SQL: Final = """
 SELECT CASE WHEN origin='legacy_delivery' THEN NULL ELSE notification_id END AS decision_ref,intent_id,event_id,
        kind,
        CASE WHEN state='sending' THEN 'pending' WHEN state='terminal' THEN 'dead' ELSE state END AS state,
        attempts,error_code,reserved_at_ms AS enqueued_at_ms,next_attempt_at_ms,last_attempt_at_ms,
        CASE WHEN state='sending' THEN NULL ELSE settled_at_ms END AS settled_at_ms,
        updated_at_ms,content_revision,claim_refs,plan_key,card AS frozen_card,lease_token,
        card_copy_input_digest,card_copy_document,settlement AS last_settlement
 FROM news_notifications WHERE kind='update' AND (state IN ('pending','dead','sending') OR (state='terminal' AND
        reserved_at_ms IS NOT NULL))
"""
NOTIFY_JOBS_SQL: Final = """
 SELECT subject_id AS event_id,'news'::text AS channel,state,attempts,next_attempt_at_ms,
        last_error_code,updated_at_ms,detail->>'content_revision' AS content_revision,
        detail->>'reader_revision' AS reader_revision,detail->>'decision_ref' AS decision_ref
 FROM news_jobs WHERE job_kind='notify'
"""
MARKET_NOTIFICATIONS_SQL: Final = """
 SELECT notification_id AS delivery_key,group_key,market_kind,trigger_reason,
        trigger_observation_id AS trigger_item_id,state,attempts,covered_count,covered_from_ms,
        covered_to_ms,card,receipt,error_code AS error,next_attempt_at_ms,
        attempted_at_ms AS first_attempt_at_ms,last_attempt_at_ms,settled_at_ms,created_at_ms,updated_at_ms
 FROM news_notifications WHERE kind='market'
"""
MARKET_JOBS_SQL: Final = """
 SELECT subject_id AS group_key,next_attempt_at_ms AS next_due_at_ms,created_at_ms,updated_at_ms,
        d.* FROM news_jobs j CROSS JOIN LATERAL jsonb_to_record(j.detail) AS d(
          market_kind text,family text,provider text,source_venue text,venue_known boolean,
          raw_instrument text,symbol text,measurement_definition text,liquidated_position_side text,
          account_key text,account_verified boolean,trader_label text,last_observed_at_ms bigint,
          last_observed_item_id text,anchor_state text,anchor_delivery_key text,anchor_attempt_at_ms bigint,
          anchor_oi_change_bps bigint,anchor_direction text,anchor_action text,anchor_position_side text,
          open_delivery_key text,pending_reason text,round_started_at_ms bigint)
 WHERE job_kind='market_notify'
"""


def smart_money_source_key(*, item_id: str, fact_id: str) -> str:
    """Historical row identity, independent of the current observation parser."""
    import hashlib

    return hashlib.sha256(f"{item_id}\x1f{fact_id}\x1fsmart_money_parser_v1".encode()).hexdigest()
