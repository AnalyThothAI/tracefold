"""Public receipt and work projections over the shared notification tables.

These are read-only projections. Writers always target the base row and its state fence.
"""

from typing import Final

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
