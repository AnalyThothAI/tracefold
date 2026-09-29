"""News reader decisions: persisted claim links and `reader_v2` notification decisions (#742 PR-2).

`news_claim_links` keeps every semantic link an adopted revision asserted between a claim and an earlier
claim (`changes` entries with a `previous_ref`). A revision's `changes` describe only that revision, so a head
alone loses links an earlier revision made; the notification decision layer reads this table in both
directions to know what the reader already holds. Rows are insert-only, one per asserting update and claim
pair; the code resolves a pair asserted more than once to its latest assertion. The table is backfilled from
every stored `news_event_updates` document.

Notification decisions gain the `reader_v2` origin: per-claim reader novelty, the anchor and incremental
importance evidence of the reader judgment, and the rule that decided. `editorial_v1` rows stay readable as
history. The review queue and the feedback guard accept both origins. Plans are no longer reused whole (reader
judgments are reused per claim from the judgment cache), so the plan-reuse index is dropped.

Forward only. Drain in-flight `editorial_v1` intents before switching images (the new code does not execute an
editorial plan); restore the verified backup with its matching image for rollback.
"""

from alembic import op

revision = "20260929_0416"
down_revision = "20260929_0415"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    op.execute("""
        CREATE TABLE public.news_claim_links (
          update_ref text NOT NULL,
          current_ref text NOT NULL,
          previous_ref text NOT NULL,
          relation text NOT NULL CHECK (
            relation IN ('equivalent','adds_information','real_world_change','corrects','conflicts')),
          current_event_id text NOT NULL,
          previous_event_id text,
          asserted_at_ms bigint NOT NULL CHECK (asserted_at_ms >= 0),
          PRIMARY KEY (update_ref, current_ref, previous_ref),
          CHECK (current_ref <> previous_ref)
        );
        CREATE INDEX news_claim_links_current ON public.news_claim_links(current_ref);
        CREATE INDEX news_claim_links_previous ON public.news_claim_links(previous_ref);
        WITH updates AS MATERIALIZED (
          SELECT u.event_id, u.adopted_at_ms, u.document,
                 public.news_identity('update', jsonb_build_array(u.event_id, u.content_revision)) AS ref
            FROM public.news_event_updates u
        )
        INSERT INTO public.news_claim_links
          (update_ref,current_ref,previous_ref,relation,current_event_id,previous_event_id,asserted_at_ms)
        SELECT DISTINCT ON (u.ref, c.value->>'current_ref', c.value->>'previous_ref')
               u.ref, c.value->>'current_ref', c.value->>'previous_ref', c.value->>'relation', u.event_id,
               previous.event_id, u.adopted_at_ms
          FROM updates u
          CROSS JOIN LATERAL jsonb_array_elements(u.document->'changes') c(value)
          LEFT JOIN updates previous ON previous.ref = c.value->>'previous_content_ref'
         WHERE c.value->>'previous_ref' IS NOT NULL
           AND c.value->>'previous_ref' <> c.value->>'current_ref'
           AND c.value->>'relation' IN ('equivalent','adds_information','real_world_change','corrects','conflicts')
         ORDER BY u.ref, c.value->>'current_ref', c.value->>'previous_ref';

        ALTER TABLE public.news_notification_decisions DROP CONSTRAINT news_notification_decisions_origin_check;
        ALTER TABLE public.news_notification_decisions ADD CONSTRAINT news_notification_decisions_origin_check
          CHECK (origin IN ('editorial_v1','legacy_work_plan','reader_v2'));
        ALTER TABLE public.news_notification_decisions DROP CONSTRAINT news_notification_decisions_contract_check;
        ALTER TABLE public.news_notification_decisions ADD CONSTRAINT news_notification_decisions_contract_check
          CHECK (
            origin='legacy_work_plan' OR (
              input_digest IS NOT NULL
              AND plan->>'update_ref'=update_ref
              AND plan->>'channel'=channel
              AND plan->>(CASE origin WHEN 'reader_v2' THEN 'input_digest' ELSE 'assessment_input_digest' END)
                  =input_digest
              AND jsonb_typeof(plan->'claim_decisions')='array'
            )
          );
        DROP INDEX public.news_notification_decisions_editorial_input;
        DROP INDEX public.news_notification_decisions_review_queue;
        CREATE INDEX news_notification_decisions_review_queue
          ON public.news_notification_decisions(created_at_ms DESC,decision_ref DESC)
          WHERE origin IN ('editorial_v1','reader_v2');
        CREATE OR REPLACE FUNCTION public.news_notification_feedback_source_guard() RETURNS trigger
          LANGUAGE plpgsql AS $$
          BEGIN
            IF NOT EXISTS (
              SELECT 1 FROM public.news_notification_decisions d
               WHERE d.decision_ref=NEW.decision_ref AND d.origin IN ('editorial_v1','reader_v2')
                 AND d.plan->'claim_decisions' @> jsonb_build_array(jsonb_build_object('claim_ref',NEW.claim_ref))
            ) THEN
              RAISE EXCEPTION USING ERRCODE='23514',
                CONSTRAINT='news_notification_feedback_source_check',
                MESSAGE='news_notification_feedback_source_missing';
            END IF;
            RETURN NEW;
          END;
          $$;
    """)


def downgrade() -> None:
    raise RuntimeError("news_reader_decisions_forward_only: restore verified backup and matching image")
