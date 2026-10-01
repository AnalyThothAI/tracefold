"""#764 P2: one notification ledger and shared jobs.

Migration evidence:
- category: forward-only projection-preserving consolidation.
- why_database_must_change: one durable row per notification; remove queue/ledger copies and ReviewDesk.
- current_source_revision: 20261001_0421
- minimum_supported_source_revision: 20261001_0421
- lock_level_and_order: ACCESS EXCLUSIVE on source tables; stop Workers then Serve.
- statement_timeout: 1800s
- lock_timeout: 5s
- estimated_rows: source notification rows and market tracks; measure backup before rollout.
- estimated_bytes: source relation sizes; data-dependent.
- rewrite_or_index_build: full backfill and shared partial indexes.
- preflight_and_maintenance_boundary: export all nine retired tables, verify backup and sha256.
- archive_current_compatibility: IDs, frozen cards, leases, judgments and receipts preserved.
- role_and_grant_impact: none; existing single owner.
- failure_state: transaction rolls back on source mismatch.
- roll_forward_or_verified_backup_restore: verified backup with matching old image.
- production_postgres_image: postgres:18-bookworm
"""

from alembic import op

revision = "20261001_0422"
down_revision = "20261001_0421"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '1800s';
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()
    AND (application_name LIKE 'tracefold_workers%' OR application_name LIKE 'tracefold_serve%'))
 THEN RAISE EXCEPTION 'p2_news_writers_connected'; END IF;
 IF EXISTS (SELECT 1 FROM news_notification_feedback)
 OR EXISTS (SELECT 1 FROM news_notification_external_feedback)
 OR EXISTS (SELECT 1 FROM news_external_miss_snapshots)
 THEN RAISE EXCEPTION 'p2_reviewdesk_not_empty'; END IF;
 IF EXISTS (SELECT decision_ref FROM news_delivery_queue WHERE decision_ref IS NOT NULL GROUP BY 1 HAVING
        count(*)>1)
 OR EXISTS (SELECT decision_ref FROM news_deliveries WHERE decision_ref IS NOT NULL GROUP BY 1 HAVING
        count(*)>1)
 THEN RAISE EXCEPTION 'p2_multiple_intents_per_decision'; END IF;
 IF EXISTS (SELECT 1 FROM news_delivery_queue q JOIN news_deliveries d USING(intent_id)
   WHERE (q.event_id,q.content_revision,q.claim_refs,q.plan_key,q.decision_ref,q.frozen_card)
       IS DISTINCT FROM (d.event_id,d.content_revision,d.claim_refs,d.plan_key,d.decision_ref,d.card)
      OR NOT ((q.state='pending' AND d.state='sending') OR (q.state='dead' AND d.state='terminal')))
 THEN RAISE EXCEPTION 'p2_queue_ledger_mismatch'; END IF;
 IF EXISTS (SELECT 1 FROM news_deliveries WHERE body IS DISTINCT FROM card->>'body'
    OR payload_sha256 IS DISTINCT FROM card->>'payload_sha256')
 THEN RAISE EXCEPTION 'p2_card_payload_mismatch'; END IF;
