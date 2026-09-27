"""Immutable News notification decisions and references from work and intents.

Forward only. Existing work plans are preserved as explicitly legacy decisions; no historical
attention assessment or lost earlier plan is invented. Old unsent intents keep their original bytes.
Stop News/Review writers for the normal schema/image switch. Transactional DDL; restore the verified
backup with its matching image for rollback. No source, receipt or Trading row is deleted.
"""

from alembic import op

revision = "20260927_0407"
down_revision = "20260927_0406"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    op.execute("""
        CREATE TABLE public.news_notification_decisions (
          decision_ref text PRIMARY KEY,
          event_id text NOT NULL REFERENCES public.news_events(event_id) ON DELETE CASCADE,
          update_ref text NOT NULL,
          channel text NOT NULL CHECK (channel = 'news'),
          input_digest text,
          input_snapshot jsonb NOT NULL CHECK (jsonb_typeof(input_snapshot) = 'object'),
          plan jsonb NOT NULL CHECK (jsonb_typeof(plan) = 'object'),
          origin text NOT NULL CHECK (origin IN ('editorial_v1','legacy_work_plan')),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          CONSTRAINT news_notification_decisions_contract_check CHECK (
            origin='legacy_work_plan' OR (
              input_digest IS NOT NULL
              AND plan->>'update_ref'=update_ref
              AND plan->>'channel'=channel
              AND plan->>'assessment_input_digest'=input_digest
              AND jsonb_typeof(plan->'claim_decisions')='array'
            )
          )
        );
        CREATE UNIQUE INDEX news_notification_decisions_input
          ON public.news_notification_decisions(event_id,channel,input_digest) WHERE input_digest IS NOT NULL;
        CREATE INDEX news_notification_decisions_event
          ON public.news_notification_decisions(event_id,created_at_ms DESC);
        CREATE INDEX news_notification_decisions_review_queue
          ON public.news_notification_decisions(created_at_ms DESC,decision_ref DESC)
          WHERE origin='editorial_v1';
        ALTER TABLE public.news_notification_work ADD COLUMN decision_ref text
          REFERENCES public.news_notification_decisions(decision_ref);
        ALTER TABLE public.news_delivery_queue ADD COLUMN decision_ref text
          REFERENCES public.news_notification_decisions(decision_ref);
        ALTER TABLE public.news_deliveries ADD COLUMN decision_ref text
          REFERENCES public.news_notification_decisions(decision_ref);
        CREATE UNIQUE INDEX news_deliveries_decision_ref
          ON public.news_deliveries(decision_ref) WHERE decision_ref IS NOT NULL;
        UPDATE public.news_delivery_queue
           SET state='dead',error_code='legacy_intent_retired',
               settled_at_ms=floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint,
               updated_at_ms=floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint
         WHERE state='pending' AND kind IN ('first','followup');
        INSERT INTO public.news_notification_decisions
          (decision_ref,event_id,update_ref,channel,input_snapshot,plan,origin,created_at_ms)
        SELECT public.news_identity('notification_decision_legacy',
                 jsonb_build_array(event_id,channel,content_revision,updated_at_ms)),
               event_id,plan->>'update_ref',channel,
               jsonb_build_object('legacy_work_plan',true,'content_revision',content_revision),
               plan,'legacy_work_plan',updated_at_ms
          FROM public.news_notification_work WHERE plan IS NOT NULL;
        UPDATE public.news_notification_work w SET decision_ref=d.decision_ref
          FROM public.news_notification_decisions d
         WHERE d.event_id=w.event_id AND d.channel=w.channel AND d.origin='legacy_work_plan';
        ALTER TABLE public.news_notification_work DROP CONSTRAINT news_notification_work_plan_check;
        ALTER TABLE public.news_notification_work ADD CONSTRAINT news_notification_work_plan_check CHECK (
          content_revision ~ '^[0-9a-f]{64}$'
          AND (state='pending' OR decision_ref IS NOT NULL OR plan IS NOT NULL)
          AND (plan IS NULL OR (jsonb_typeof(plan)='object'
                AND plan->>'reader_revision'=reader_revision AND plan->>'channel'=channel)));
        UPDATE public.news_notification_work SET plan=NULL WHERE decision_ref IS NOT NULL;
        CREATE TABLE public.news_notification_feedback (
          review_id text PRIMARY KEY,
          decision_ref text NOT NULL REFERENCES public.news_notification_decisions(decision_ref),
          claim_ref text NOT NULL,
          task_version text NOT NULL CHECK (task_version ~ '^[0-9a-f]{64}$'),
          reviewer text NOT NULL CHECK (length(reviewer) BETWEEN 1 AND 64),
          idempotency_key text NOT NULL,
          request_sha text NOT NULL CHECK (request_sha ~ '^[0-9a-f]{64}$'),
          should_push text NOT NULL CHECK (should_push IN ('should_push','should_hold','uncertain')),
          note text NOT NULL DEFAULT '',
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          UNIQUE(reviewer,idempotency_key)
        );
        CREATE INDEX news_notification_feedback_task
          ON public.news_notification_feedback(decision_ref,claim_ref,created_at_ms DESC);
        CREATE FUNCTION public.news_notification_record_update_guard() RETURNS trigger
          LANGUAGE plpgsql AS $$
          BEGIN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='news_notification_record_append_only';
          END;
          $$;
        CREATE TRIGGER news_notification_decisions_append_only
          BEFORE UPDATE ON public.news_notification_decisions
          FOR EACH ROW EXECUTE FUNCTION public.news_notification_record_update_guard();
        CREATE TRIGGER news_notification_feedback_append_only
          BEFORE UPDATE ON public.news_notification_feedback
          FOR EACH ROW EXECUTE FUNCTION public.news_notification_record_update_guard();
        CREATE FUNCTION public.news_notification_feedback_source_guard() RETURNS trigger
          LANGUAGE plpgsql AS $$
          BEGIN
            IF NOT EXISTS (
              SELECT 1 FROM public.news_notification_decisions d
               WHERE d.decision_ref=NEW.decision_ref AND d.origin='editorial_v1'
                 AND d.plan->'claim_decisions' @> jsonb_build_array(jsonb_build_object('claim_ref',NEW.claim_ref))
            ) THEN
              RAISE EXCEPTION USING ERRCODE='23514',
                CONSTRAINT='news_notification_feedback_source_check',
                MESSAGE='news_notification_feedback_source_missing';
            END IF;
            RETURN NEW;
          END;
          $$;
        CREATE TRIGGER news_notification_feedback_source_check
          BEFORE INSERT ON public.news_notification_feedback
          FOR EACH ROW EXECUTE FUNCTION public.news_notification_feedback_source_guard();
        CREATE TABLE public.news_notification_external_feedback (
          review_id text PRIMARY KEY,
          snapshot_id text NOT NULL REFERENCES public.news_external_miss_snapshots(snapshot_id),
          reviewer text NOT NULL CHECK (length(reviewer) BETWEEN 1 AND 64),
          idempotency_key text NOT NULL,
          request_sha text NOT NULL CHECK (request_sha ~ '^[0-9a-f]{64}$'),
          should_push text NOT NULL CHECK (should_push IN ('should_push','should_hold','uncertain')),
          note text NOT NULL DEFAULT '',
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          UNIQUE(reviewer,idempotency_key)
        );
        CREATE TRIGGER news_notification_external_feedback_append_only
          BEFORE UPDATE ON public.news_notification_external_feedback
          FOR EACH ROW EXECUTE FUNCTION public.news_notification_record_update_guard();
        CREATE VIEW public.news_notification_review_tasks_v1 WITH (security_barrier = true) AS
        SELECT d.decision_ref,d.event_id,d.update_ref,d.channel,d.input_snapshot,d.plan,
               d.origin,d.created_at_ms,claimed.value AS claim_decision,
               claimed.value->>'claim_ref' AS claim_ref,claim.value AS claim,
               u.document AS update_document,q.intent_id,q.state AS queue_state,
               delivery.state AS delivery_state,delivery.body AS delivery_body,
               delivery.payload_sha256 AS delivery_sha256,delivery.settled_at_ms,
               delivery.error_code AS delivery_error_code
          FROM public.news_notification_decisions d
          JOIN public.news_event_updates u ON u.event_id=d.event_id
           AND d.update_ref=public.news_identity('update',jsonb_build_array(u.event_id,u.content_revision))
          CROSS JOIN LATERAL jsonb_array_elements(d.plan->'claim_decisions') claimed(value)
          JOIN LATERAL jsonb_array_elements(u.document->'claims') claim(value)
            ON claim.value->>'ref'=claimed.value->>'claim_ref'
          LEFT JOIN public.news_delivery_queue q ON q.decision_ref=d.decision_ref
          LEFT JOIN public.news_deliveries delivery ON delivery.decision_ref=d.decision_ref;
    """)


def downgrade() -> None:
    raise RuntimeError("news_notification_decisions_forward_only: restore verified backup and matching image")
