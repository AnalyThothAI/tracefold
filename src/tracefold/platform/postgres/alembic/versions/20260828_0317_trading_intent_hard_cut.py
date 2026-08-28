"""Make Nautilus the sole execution authority (#283 PR 2).

Revision ID: 20260828_0317
Revises: 20260828_0316
"""

from __future__ import annotations

from alembic import op

revision = "20260828_0317"
down_revision = "20260828_0316"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This migration is the atomic authority cut. History stays readable, but any unresolved row
    # means the old writer may still own exposure and the cut must stop before changing privileges.
    op.execute(
        """
        DO $cutover$
        BEGIN
          IF EXISTS (SELECT 1 FROM trading_cases WHERE state IN ('PENDING', 'RUNNING')) THEN
            RAISE EXCEPTION 'trading_hard_cut_pending_case';
          END IF;
          IF EXISTS (
            SELECT 1 FROM trading_intents
             WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
          ) THEN
            RAISE EXCEPTION 'trading_hard_cut_nonterminal_intent';
          END IF;
          IF EXISTS (
            SELECT 1 FROM trading_orders
             WHERE state IN (
               'PREPARED', 'AWAITING_APPROVAL', 'APPROVED', 'SUBMITTING', 'AMBIGUOUS',
               'RECONCILING', 'MANUAL_REVIEW_REQUIRED', 'ACKNOWLEDGED', 'PARTIAL',
               'OPEN', 'UNPROTECTED', 'SAFETY_CLOSING'
             )
          ) THEN
            RAISE EXCEPTION 'trading_hard_cut_active_legacy_order';
          END IF;
        END
        $cutover$
        """
    )

    op.execute("ALTER TABLE trading_cases DROP CONSTRAINT trading_cases_state_check")
    op.execute(
        """
        ALTER TABLE trading_cases
          ADD CONSTRAINT trading_cases_state_check CHECK (
            state IN (
              'PENDING', 'RUNNING', 'NO_TRADE', 'POLICY_REJECTED',
              'INTENT_EMITTED', 'ORDER_PREPARED', 'BLOCKED'
            )
          )
        """
    )

    # The tables remain immutable audit history. Serve can still read them, while Workers lose every
    # legacy execution mutation privilege in the same migration that admits INTENT_EMITTED.
    op.execute("REVOKE INSERT, UPDATE, DELETE ON trading_orders, trading_order_observations FROM tracefold_workers")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON trading_runtime_state FROM tracefold_workers")
    op.execute(
        """
        GRANT UPDATE (control, day_key, dspy_calls_today, funnel, updated_at_ms)
          ON trading_runtime_state TO tracefold_workers
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260828_0317 removes the legacy execution authority and cannot be downgraded")