END $$;
CREATE TABLE news_jobs (
  job_kind text NOT NULL CHECK (job_kind IN ('semantic','notify','market_notify')), subject_id text NOT NULL,
  state text NOT NULL CHECK (state IN ('pending','running','done','failed')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0), next_attempt_at_ms bigint,
  lease_token text, lease_until_ms bigint, last_error_code text,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(detail) = 'object'),
  created_at_ms bigint NOT NULL, updated_at_ms bigint NOT NULL,
  PRIMARY KEY (job_kind, subject_id), CHECK ((lease_token IS NULL) = (lease_until_ms IS NULL))
) WITH (fillfactor = 85);
CREATE INDEX news_jobs_due ON news_jobs (job_kind, next_attempt_at_ms) WHERE state = 'pending'; -- 到期 job
CREATE TABLE news_notifications (
  notification_id text PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('update','market')),
  origin text CHECK (origin IN ('reader_v2','editorial_v1','legacy_work_plan','legacy_delivery')),
  event_id text REFERENCES news_events(event_id) ON DELETE CASCADE, group_key text,
  market_kind text CHECK (market_kind IN ('oi','liquidation','smart_money','unknown_market','wallet')),
  trigger_reason text CHECK (trigger_reason IN ('first','followup','action_change','raw')),
  trigger_observation_id text REFERENCES news_market_observations(observation_id) ON DELETE CASCADE,
  update_ref text, input_digest text, input_snapshot jsonb CHECK (jsonb_typeof(input_snapshot) = 'object'),
  plan jsonb CHECK (jsonb_typeof(plan) = 'object'), decided_at_ms bigint,
  state text NOT NULL, intent_id text, content_revision text, claim_refs jsonb CHECK (jsonb_typeof(claim_refs) =
        'array'),
  plan_key boolean, attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3), next_attempt_at_ms
        bigint,
  lease_token text, lease_until_ms bigint,
  card jsonb CHECK (jsonb_typeof(card) = 'object'), card_copy_input_digest text, card_copy_document jsonb,
  history_context jsonb, sent_claims jsonb CHECK (jsonb_typeof(sent_claims) = 'array'),
  covered_count integer CHECK (covered_count >= 0), covered_from_ms bigint, covered_to_ms bigint,
  receipt jsonb, settlement jsonb CHECK (jsonb_typeof(settlement) = 'object'), error_code text,
  reserved_at_ms bigint, last_attempt_at_ms bigint, attempted_at_ms bigint, settled_at_ms bigint,
  edit_state text CHECK (edit_state IN ('editing','edited','ambiguous')), pending_card jsonb,
  edit_error_code text, edit_attempted_at_ms bigint, edit_settled_at_ms bigint, created_at_ms bigint NOT NULL,
  updated_at_ms bigint NOT NULL,
  CONSTRAINT news_notifications_subject_check CHECK ((
    (kind = 'update' AND origin IS NOT NULL AND event_id IS NOT NULL AND group_key IS NULL AND
        trigger_observation_id IS NULL
     AND state IN ('decided','pending','dead','sending','sent','ambiguous','terminal'))
    OR (kind = 'market' AND origin IS NULL AND event_id IS NULL AND group_key IS NOT NULL AND market_kind IS NOT
        NULL
     AND trigger_reason IS NOT NULL AND trigger_observation_id IS NOT NULL AND card IS NOT NULL
     AND state IN ('pending','unavailable','sending','sent','failed','unknown'))) IS TRUE),
  CONSTRAINT news_notifications_decision_check CHECK ((origin IS NULL OR origin = 'legacy_delivery'
    OR (update_ref IS NOT NULL AND plan IS NOT NULL AND input_snapshot IS NOT NULL AND decided_at_ms IS NOT NULL
    AND (origin = 'legacy_work_plan' OR input_digest IS NOT NULL))) IS TRUE),
  CONSTRAINT news_notifications_intent_check CHECK ((kind = 'market' OR ((state = 'decided') = (intent_id IS
        NULL)
    AND (intent_id IS NULL OR (content_revision IS NOT NULL AND claim_refs IS NOT NULL AND plan_key IS NOT
        NULL)))) IS TRUE),
  CONSTRAINT news_notifications_settled_check CHECK (
    (settled_at_ms IS NOT NULL) = (state IN ('sent','ambiguous','terminal','dead','failed','unknown'))),
  CONSTRAINT news_notifications_market_receipt_check CHECK ((
    kind = 'update' OR (((state = 'sent') = (receipt IS NOT NULL)) AND ((attempts = 0) = (card = '{}'::jsonb))))
        IS TRUE),
  CONSTRAINT news_notifications_lease_check CHECK ((lease_token IS NULL) = (lease_until_ms IS NULL))
) WITH (fillfactor = 85);
CREATE UNIQUE INDEX news_notifications_intent ON news_notifications (intent_id) WHERE intent_id IS NOT NULL; --
        -- 单次发送
CREATE UNIQUE INDEX news_notifications_market_open ON news_notifications (group_key)
  WHERE kind = 'market' AND state IN ('pending','unavailable') AND attempts = 0; -- 组内未开始卡片
CREATE INDEX news_notifications_event ON news_notifications (event_id) WHERE event_id IS NOT NULL; -- 事件详情
CREATE INDEX news_notifications_due ON news_notifications (kind, next_attempt_at_ms) WHERE state IN ('pending',
        'unavailable'); -- 到期 SKIP LOCKED
CREATE INDEX news_notifications_sent ON news_notifications (settled_at_ms) WHERE kind = 'update' AND state =
        'sent'; -- 回执召回
CREATE INDEX news_notifications_sending ON news_notifications (attempted_at_ms) WHERE state = 'sending'; -- 孤儿清扫
CREATE INDEX news_notifications_editing ON news_notifications (edit_attempted_at_ms) WHERE edit_state =
        'editing'; -- 超时编辑
