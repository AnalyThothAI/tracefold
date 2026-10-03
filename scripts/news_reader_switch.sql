-- Run only with Workers stopped. Reversible reservation cleanup; immutable
-- decisions, adopted knowledge and settled delivery evidence are preserved.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '10s';
LOCK TABLE news_notifications IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE news_jobs IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM news_notifications WHERE kind='update' AND state='sending') THEN
    RAISE check_violation USING MESSAGE='news_reader_switch_sending_not_drained';
  END IF;
  IF EXISTS (
    SELECT 1 FROM news_notifications WHERE kind='update' AND state='pending'
      AND lease_token IS NOT NULL
      AND lease_until_ms > floor(extract(epoch FROM transaction_timestamp()) * 1000)::bigint
  ) THEN
    RAISE check_violation USING MESSAGE='news_reader_switch_pending_lease_active';
  END IF;
  IF EXISTS (
    SELECT 1 FROM news_notifications WHERE kind='update' AND state='pending'
      AND (attempts > 0 OR attempted_at_ms IS NOT NULL OR settlement IS NOT NULL OR error_code IS NOT NULL)
  ) THEN
    -- Clearing intent_id would disconnect the next plan from its existing
    -- bounded failure budget and provider's explicitly not_sent evidence.
    RAISE check_violation USING MESSAGE='news_reader_switch_pending_attempt_history';
  END IF;
  IF EXISTS (
    SELECT 1 FROM news_notifications n WHERE n.kind='update' AND n.state='pending'
      AND NOT EXISTS (SELECT 1 FROM news_jobs j WHERE j.job_kind='notify'
        AND j.subject_id=n.event_id AND j.state IN ('pending','done'))
  ) THEN
    RAISE check_violation USING MESSAGE='news_reader_switch_notify_work_not_replannable';
  END IF;
END $$;

WITH cleared AS (
  UPDATE news_notifications SET state='decided',intent_id=NULL,content_revision=NULL,
    claim_refs=NULL,plan_key=NULL,attempts=0,next_attempt_at_ms=NULL,
    lease_token=NULL,lease_until_ms=NULL,card=NULL,card_copy_input_digest=NULL,
    card_copy_document=NULL,settlement=NULL,error_code=NULL,
    reserved_at_ms=NULL,last_attempt_at_ms=NULL,
    updated_at_ms=floor(extract(epoch FROM transaction_timestamp()) * 1000)::bigint
  WHERE kind='update' AND state='pending'
  RETURNING notification_id,event_id
), woken AS (
  UPDATE news_jobs SET state='pending',
    next_attempt_at_ms=floor(extract(epoch FROM transaction_timestamp()) * 1000)::bigint,
    updated_at_ms=floor(extract(epoch FROM transaction_timestamp()) * 1000)::bigint
  WHERE job_kind='notify' AND state IN ('pending','done')
    AND subject_id IN (SELECT event_id FROM cleared)
  RETURNING subject_id
)
SELECT (SELECT count(*) FROM cleared) AS cleared_pending_reservations,
       (SELECT count(*) FROM woken) AS due_notify_jobs;
COMMIT;
