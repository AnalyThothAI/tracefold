"""Preserve Trading facts while completing admission and evaluation contracts (#760).

Migration evidence:
- category: stopped-writer, forward-only preservation of existing Trading facts.
- why_database_must_change: admission needs venue margin evidence; evaluation
  identity must distinguish model contracts and isolated runs.
- current/minimum_source_revision: 20260929_0418.
- lock_level_and_order: stop Analysis/Executor writers, take a verified backup;
  ACCESS EXCLUSIVE on altered Trading tables in declaration order.
- statement_timeout: 60s; lock_timeout: 5s.
- estimated_rows/bytes: small current Trading ledger, no facts discarded.
- preflight: legacy inputs remain explicit; active Plans need no fabricated facts.
- archive_current_compatibility: all Case, Signal, Order and Fill facts retained.
- role_and_grant_impact: no new role, existing application migration grants.
- failure_state: transactional rollback preserves the previous schema.
- roll_forward_or_verified_backup_restore: forward repair or verified backup restore.
"""

from alembic import op

revision = "20260930_0419"
down_revision = "20260929_0418"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("""
        ALTER TABLE public.trading_plans
          ADD COLUMN admitted_at_ns bigint,
          ADD COLUMN admission_snapshot jsonb,
          ADD COLUMN reserved_margin numeric CHECK (reserved_margin >= 0)
    """)
    op.execute("ALTER TABLE public.trading_dispositions ADD COLUMN admission_snapshot jsonb")
    op.execute("ALTER TABLE public.trading_trade_cursors ADD COLUMN bootstrap_since_ns bigint")
    op.execute("ALTER TABLE public.trading_executor_state ADD COLUMN faults jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE public.trading_executor_state ADD COLUMN last_error_at_ns bigint")

    op.execute("""
        ALTER TABLE public.trading_cases
          ADD COLUMN intake_context jsonb CHECK (jsonb_typeof(intake_context)='object'),
          ADD COLUMN units_per_contract numeric CHECK (units_per_contract > 0),
          ADD COLUMN episode_id text,
          ADD COLUMN episode_role text CHECK (episode_role IN ('leader','repeat','material'))
    """)
    op.execute("CREATE INDEX ix_trading_cases_episode ON public.trading_cases(episode_id)")
    op.execute("""
        ALTER TABLE public.trading_policy_actions DROP CONSTRAINT trading_policy_actions_publish_status_check,
          ADD CHECK (publish_status IN ('not_live','published','publish_disabled','runtime_unavailable',
            'execution_venue_unlisted','source_corrected','source_superseded','abstained','signal_expired',
            'episode_repeated','episode_unknown','source_stale','input_incomplete'))
    """)
    op.execute("""
        CREATE TABLE public.trading_evaluation_runs (
          run_id text PRIMARY KEY CHECK (run_id ~ '^[0-9a-f]{64}$'),
          evaluator_id text NOT NULL CHECK (evaluator_id ~ '^[0-9a-f]{64}$'),
          kind text NOT NULL CHECK (kind IN ('online','inference','policies','legacy')),
          evaluator_spec jsonb NOT NULL,
          manifest jsonb NOT NULL,
          created_at_ms bigint NOT NULL,
          UNIQUE (run_id,evaluator_id)
        )
    """)
    op.execute("""
        INSERT INTO public.trading_evaluation_runs
          (run_id,evaluator_id,kind,evaluator_spec,manifest,created_at_ms)
        SELECT encode(sha256(convert_to('legacy_run|'||program_sha||'|'||route,'UTF8')),'hex'),
          encode(sha256(convert_to('legacy_evaluator|'||program_sha||'|'||route,'UTF8')),'hex'),
          'legacy',jsonb_build_object('program_sha',program_sha,'model_name',route,
            'model_revision',NULL,'input_contract','case_view_v1','output_contract','forecast_v1',
            'generation_parameters','unknown'),
          '{"mode":"legacy","sampling":"unknown","cohort":"unknown"}'::jsonb,min(started_at_ms)
        FROM public.trading_assessments GROUP BY program_sha,route
    """)
    op.execute("""
        ALTER TABLE public.trading_assessments
          ADD COLUMN assessment_id text,
          ADD COLUMN run_id text,
          ADD COLUMN evaluator_id text,
          ADD COLUMN reused_assessment_id text,
          ADD COLUMN error_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    """)
    op.execute("""
        UPDATE public.trading_assessments a SET
          assessment_id=encode(sha256(convert_to('legacy_assessment|'||case_id||'|'||program_sha,'UTF8')),'hex'),
          run_id=encode(sha256(convert_to('legacy_run|'||program_sha||'|'||route,'UTF8')),'hex'),
          evaluator_id=encode(sha256(convert_to('legacy_evaluator|'||program_sha||'|'||route,'UTF8')),'hex')
    """)
    op.execute("""
        ALTER TABLE public.trading_assessments DROP CONSTRAINT trading_assessments_pkey,
          ALTER COLUMN assessment_id SET NOT NULL,
          ALTER COLUMN run_id SET NOT NULL,
          ALTER COLUMN evaluator_id SET NOT NULL,
          ADD PRIMARY KEY (assessment_id),
          ADD UNIQUE (case_id,run_id),
          ADD UNIQUE (assessment_id,case_id,program_sha),
          ADD FOREIGN KEY (run_id,evaluator_id)
            REFERENCES public.trading_evaluation_runs(run_id,evaluator_id),
          ADD FOREIGN KEY (reused_assessment_id) REFERENCES public.trading_assessments(assessment_id)
    """)
    op.execute("""
        ALTER TABLE public.trading_policy_actions
          ADD COLUMN action_id text,
          ADD COLUMN assessment_id text,
          ADD COLUMN policy_config jsonb NOT NULL DEFAULT '{"contract":"legacy","parameters":"unknown"}'::jsonb
    """)
    op.execute("""
        UPDATE public.trading_policy_actions a SET
          assessment_id=b.assessment_id,
          action_id=encode(sha256(convert_to('legacy_action|'||a.case_id||'|'||a.program_sha||'|'||
              a.policy_id||'|'||a.policy_version,'UTF8')),'hex')
        FROM public.trading_assessments b
        WHERE b.case_id=a.case_id AND b.program_sha=a.program_sha
    """)
    # Published historical actions retain the immutable Signal's decision identity.
    op.execute("""
        UPDATE public.trading_policy_actions a SET action_id=s.decision_id
        FROM public.trading_signals s WHERE a.signal_id=s.signal_id
    """)
    op.execute("""
        ALTER TABLE public.trading_policy_actions DROP CONSTRAINT trading_policy_actions_pkey,
          ALTER COLUMN action_id SET NOT NULL,
          ALTER COLUMN assessment_id SET NOT NULL,
          ADD PRIMARY KEY (action_id),
          ADD UNIQUE (assessment_id,policy_id,policy_version),
          ADD FOREIGN KEY (assessment_id,case_id,program_sha)
            REFERENCES public.trading_assessments(assessment_id,case_id,program_sha)
    """)
    # NOT VALID preserves imported Signals whose legacy evaluator cannot be recovered;
    # every new Signal is nevertheless constrained to a durable policy action.
    op.execute("""
        ALTER TABLE public.trading_signals ADD CONSTRAINT trading_signals_action_fkey
          FOREIGN KEY (decision_id) REFERENCES public.trading_policy_actions(action_id) NOT VALID
    """)
    op.execute("""
        CREATE TRIGGER trg_trading_assessments_append_only
          BEFORE UPDATE OR DELETE ON public.trading_assessments
          FOR EACH ROW EXECUTE FUNCTION public.reject_trading_executor_append_mutation()
    """)
    op.execute("""
        CREATE TRIGGER trg_trading_evaluation_runs_append_only
          BEFORE UPDATE OR DELETE ON public.trading_evaluation_runs
          FOR EACH ROW EXECUTE FUNCTION public.reject_trading_executor_append_mutation()
    """)

    op.execute("""
        CREATE FUNCTION public.guard_trading_action_facts() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'trading policy action is durable'; END IF;
          IF (to_jsonb(NEW)-'publish_status'-'signal_id') <>
             (to_jsonb(OLD)-'publish_status'-'signal_id') OR
             OLD.publish_status NOT IN ('not_live','abstained') THEN
            RAISE EXCEPTION 'trading policy action is immutable';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_trading_policy_actions_facts BEFORE UPDATE OR DELETE
          ON public.trading_policy_actions FOR EACH ROW EXECUTE FUNCTION public.guard_trading_action_facts()
    """)


def downgrade() -> None:
    raise RuntimeError("trading_rootfix_forward_only_restore_verified_backup")