CREATE INDEX news_notifications_copy_input ON news_notifications (card_copy_input_digest) WHERE
        card_copy_document IS NOT NULL; -- 文案复用
CREATE INDEX news_notifications_trigger ON news_notifications (trigger_observation_id) WHERE
        trigger_observation_id IS NOT NULL; -- 观测级联
CREATE INDEX news_notifications_market_created ON news_notifications (created_at_ms) WHERE kind = 'market'; --
        -- 卡片汇总
CREATE INDEX news_notifications_decided ON news_notifications (decided_at_ms) WHERE origin IN ('editorial_v1',
        'reader_v2'); -- 判断计数

INSERT INTO news_jobs(job_kind,subject_id,state,attempts,next_attempt_at_ms,last_error_code,detail,created_at_ms,
        updated_at_ms)
 SELECT 'notify',event_id,state,attempts,next_attempt_at_ms,last_error_code,
   jsonb_build_object('content_revision',content_revision,'reader_revision',reader_revision,'decision_ref',
        decision_ref),
   updated_at_ms,updated_at_ms FROM news_notification_work;
INSERT INTO news_jobs(job_kind,subject_id,state,next_attempt_at_ms,detail,created_at_ms,updated_at_ms)
 SELECT 'market_notify',group_key,CASE WHEN open_delivery_key IS NULL THEN 'done' ELSE 'pending' END,
   next_due_at_ms,to_jsonb(t)-'{group_key,next_due_at_ms,created_at_ms,updated_at_ms}'::text[],
   created_at_ms,updated_at_ms FROM news_market_tracks t;

CREATE TEMP TABLE p2_send_source ON COMMIT DROP AS
 SELECT COALESCE(d.intent_id,q.intent_id) AS intent_id,
   COALESCE(d.decision_ref,q.decision_ref) AS decision_ref, COALESCE(d.event_id,q.event_id) AS event_id,
   COALESCE(d.state,q.state) AS state,COALESCE(d.card,q.frozen_card) AS card,
   COALESCE(d.content_revision,q.content_revision) AS content_revision,
   COALESCE(d.claim_refs,q.claim_refs) AS claim_refs,COALESCE(d.plan_key,q.plan_key) AS plan_key,
   COALESCE(q.attempts,0) AS attempts,q.next_attempt_at_ms,q.lease_token,
   CASE WHEN q.lease_token IS NOT NULL THEN q.next_attempt_at_ms END AS lease_until_ms,
   q.enqueued_at_ms AS reserved_at_ms,q.last_attempt_at_ms,d.attempted_at_ms,
   COALESCE(d.settled_at_ms,q.settled_at_ms) AS settled_at_ms,
   CASE WHEN d.state='sending' THEN q.error_code ELSE COALESCE(d.error_code,q.error_code) END AS error_code,
   CASE WHEN d.state='sending' THEN q.last_settlement ELSE COALESCE(d.settlement,q.last_settlement) END AS
        settlement,
   COALESCE(d.card_copy_input_digest,q.card_copy_input_digest) AS card_copy_input_digest,
   COALESCE(d.card_copy_document,q.card_copy_document) AS card_copy_document,
   d.receipt,d.history_context,d.sent_claims,d.edit_state,d.pending_card,d.edit_error_code,
   d.edit_attempted_at_ms,d.edit_settled_at_ms,
   COALESCE(d.created_at_ms,q.enqueued_at_ms) AS created_at_ms,
   COALESCE(q.updated_at_ms,d.settled_at_ms,d.created_at_ms) AS updated_at_ms
 FROM news_deliveries d FULL JOIN news_delivery_queue q USING(intent_id);

