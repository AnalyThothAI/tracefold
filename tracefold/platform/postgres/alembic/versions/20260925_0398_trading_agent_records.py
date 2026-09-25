"""Retain actual Trading Agent manifest, termination and per-call model route.

Migration evidence:

- category: additive audit columns, new-write checks, and Signal index renames.
- why_database_must_change: Case replay needs the actual model route and terminal
  receipt; new writes must use the one V3 Signal, directed WATCH, V4 Decision contract.
- current_source_revision: 20260925_0397
- minimum_supported_source_revision: 20260925_0397
- lock_level_and_order: acquire brief ACCESS EXCLUSIVE locks on the four affected
  tables in the listed DDL order; no external I/O occurs in the transaction.
- statement_timeout: 60s locally; lock_timeout: 5s locally.
- estimated_rows: existing business rows are untouched; no backfill.
- estimated_bytes: only small catalog additions; no table rewrite or index build.
- preflight_and_maintenance_boundary: stop writers, reconcile pre-V3 active
  plans with the venue, then migrate before starting the new image.
- archive_current_compatibility: historical rows remain readable; NOT VALID
  checks reject old-format new writes without validating old rows.
- role_and_grant_impact: none; the single tracefold login remains unchanged.
- failure_state: transactional DDL rolls back on any failure.
- roll_forward_or_verified_backup_restore: restore the verified pre-cut archive
  if the new image cannot start; never infer a fill from a pending plan.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260925_0398
Revises: 20260925_0397
"""

from __future__ import annotations

from alembic import op

revision = "20260925_0398"
down_revision = "20260925_0397"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute(
        "ALTER TABLE public.trading_case_attempts "
        "ADD COLUMN final_manifest_ref text, ADD COLUMN termination_reason text"
    )
    op.execute(
        "ALTER TABLE public.trading_model_calls "
        "ADD COLUMN phase text, ADD COLUMN endpoint text, "
        "ADD COLUMN requested_model text, ADD COLUMN served_model text"
    )
    # Historical rows remain queryable. NOT VALID avoids rewriting them while
    # every new durable fact must use the one current contract.
    op.execute(
        "ALTER TABLE public.trading_trade_signals ADD CONSTRAINT trading_signal_v3_only "
        "CHECK (COALESCE(payload ->> 'signal_version' = 'trade_signal_v3', false)) NOT VALID"
    )
    op.execute(
        "ALTER TABLE public.trading_watch_observations ADD CONSTRAINT trading_watch_directed_only "
        "CHECK (COALESCE(condition ->> 'kind' = 'closed_1m_directed_cross', false)) NOT VALID"
    )
    op.execute(
        "ALTER TABLE public.trading_case_decisions ADD CONSTRAINT trading_decision_v4_only "
        "CHECK (policy_version = 'v4' AND "
        "COALESCE(decision ->> 'decision_version' = 'trade_decision_v4', false)) NOT VALID"
    )
    op.execute(
        "ALTER INDEX public.ix_trading_trade_signals_v2_account RENAME TO ix_trading_trade_signals_account_mode_expiry"
    )
    op.execute("ALTER INDEX public.ix_trading_trade_signals_unresolved RENAME TO ix_trading_trade_signals_seq_payload")


def downgrade() -> None:
    raise RuntimeError("trading_agent_records_forward_only: restore a verified pre-0398 archive")
