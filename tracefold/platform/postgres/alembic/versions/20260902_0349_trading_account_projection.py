"""Add the bounded current account read model to the Runtime projection.

Migration evidence:

- category: additive current read projection
- why_database_must_change: the operator page must show current equity, risk,
  position, protection, and order facts without folding an observation window
- current_source_revision: 20260902_0348
- minimum_supported_source_revision: 20260902_0348
- lock_level_and_order: one short ALTER TABLE while canonical migration has
  stopped every steady application process
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: one current row per account slot, currently single digits
- estimated_bytes: bounded JSON, at most 100 positions and 200 orders per row
- rewrite_or_index_build: nullable metadata-only column; no index or heap rewrite
- preflight_and_maintenance_boundary: ordinary canonical migration stop
- role_and_grant_impact: none; the existing Runtime owner writes the same table
- failure_state: the transaction rolls back completely
- roll_forward_or_verified_backup_restore: correct with a forward revision or
  restore the verified backup
- archive_current_compatibility: Signal, Command, and Observation history is
  unchanged; this is only the replaceable current read model
- evidence_postgresql_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260902_0349
Revises: 20260902_0348
Create Date: 2026-09-02 15:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260902_0349"
down_revision = "20260902_0348"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("ALTER TABLE trading_execution_runtime_state ADD COLUMN account_snapshot jsonb")
    op.execute(
        """
        ALTER TABLE trading_execution_runtime_state
          ADD CONSTRAINT trading_execution_runtime_account_snapshot_check CHECK (
            account_snapshot IS NULL OR (
              jsonb_typeof(account_snapshot) = 'object'
              AND trading_jsonb_object_size(account_snapshot) = 15
              AND account_snapshot ?& ARRAY[
                'version', 'observed_at_ns', 'market_observed_at_ns', 'equity_usd',
                'day_start_equity_usd', 'daily_drawdown_usd', 'daily_drawdown_bps',
                'aggregate_risk_usd', 'positions', 'orders', 'open_orders_count',
                'inflight_orders_count', 'unknown_orders_count', 'complete', 'truncated'
              ]
              AND octet_length(account_snapshot::text) <= 262144
              AND account_snapshot ->> 'version' = 'execution_account_snapshot_v1'
              AND jsonb_typeof(account_snapshot -> 'observed_at_ns') = 'number'
              AND jsonb_typeof(account_snapshot -> 'market_observed_at_ns') IN ('null', 'number')
              AND jsonb_typeof(account_snapshot -> 'equity_usd') IN ('null', 'string')
              AND jsonb_typeof(account_snapshot -> 'day_start_equity_usd') IN ('null', 'string')
              AND jsonb_typeof(account_snapshot -> 'daily_drawdown_usd') IN ('null', 'string')
              AND jsonb_typeof(account_snapshot -> 'daily_drawdown_bps') IN ('null', 'number')
              AND jsonb_typeof(account_snapshot -> 'aggregate_risk_usd') IN ('null', 'string')
              AND jsonb_typeof(account_snapshot -> 'positions') = 'array'
              AND jsonb_array_length(account_snapshot -> 'positions') <= 100
              AND jsonb_typeof(account_snapshot -> 'orders') = 'array'
              AND jsonb_array_length(account_snapshot -> 'orders') <= 200
              AND (account_snapshot ->> 'observed_at_ns')::bigint > 0
              AND (account_snapshot ->> 'open_orders_count')::integer >= 0
              AND (account_snapshot ->> 'inflight_orders_count')::integer >= 0
              AND (account_snapshot ->> 'unknown_orders_count')::integer >= 0
              AND jsonb_typeof(account_snapshot -> 'complete') = 'boolean'
              AND jsonb_typeof(account_snapshot -> 'truncated') = 'boolean'
            )
          )
        """
    )


def downgrade() -> None:
    raise RuntimeError("trading_account_projection_forward_only")