INSERT INTO news_notifications(notification_id,kind,origin,event_id,update_ref,input_digest,input_snapshot,plan,
        decided_at_ms,
 state,intent_id,content_revision,claim_refs,plan_key,attempts,next_attempt_at_ms,lease_token,lease_until_ms,
        card,
 card_copy_input_digest,card_copy_document,history_context,sent_claims,receipt,settlement,error_code,
        reserved_at_ms,
 last_attempt_at_ms,attempted_at_ms,settled_at_ms,edit_state,pending_card,edit_error_code,edit_attempted_at_ms,
 edit_settled_at_ms,created_at_ms,updated_at_ms)
 SELECT n.decision_ref,'update',n.origin,n.event_id,n.update_ref,n.input_digest,n.input_snapshot,n.plan,
        n.created_at_ms,
 COALESCE(s.state,'decided'),s.intent_id,s.content_revision,s.claim_refs,s.plan_key,COALESCE(s.attempts,0),
 s.next_attempt_at_ms,s.lease_token,s.lease_until_ms,s.card,s.card_copy_input_digest,s.card_copy_document,
 s.history_context,s.sent_claims,s.receipt,s.settlement,s.error_code,s.reserved_at_ms,s.last_attempt_at_ms,
        s.attempted_at_ms,
 s.settled_at_ms,s.edit_state,s.pending_card,s.edit_error_code,s.edit_attempted_at_ms,s.edit_settled_at_ms,
 COALESCE(s.created_at_ms,n.created_at_ms),COALESCE(s.updated_at_ms,n.created_at_ms)
 FROM news_notification_decisions n LEFT JOIN p2_send_source s ON s.decision_ref=n.decision_ref;
INSERT INTO news_notifications(notification_id,kind,origin,event_id,state,intent_id,content_revision,claim_refs,
        plan_key,
 attempts,next_attempt_at_ms,lease_token,lease_until_ms,card,card_copy_input_digest,card_copy_document,
 history_context,sent_claims,receipt,settlement,error_code,reserved_at_ms,last_attempt_at_ms,attempted_at_ms,
 settled_at_ms,edit_state,pending_card,edit_error_code,edit_attempted_at_ms,edit_settled_at_ms,created_at_ms,
        updated_at_ms)
 SELECT intent_id,'update','legacy_delivery',event_id,state,intent_id,content_revision,claim_refs,plan_key,
 attempts,next_attempt_at_ms,lease_token,lease_until_ms,card,card_copy_input_digest,card_copy_document,
 history_context,sent_claims,receipt,settlement,error_code,reserved_at_ms,last_attempt_at_ms,attempted_at_ms,
 settled_at_ms,edit_state,pending_card,edit_error_code,edit_attempted_at_ms,edit_settled_at_ms,created_at_ms,
        updated_at_ms
 FROM p2_send_source WHERE decision_ref IS NULL;
INSERT INTO news_notifications(notification_id,kind,group_key,market_kind,trigger_reason,trigger_observation_id,
 state,attempts,covered_count,covered_from_ms,covered_to_ms,card,receipt,error_code,next_attempt_at_ms,
 attempted_at_ms,last_attempt_at_ms,settled_at_ms,created_at_ms,updated_at_ms)
 SELECT delivery_key,'market',group_key,market_kind,trigger_reason,trigger_item_id,state,attempts,
 covered_count,covered_from_ms,covered_to_ms,card,receipt,error,next_attempt_at_ms,
 first_attempt_at_ms,last_attempt_at_ms,settled_at_ms,created_at_ms,updated_at_ms FROM news_market_deliveries;

-- Compare every persisted source column to its reconstructed projection, with multiplicity.
CREATE FUNCTION pg_temp.p2_verify(label text, before_query text, after_query text) RETURNS void
 LANGUAGE plpgsql AS $$
DECLARE before_count bigint; after_count bigint; before_hash text; after_hash text; different boolean;
BEGIN
 EXECUTE
        'SELECT count(*),md5(COALESCE(string_agg(to_jsonb(r)::text, '
        ||''''' ORDER BY to_jsonb(r)::text),'''')) FROM ('||before_query||') r'
 INTO before_count,before_hash;
 EXECUTE
        'SELECT count(*),md5(COALESCE(string_agg(to_jsonb(r)::text, '
        ||''''' ORDER BY to_jsonb(r)::text),'''')) FROM ('||after_query||') r'
 INTO after_count,after_hash;
 EXECUTE
        'SELECT EXISTS(('||before_query||' EXCEPT ALL '||after_query||') UNION ALL ('
        ||after_query||' EXCEPT ALL '||before_query||'))'
        INTO different;
 IF different OR (before_count,before_hash) IS DISTINCT FROM (after_count,after_hash)
 THEN RAISE EXCEPTION 'p2_verify_% failed: % -> %',label,before_count,after_count; END IF;
 RAISE NOTICE 'p2_verify_% ok: % rows',label,before_count;
