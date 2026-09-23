"""Durable News/OI handoff and fenced Trading analysis ledger (#683).

Migration evidence:
- category: additive fact and decision tables; Case vocabulary and pending uniqueness hard cut.
- why_database_must_change: source delivery must survive Analysis outages, long Agent calls need
  claim fencing, and a root fact can have distinct initial and WATCH Cases without a second entry.
- current/minimum_source_revision: 20260923_0390.
- lock_level_and_order: stop the old Signal lane, then ACCESS EXCLUSIVE on trading_cases for the
  two CHECK changes and index replacement; new tables and columns are empty/nullable.
- statement_timeout: 60s; lock_timeout: 5s.
- estimated_rows/bytes: existing Cases are unchanged; new tables empty; no heap rewrite.
- rewrite_or_index_build: one partial Case index replaces the old pending uniqueness index.
- preflight: stop old Alpha producer and confirm a backup before schema migration. Historical
  Signals and TradePlans remain readable; unexecuted old Signals are retired at cutover.
- role_and_grant_impact: unchanged single application login and default privileges.
- failure_state: transactional rollback restores the prior schema.
- roll_forward_or_verified_backup_restore: forward revision or restore the verified archive.

Revision ID: 20260923_0391
Revises: 20260923_0390
"""

from __future__ import annotations

from alembic import op

