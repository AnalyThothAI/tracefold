"""Replace DEMO-era Analysis facts with frozen LIVE Cases (#746 PR-2).

Stop Analysis, verify the #746 Trading backup and 0 DEMO exposure before
deployment. This is an irreversible, transactional hard cut: no old decisions
or snapshots are copied into the new ledger. The execution schema from 0417
remains intact. The first deployment must have no unpublished 0417 signals.

Revision ID: 20260929_0418
Revises: 20260929_0417
"""

from alembic import op

revision = "20260929_0418"
down_revision = "20260929_0417"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM public.trading_signals LIMIT 1) THEN
            RAISE EXCEPTION 'trading_analysis_hard_cut_requires_empty_signals';
          END IF;
        END $$
    """)
    op.execute("ALTER TABLE public.trading_signals DROP CONSTRAINT trading_signals_case_id_fkey")
    for table in (
        "trading_model_calls",
        "trading_watch_observations",
        "trading_candidate_gate_decisions",
        "trading_case_attempts",
        "trading_case_decisions",
        "trading_case_outcomes",
        "trading_cases",
        "trading_trigger_conflicts",
        "trading_triggers",
        "trading_analysis_runtime",
    ):
        op.execute(f"DROP TABLE public.{table}")

    op.execute("""
        CREATE TABLE public.trading_triggers (
          trigger_id text PRIMARY KEY CHECK (trigger_id ~ '^[0-9a-f]{64}$'),
          kind text NOT NULL CHECK (kind IN ('oi','catalyst')),
          source_fact_key text NOT NULL,
          source_revision text NOT NULL,
          payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
          payload jsonb NOT NULL,
          first_visible_at_ms bigint NOT NULL,
          source_observed_at_ms bigint NOT NULL,
          selected_asset_id text,
          target_selection jsonb NOT NULL,
          exclusion_reason text,
          created_at_ms bigint NOT NULL,
          UNIQUE (kind,source_fact_key,source_revision),
          CHECK ((selected_asset_id IS NULL) = (exclusion_reason IS NOT NULL))
        )
    """)
    op.execute("CREATE INDEX ix_trading_triggers_created ON public.trading_triggers (created_at_ms DESC)")
    op.execute(
        "CREATE INDEX ix_trading_triggers_evidence_ref ON public.trading_triggers "
        "((payload->>'evidence_ref')) WHERE kind='oi'"
    )
    op.execute(
        "CREATE INDEX ix_trading_triggers_source ON public.trading_triggers (source_fact_key,created_at_ms DESC)"
    )
    op.execute("""
        CREATE TABLE public.trading_trigger_conflicts (
          kind text NOT NULL,
          source_fact_key text NOT NULL,
          source_revision text NOT NULL,
          attempted_sha256 text NOT NULL,
          original_sha256 text NOT NULL,
          observed_at_ms bigint NOT NULL,
          PRIMARY KEY (kind,source_fact_key,source_revision,attempted_sha256)
        )
    """)
    op.execute("""
        CREATE TABLE public.trading_cases (
          case_id text PRIMARY KEY CHECK (case_id ~ '^[0-9a-f]{64}$'),
          trigger_id text NOT NULL UNIQUE REFERENCES public.trading_triggers(trigger_id),
          trigger_kind text NOT NULL CHECK (trigger_kind IN ('oi','catalyst')),
          asset_id text NOT NULL,
          native_symbol text NOT NULL,
          mapping_digest text NOT NULL,
          created_at_ms bigint NOT NULL,
          root_expires_at_ms bigint NOT NULL,
          state text NOT NULL CHECK (state IN ('pending','running','complete','failed')),
          claim_token text,
          lease_until_ms bigint,
          claim_attempt integer NOT NULL DEFAULT 0 CHECK (claim_attempt >= 0),
          view jsonb,
          view_sha256 text CHECK (view_sha256 ~ '^[0-9a-f]{64}$'),
          raw_snapshot_ref text,
          geometry_version text,
          stop_bps integer CHECK (stop_bps > 0),
          tp_bps integer CHECK (tp_bps > 0),
          half_spread_bps numeric CHECK (half_spread_bps >= 0),
          reference_price numeric CHECK (reference_price > 0),
          decided_at_ms bigint,
          failure_code text,
          updated_at_ms bigint NOT NULL,
          CHECK (root_expires_at_ms > created_at_ms),
          CHECK ((view IS NULL) = (view_sha256 IS NULL)),
          CHECK ((stop_bps IS NULL) = (tp_bps IS NULL))
        )
    """)
    op.execute("CREATE INDEX ix_trading_cases_claim ON public.trading_cases (state,lease_until_ms,created_at_ms)")
    op.execute("CREATE INDEX ix_trading_cases_score ON public.trading_cases (created_at_ms,asset_id)")
    op.execute(
        "CREATE UNIQUE INDEX ux_trading_cases_running_asset ON public.trading_cases (asset_id) WHERE state='running'"
    )
    op.execute("""
        CREATE TABLE public.trading_assessments (
          case_id text NOT NULL REFERENCES public.trading_cases(case_id),
          program_sha text NOT NULL CHECK (program_sha ~ '^[0-9a-f]{64}$'),
          route text NOT NULL,
          status text NOT NULL CHECK (status IN
            ('ok','parse','truncated','schema','provider','timeout','rate_limit','data_missing')),
          forecast jsonb,
          drivers jsonb NOT NULL DEFAULT '[]'::jsonb,
          notes jsonb NOT NULL DEFAULT '[]'::jsonb,
          input_tokens integer,
          output_tokens integer,
          started_at_ms bigint NOT NULL,
          ended_at_ms bigint NOT NULL,
          PRIMARY KEY (case_id,program_sha),
          CHECK ((status='ok') = (forecast IS NOT NULL)),
          CHECK (ended_at_ms >= started_at_ms)
        )
    """)
    op.execute("CREATE INDEX ix_trading_assessments_route ON public.trading_assessments (program_sha,route,status)")
    op.execute("""
        CREATE TABLE public.trading_policy_actions (
          case_id text NOT NULL REFERENCES public.trading_cases(case_id),
          program_sha text NOT NULL CHECK (program_sha ~ '^[0-9a-f]{64}$'),
          policy_id text NOT NULL,
          policy_version text NOT NULL,
          calibrator_version text NOT NULL,
          action text NOT NULL CHECK (action IN ('long','short','abstain')),
          reason text NOT NULL,
          expected_r numeric,
          publish_status text NOT NULL DEFAULT 'not_live' CHECK (publish_status IN
            ('not_live','published','publish_disabled','runtime_unavailable',
             'execution_venue_unlisted','source_corrected','source_superseded',
             'abstained','signal_expired')),
          signal_id text REFERENCES public.trading_signals(signal_id),
          decided_at_ms bigint NOT NULL,
          PRIMARY KEY (case_id,program_sha,policy_id,policy_version),
          CHECK ((publish_status='published') = (signal_id IS NOT NULL))
        )
    """)
    op.execute(
        "CREATE INDEX ix_trading_policy_actions_score ON public.trading_policy_actions "
        "(program_sha,policy_id,policy_version,decided_at_ms)"
    )
    op.execute("""
        CREATE TABLE public.trading_paper_legs (
          case_id text NOT NULL REFERENCES public.trading_cases(case_id),
          side text NOT NULL CHECK (side IN ('long','short')),
          geometry_version text NOT NULL,
          status text NOT NULL CHECK (status IN ('complete','missing')),
          outcome text CHECK (outcome IN ('tp','sl','timeout')),
          reason text,
          anchor_at_ms bigint,
          exit_at_ms bigint,
          anchor_price numeric,
          exit_price numeric,
          gross_bps numeric,
          cost_bps numeric,
          net_r numeric,
          labeled_at_ms bigint NOT NULL,
          PRIMARY KEY (case_id,side,geometry_version),
          CHECK ((status='complete') = (outcome IS NOT NULL)),
          CHECK ((status='complete') = (net_r IS NOT NULL)),
          CHECK ((status='missing') = (reason IS NOT NULL))
        )
    """)
    op.execute(
        "CREATE INDEX ix_trading_paper_legs_score ON public.trading_paper_legs (geometry_version,status,labeled_at_ms)"
    )
    op.execute("""
        CREATE TABLE public.trading_analysis_runtime (
          runtime_id text PRIMARY KEY,
          heartbeat_at_ms bigint NOT NULL,
          active_policy text NOT NULL,
          program_sha text NOT NULL,
          model_name text,
          model_configured boolean NOT NULL,
          publish_signals boolean NOT NULL,
          config_digest text NOT NULL,
          fault_code text
        )
    """)
    op.execute(
        "ALTER TABLE public.trading_signals ADD CONSTRAINT trading_signals_case_id_fkey "
        "FOREIGN KEY (case_id) REFERENCES public.trading_cases(case_id)"
    )


def downgrade() -> None:
    raise RuntimeError("Irreversible #746 Analysis hard cut; restore the verified Trading backup")