END $$;
SELECT pg_temp.p2_verify('news_notification_decisions', $q$SELECT * FROM news_notification_decisions$q$,
        $q$SELECT notification_id AS decision_ref,event_id AS event_id,update_ref AS update_ref,'news'::text AS channel,
        input_digest AS input_digest,input_snapshot AS input_snapshot,plan AS plan,origin AS origin,decided_at_ms AS
        created_at_ms FROM news_notifications WHERE kind='update' AND origin<>'legacy_delivery'$q$);
SELECT pg_temp.p2_verify('news_deliveries', $q$SELECT * FROM news_deliveries$q$, $q$SELECT event_id AS event_id,
        kind AS kind,state AS state,card AS card,receipt AS receipt,CASE WHEN state='sending' THEN NULL ELSE error_code
        END AS error_code,attempted_at_ms AS attempted_at_ms,settled_at_ms AS settled_at_ms,created_at_ms AS
        created_at_ms,edit_state AS edit_state,pending_card AS pending_card,edit_error_code AS edit_error_code,
        edit_attempted_at_ms AS edit_attempted_at_ms,edit_settled_at_ms AS edit_settled_at_ms,history_context AS
        history_context,intent_id AS intent_id,content_revision AS content_revision,claim_refs AS claim_refs,
        card->>'body' AS body,card->>'payload_sha256' AS payload_sha256,plan_key AS plan_key,CASE WHEN
        origin='legacy_delivery' THEN NULL ELSE notification_id END AS decision_ref,card_copy_input_digest AS
        card_copy_input_digest,card_copy_document AS card_copy_document,CASE WHEN state='sending' THEN NULL ELSE
        settlement END AS settlement,sent_claims AS sent_claims FROM news_notifications WHERE kind='update' AND state IN
        ('sending','sent','ambiguous','terminal')$q$);
SELECT pg_temp.p2_verify('news_delivery_queue', $q$SELECT * FROM news_delivery_queue$q$, $q$SELECT event_id AS
        event_id,kind AS kind,CASE WHEN state='sending' THEN 'pending' WHEN state='terminal' THEN 'dead' ELSE state END
        AS state,attempts AS attempts,error_code AS error_code,reserved_at_ms AS enqueued_at_ms,next_attempt_at_ms AS
        next_attempt_at_ms,last_attempt_at_ms AS last_attempt_at_ms,CASE WHEN state='sending' THEN NULL ELSE
        settled_at_ms END AS settled_at_ms,updated_at_ms AS updated_at_ms,intent_id AS intent_id,content_revision AS
        content_revision,claim_refs AS claim_refs,plan_key AS plan_key,card AS frozen_card,lease_token AS lease_token,
        CASE WHEN origin='legacy_delivery' THEN NULL ELSE notification_id END AS decision_ref,card_copy_input_digest AS
        card_copy_input_digest,card_copy_document AS card_copy_document,settlement AS last_settlement FROM
        news_notifications WHERE kind='update' AND (state IN ('pending','dead','sending') OR
        (state='terminal' AND reserved_at_ms IS NOT NULL))$q$);
SELECT pg_temp.p2_verify('news_notification_work', $q$SELECT * FROM news_notification_work$q$, $q$SELECT
        subject_id AS event_id,'news'::text AS channel,detail->>'content_revision' AS content_revision,state AS state,
        detail->>'reader_revision' AS reader_revision,attempts AS attempts,next_attempt_at_ms AS next_attempt_at_ms,
        updated_at_ms AS updated_at_ms,detail->>'decision_ref' AS decision_ref,last_error_code AS last_error_code FROM
        news_jobs WHERE job_kind='notify'$q$);
SELECT pg_temp.p2_verify('news_market_deliveries', $q$SELECT * FROM news_market_deliveries$q$, $q$SELECT
        notification_id AS delivery_key,group_key AS group_key,market_kind AS market_kind,trigger_reason AS
        trigger_reason,trigger_observation_id AS trigger_item_id,state AS state,attempts AS attempts,covered_count AS
        covered_count,covered_from_ms AS covered_from_ms,covered_to_ms AS covered_to_ms,card AS card,receipt AS receipt,
        error_code AS error,next_attempt_at_ms AS next_attempt_at_ms,attempted_at_ms AS first_attempt_at_ms,
        last_attempt_at_ms AS last_attempt_at_ms,settled_at_ms AS settled_at_ms,created_at_ms AS created_at_ms,
        updated_at_ms AS updated_at_ms FROM news_notifications WHERE kind='market'$q$);