revision = "20260923_0391"
down_revision = "20260923_0390"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("""
        CREATE TABLE public.news_trade_events (
            event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            kind text NOT NULL CHECK (kind IN ('catalyst', 'oi')),
            source_fact_key text NOT NULL,
            source_revision text NOT NULL,
            payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            payload jsonb NOT NULL,
            source_recorded_at_ms bigint NOT NULL,
            acknowledged_at_ms bigint,
            rejected_reason text,
            conflict_sha256 text,
            UNIQUE (kind, source_fact_key, source_revision),
            CHECK (acknowledged_at_ms IS NULL OR rejected_reason IS NULL)
        )
    """)
    op.execute(
        "CREATE INDEX ix_news_trade_events_unack ON public.news_trade_events (event_id) "
        "WHERE acknowledged_at_ms IS NULL AND rejected_reason IS NULL"
    )
    op.execute("""
        CREATE TABLE public.trading_analysis_runtime (
            runtime_id text PRIMARY KEY,
            heartbeat_at_ms bigint NOT NULL,
            active_policy text NOT NULL,
            model_name text,
            model_configured boolean NOT NULL,
            publish_signals boolean NOT NULL,
            config_digest text NOT NULL CHECK (config_digest ~ '^[0-9a-f]{64}$')
        )
    """)
    op.execute("""
        CREATE TABLE public.trading_triggers (
            trigger_id text PRIMARY KEY CHECK (trigger_id ~ '^[0-9a-f]{64}$'),
            kind text NOT NULL CHECK (kind IN ('catalyst', 'oi')),
            source_fact_key text NOT NULL,
            source_revision text NOT NULL,
            payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            payload jsonb NOT NULL,
            asset_id text,
            target_selection jsonb NOT NULL,
            first_visible_at_ms bigint NOT NULL,
            source_observed_at_ms bigint NOT NULL,
            root_expires_at_ms bigint NOT NULL,
            supersedes_ref text,
            created_at_ms bigint NOT NULL,
            UNIQUE (kind, source_fact_key, source_revision)
        )
    """)
    op.execute("""
        CREATE TABLE public.trading_trigger_conflicts (
            kind text NOT NULL,
            source_fact_key text NOT NULL,
            source_revision text NOT NULL,
            attempted_sha256 text NOT NULL,
            original_sha256 text NOT NULL,
            observed_at_ms bigint NOT NULL,
            PRIMARY KEY (kind, source_fact_key, source_revision, attempted_sha256)
        )
    """)
    op.execute("DROP INDEX public.ux_trading_case_in_flight_underlying")
    op.execute("ALTER TABLE public.trading_cases DROP CONSTRAINT trading_cases_primary_source_key_unique")
    op.execute("ALTER TABLE public.trading_cases DROP CONSTRAINT trading_cases_state_check")
    op.execute("""
        ALTER TABLE public.trading_cases ADD CONSTRAINT trading_cases_state_check
          CHECK (state IN ('PENDING','RUNNING','DONE','FAILED','EXCLUDED',
                           'NO_TRADE','SIGNAL_EMITTED','BLOCKED'))
    """)
    op.execute("ALTER TABLE public.trading_cases DROP CONSTRAINT trading_cases_policy_decision_check")
    op.execute("""
        ALTER TABLE public.trading_cases ADD CONSTRAINT trading_cases_policy_decision_check
          CHECK (policy_decision IN ('long','short','no_trade','watch','not_run'))
    """)
    op.execute("ALTER TABLE public.trading_cases DROP CONSTRAINT trading_cases_trigger_kind_check")
    op.execute("""
        ALTER TABLE public.trading_cases ADD CONSTRAINT trading_cases_trigger_kind_check
          CHECK (trigger_kind IN ('oi','catalyst','news','liquidation'))
    """)
    op.execute("""
        ALTER TABLE public.trading_cases
          ADD COLUMN trigger_id text REFERENCES public.trading_triggers(trigger_id),
          ADD COLUMN run_kind text CHECK (run_kind IN ('initial','recheck')),
          ADD COLUMN recheck_seq integer,
          ADD COLUMN target_asset_id text,
          ADD COLUMN target_selection jsonb,
          ADD COLUMN entry_scope_id text,
          ADD COLUMN mapping_semantics_digest text,
          ADD COLUMN root_expires_at_ms bigint,
          ADD COLUMN work_deadline_at_ms bigint,
          ADD COLUMN next_attempt_at_ms bigint,
          ADD COLUMN claim_token text,
          ADD COLUMN lease_until_ms bigint,
          ADD COLUMN claim_attempt integer NOT NULL DEFAULT 0,
          ADD COLUMN analysis_status text,
          ADD COLUMN evidence_ref text
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_trading_case_trigger_run
          ON public.trading_cases (trigger_id, run_kind, recheck_seq)
          WHERE trigger_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_trading_case_running_asset
          ON public.trading_cases (target_asset_id)
          WHERE state = 'RUNNING' AND target_asset_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_trading_case_due
          ON public.trading_cases (next_attempt_at_ms, created_at_ms, case_id)
          WHERE state IN ('PENDING','RUNNING') AND trigger_id IS NOT NULL
    """)
    op.execute("""
        CREATE TABLE public.trading_case_decisions (
            case_id text PRIMARY KEY REFERENCES public.trading_cases(case_id) ON DELETE RESTRICT,
            decision_id text NOT NULL UNIQUE,
            policy_id text NOT NULL,
            policy_version text NOT NULL,
            input_ref text NOT NULL,
            assessment_ref text,
            action text NOT NULL CHECK (action IN ('TRADE','NO_TRADE','WATCH')),
            decision jsonb NOT NULL,
            publish_status text NOT NULL,
            publish_reason text,
            decided_at_ms bigint NOT NULL,
            valid_until_ms bigint NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE public.trading_case_outcomes (
            case_id text NOT NULL REFERENCES public.trading_cases(case_id) ON DELETE RESTRICT,
            axis text NOT NULL CHECK (axis IN ('source','decision')),
            horizon_seconds integer NOT NULL CHECK (horizon_seconds > 0),
            label_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('pending','ok','missing')),
            return_bps numeric,
            available_at_ms bigint NOT NULL,
            next_attempt_at_ms bigint NOT NULL DEFAULT 0,
            labeled_at_ms bigint,
            path_ref text,
            PRIMARY KEY (case_id, axis, horizon_seconds, label_version),
            CHECK ((status = 'ok') = (return_bps IS NOT NULL))
        )
    """)


def downgrade() -> None:
    raise RuntimeError("trading_analysis_foundation_forward_only: restore a verified pre-0391 archive")
