"""Index every claimed analysis attempt, including failures and fenced late work.

Revision ID: 20260924_0393
Revises: 20260923_0392

Additive, forward-only ledger. No existing Case or Decision is rewritten. The
large frozen inputs and physical responses remain in the content-addressed
archive; these rows make them discoverable even when no Decision exists.
"""

from __future__ import annotations

from alembic import op

revision = "20260924_0393"
down_revision = "20260923_0392"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("ALTER TABLE public.trading_cases DROP CONSTRAINT trading_cases_run_kind_check")
    op.execute(
        "ALTER TABLE public.trading_cases ADD CONSTRAINT trading_cases_run_kind_check "
        "CHECK (run_kind IN ('initial','recheck','conditional'))"
    )
    op.execute("""
        CREATE TABLE public.trading_case_attempts (
            case_id text NOT NULL REFERENCES public.trading_cases(case_id) ON DELETE RESTRICT,
            claim_attempt integer NOT NULL CHECK (claim_attempt > 0),
            claim_token text NOT NULL,
            brief_ref text,
            evidence_ref text,
            assessment_ref text,
            model_name text,
            prompt_sha text,
            started_at_ms bigint,
            ended_at_ms bigint,
            provider_status text,
            analysis_status text NOT NULL,
            error_code text,
            validation_errors jsonb NOT NULL DEFAULT '[]'::jsonb,
            physical_call_count integer NOT NULL DEFAULT 0 CHECK (physical_call_count >= 0),
            input_tokens bigint,
            output_tokens bigint,
            cost_microusd bigint CHECK (cost_microusd >= 0),
            known_cost_microusd bigint NOT NULL DEFAULT 0 CHECK (known_cost_microusd >= 0),
            unknown_cost_calls integer NOT NULL DEFAULT 0 CHECK (unknown_cost_calls >= 0),
            cost_upper_estimate_microusd bigint CHECK (cost_upper_estimate_microusd >= 0),
            cost_unknown_reason text,
            settled boolean NOT NULL DEFAULT false,
            PRIMARY KEY (case_id, claim_attempt),
            CHECK (cost_microusd IS NULL OR cost_unknown_reason IS NULL)
        )
    """)
    op.execute("""
        CREATE TABLE public.trading_model_calls (
            case_id text NOT NULL,
            claim_attempt integer NOT NULL,
            call_index integer NOT NULL CHECK (call_index >= 0),
            status text NOT NULL DEFAULT 'requested' CHECK (status IN ('requested','completed','result_unknown')),
            started_at_ms bigint,
            finished_at_ms bigint,
            timeout_ms integer,
            remaining_deadline_ms integer,
            reserved_cost_microusd bigint,
            request_ref text,
            response_ref text,
            input_tokens bigint,
            output_tokens bigint,
            cost_microusd bigint CHECK (cost_microusd >= 0),
            cost_unknown_reason text,
            PRIMARY KEY (case_id, claim_attempt, call_index),
            FOREIGN KEY (case_id, claim_attempt)
                REFERENCES public.trading_case_attempts(case_id, claim_attempt) ON DELETE RESTRICT,
            CHECK (cost_microusd IS NULL OR cost_unknown_reason IS NULL)
        )
    """)
    op.execute("""
        CREATE TABLE public.trading_watch_observations (
            parent_case_id text PRIMARY KEY REFERENCES public.trading_cases(case_id) ON DELETE RESTRICT,
            condition jsonb NOT NULL,
            status text NOT NULL CHECK (status IN ('waiting','triggered','expired','cancelled')),
            last_observation_status text CHECK (
                last_observation_status IN ('not_met','data_missing','satisfied','missed')
            ),
            trigger_id text NOT NULL UNIQUE REFERENCES public.trading_triggers(trigger_id) ON DELETE RESTRICT,
            last_observed_at_ms bigint,
            last_observed_value numeric,
            trigger_side text CHECK (trigger_side IN ('long','short')),
            last_observation_ref text,
            next_check_at_ms bigint NOT NULL,
            expires_at_ms bigint NOT NULL,
            child_case_id text UNIQUE REFERENCES public.trading_cases(case_id) ON DELETE RESTRICT,
            created_at_ms bigint NOT NULL,
            updated_at_ms bigint NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX ix_trading_watch_due ON public.trading_watch_observations
          (next_check_at_ms,parent_case_id) WHERE status='waiting'
    """)
    op.execute("""
        CREATE TABLE public.trading_case_evaluations (
            case_id text NOT NULL REFERENCES public.trading_cases(case_id) ON DELETE RESTRICT,
            source text NOT NULL CHECK (source IN ('shadow_simulation','paper_venue')),
            evaluation_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('pending','simulated','paper_venue_net','unevaluable')),
            reason text,
            decision_at_ms bigint NOT NULL,
            scheduled_at_ms bigint NOT NULL,
            due_at_ms bigint NOT NULL,
            next_attempt_at_ms bigint NOT NULL,
            decision_quote_ref text,
            planned_quote_ref text,
            mark_path_ref text,
            funding_ref text,
            venue_receipt_ref text,
            result jsonb,
            evaluated_at_ms bigint,
            PRIMARY KEY (case_id,source,evaluation_version)
        )
    """)
    op.execute("""
        CREATE INDEX ix_trading_case_evaluations_due ON public.trading_case_evaluations
          (next_attempt_at_ms,case_id) WHERE status='pending'
    """)


def downgrade() -> None:
    raise RuntimeError("trading_analysis_attempts_forward_only")
