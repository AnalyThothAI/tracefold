"""Keep runtime health separate from account, convergence and venue evidence (#699).

Migration evidence:
- category: additive current-state diagnostics, no ledger or Plan rewrite.
- why_database_must_change: the existing Runtime row must preserve the last successful
  account and venue times while a new health heartbeat records a failed check.
- current_source_revision: 20260925_0399
- minimum_supported_source_revision: 20260925_0399
- lock_level_and_order: one brief ACCESS EXCLUSIVE catalog change on runtime state.
- statement_timeout: 60s locally; lock_timeout: 5s locally.
- estimated_rows: one current row per account slot; nullable columns need no backfill.
- estimated_bytes: catalog addition, no index or table rewrite.
- preflight_and_maintenance_boundary: stop Runtime and serve, migrate, then start
  the matching Runtime and serve/web image as one contract cut.
- archive_current_compatibility: existing current rows retain account facts and have
  unknown check diagnostics until a new generation writes them.
- role_and_grant_impact: none.
- failure_state: transactional DDL rolls back.
- roll_forward_or_verified_backup_restore: restore the verified pre-cut archive
  with the corresponding pre-cut images; do not run an old Runtime on this contract.

Revision ID: 20260925_0400
Revises: 20260925_0399
"""

from __future__ import annotations

from alembic import op

revision = "20260925_0400"
down_revision = "20260925_0399"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute(
        "ALTER TABLE public.trading_execution_runtime_state DROP CONSTRAINT trading_execution_runtime_protection_check"
    )
    op.execute(
        "ALTER TABLE public.trading_execution_runtime_state "
        "ADD CONSTRAINT trading_execution_runtime_protection_check "
        "CHECK (protection_status IN ('not_applicable', 'protected', 'pending', 'unprotected', 'unknown'))"
    )
    op.execute(
        "ALTER TABLE public.trading_execution_runtime_state "
        "ADD COLUMN account_projection_failure text, "
        "ADD COLUMN convergence_checked_at_ns bigint, "
        "ADD COLUMN convergence_failure text, "
        "ADD COLUMN venue_read_started_at_ns bigint, "
        "ADD COLUMN venue_read_completed_at_ns bigint, "
        "ADD COLUMN venue_read_failure text, "
        "ADD COLUMN recovery_attempted_at_ns bigint, "
        "ADD COLUMN recovery_result text"
    )
    # One-time hard cut for the current row; retain its last observed Cache facts,
    # but remove the old price-only protection and loose ownership claims.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM public.trading_execution_runtime_state
             WHERE account_snapshot IS NOT NULL
               AND account_snapshot ->> 'version' IS DISTINCT FROM 'execution_account_snapshot_v2'
          ) THEN
            RAISE EXCEPTION 'unexpected execution account snapshot version';
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        UPDATE public.trading_execution_runtime_state AS state
           SET protection_status = CASE
                 WHEN state.positions_count > 0 THEN 'unknown'
                 ELSE 'not_applicable'
               END,
               account_snapshot = state.account_snapshot || jsonb_build_object(
             'version', 'execution_account_snapshot_v3',
             'positions_total', jsonb_array_length(state.account_snapshot -> 'positions'),
             'orders_total', jsonb_array_length(state.account_snapshot -> 'orders'),
             'findings', '[]'::jsonb,
             'findings_total', 0,
             'positions', (
                SELECT COALESCE(jsonb_agg(item.value || jsonb_build_object(
                    'source', 'cache', 'owned', false, 'plan_entry_id', NULL,
                    'protection_status', 'unknown'
                ) ORDER BY item.ordinal), '[]'::jsonb)
                  FROM jsonb_array_elements(state.account_snapshot -> 'positions')
                       WITH ORDINALITY AS item(value, ordinal)
             ),
             'orders', (
                SELECT COALESCE(jsonb_agg(item.value || jsonb_build_object(
                    'owned', false, 'plan_entry_id', NULL
                ) ORDER BY item.ordinal), '[]'::jsonb)
                  FROM jsonb_array_elements(state.account_snapshot -> 'orders')
                       WITH ORDINALITY AS item(value, ordinal)
             )
           )
         WHERE state.account_snapshot IS NOT NULL
        """
    )


def downgrade() -> None:
    raise RuntimeError("trading_runtime_observation_truth_forward_only: restore a verified pre-0400 archive")
