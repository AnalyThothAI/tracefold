"""Admit venue funding cashflows and complete income-scan coverage in the execution ledger.

Revision ID: 20260924_0394
Revises: 20260924_0393

Additive observation kinds; historical rows remain unchanged. PAPER income
reconciliation supplies signed cashflows and coverage before net is known.
"""

from __future__ import annotations

from alembic import op

revision = "20260924_0394"
down_revision = "20260924_0393"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute(
        "ALTER TABLE public.trading_execution_observations DROP CONSTRAINT trading_execution_observation_kind_check"
    )
    op.execute(
        """
        ALTER TABLE public.trading_execution_observations
          ADD CONSTRAINT trading_execution_observation_kind_check
          CHECK (normalized_kind IN (
            'signal_disposition','control_disposition','risk','order','fill',
            'position','protection','funding','funding_coverage'
          ))
        """
    )
    op.execute(
        "CREATE INDEX ix_trading_execution_funding_slot_time "
        "ON public.trading_execution_observations(account_slot, occurred_at_ns) "
        "WHERE normalized_kind IN ('funding', 'funding_coverage')"
    )


def downgrade() -> None:
    raise RuntimeError("paper_funding_observations_forward_only: restore a verified pre-0394 archive")