SELECT pg_temp.p2_verify('news_market_tracks', $q$SELECT * FROM news_market_tracks$q$, $q$SELECT subject_id AS
        group_key,(detail->>'market_kind')::text AS market_kind,(detail->>'family')::text AS family,
        (detail->>'provider')::text AS provider,(detail->>'source_venue')::text AS source_venue,
        (detail->>'venue_known')::boolean AS venue_known,(detail->>'raw_instrument')::text AS raw_instrument,
        (detail->>'symbol')::text AS symbol,(detail->>'measurement_definition')::text AS measurement_definition,
        (detail->>'liquidated_position_side')::text AS liquidated_position_side,(detail->>'account_key')::text AS
        account_key,(detail->>'account_verified')::boolean AS account_verified,(detail->>'trader_label')::text AS
        trader_label,(detail->>'last_observed_at_ms')::bigint AS last_observed_at_ms,
        (detail->>'last_observed_item_id')::text AS last_observed_item_id,(detail->>'anchor_state')::text AS
        anchor_state,(detail->>'anchor_delivery_key')::text AS anchor_delivery_key,
        (detail->>'anchor_attempt_at_ms')::bigint AS anchor_attempt_at_ms,(detail->>'anchor_oi_change_bps')::bigint AS
        anchor_oi_change_bps,(detail->>'anchor_direction')::text AS anchor_direction,(detail->>'anchor_action')::text AS
        anchor_action,(detail->>'anchor_position_side')::text AS anchor_position_side,
        (detail->>'open_delivery_key')::text AS open_delivery_key,next_attempt_at_ms AS next_due_at_ms,
        (detail->>'pending_reason')::text AS pending_reason,created_at_ms AS created_at_ms,updated_at_ms AS
        updated_at_ms,(detail->>'round_started_at_ms')::bigint AS round_started_at_ms FROM news_jobs WHERE
        job_kind='market_notify'$q$);
ALTER TABLE news_market_observations DROP CONSTRAINT news_market_observations_notification_id_fkey;
ALTER TABLE news_market_observations ADD CONSTRAINT news_market_observations_notification_fkey
 FOREIGN KEY(notification_id) REFERENCES news_notifications(notification_id) ON DELETE SET NULL;
DO $$ BEGIN RAISE NOTICE 'p2_verify ok'; END $$;
DROP VIEW news_notification_review_tasks_v1;
DROP TABLE news_notification_feedback,news_notification_external_feedback,news_external_miss_snapshots;
DROP TABLE news_notification_work,news_delivery_queue,news_deliveries,news_notification_decisions,
        news_market_tracks,news_market_deliveries;
DROP FUNCTION news_notification_record_update_guard(),news_notification_feedback_source_guard(),
        reject_news_review_mutation();
CREATE FUNCTION news_notifications_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.notification_id, NEW.kind, NEW.origin, NEW.event_id, NEW.group_key, NEW.market_kind,
        NEW.trigger_reason,
    NEW.trigger_observation_id, NEW.update_ref, NEW.input_digest, NEW.input_snapshot, NEW.plan,
        NEW.decided_at_ms)
    IS DISTINCT FROM (OLD.notification_id, OLD.kind, OLD.origin, OLD.event_id, OLD.group_key, OLD.market_kind,
        OLD.trigger_reason,
    OLD.trigger_observation_id, OLD.update_ref, OLD.input_digest, OLD.input_snapshot, OLD.plan,
        OLD.decided_at_ms)
  THEN RAISE check_violation USING MESSAGE = 'news_notification_decision_immutable'; END IF;
  IF OLD.state IN ('sent','ambiguous','terminal','unknown')
    AND (NEW.state, NEW.intent_id, NEW.card, NEW.claim_refs, NEW.content_revision, NEW.settled_at_ms)
    IS DISTINCT FROM (OLD.state, OLD.intent_id, OLD.card, OLD.claim_refs, OLD.content_revision,
        OLD.settled_at_ms)
  THEN RAISE check_violation USING MESSAGE = 'news_notification_settled_send_immutable'; END IF;
  IF NEW.edit_state IS DISTINCT FROM OLD.edit_state AND NEW.state <> 'sent'
  THEN RAISE check_violation USING MESSAGE = 'news_notification_edit_requires_sent'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER news_notifications_guard BEFORE UPDATE ON news_notifications FOR EACH ROW EXECUTE FUNCTION
        news_notifications_guard();

""")


def downgrade() -> None:
    raise RuntimeError("p2_forward_only_restore_verified_backup")
