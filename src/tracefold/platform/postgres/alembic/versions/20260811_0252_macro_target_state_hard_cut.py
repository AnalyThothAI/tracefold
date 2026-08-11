"""Hard-cut Macro acquisition targets to the reachable state machine.

The target-state rewrite changes every module's deterministic projection input.
Keep the current serving rows available, but invalidate the rebuildable frontiers
so worker startup reconciliation republishes all six modules from persisted facts.

Revision ID: 20260811_0252
Revises: 20260810_0251
"""

from __future__ import annotations

from alembic import op

revision = "20260811_0252"
down_revision = "20260810_0251"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        """
        UPDATE macro_acquisition_targets
           SET status = CASE
                 WHEN clock_kind = 'backfill' THEN 'stale'
                 ELSE 'delayed'
               END,
               attempt_count = CASE
                 WHEN clock_kind = 'backfill' THEN attempt_count
                 ELSE 0
               END,
               next_due_at_ms = CASE
                 WHEN clock_kind = 'backfill' THEN next_due_at_ms
                 ELSE LEAST(next_due_at_ms, updated_at_ms)
               END
         WHERE status = 'invalid'
            OR (status = 'stale' AND clock_kind <> 'backfill');

        ALTER TABLE macro_acquisition_targets
          DROP CONSTRAINT macro_acquisition_targets_status_check;
        ALTER TABLE macro_acquisition_targets
          ADD CONSTRAINT macro_acquisition_targets_status_check
          CHECK (
            status IN (
              'pending', 'claimed', 'current', 'delayed',
              'stale', 'unavailable', 'backfilling'
            )
          );

        DROP INDEX idx_macro_acquisition_targets_due;
        CREATE INDEX idx_macro_acquisition_targets_due
          ON macro_acquisition_targets(
            clock_kind, priority, next_due_at_ms, target_key
          )
          WHERE status IN (
            'pending', 'claimed', 'current', 'delayed', 'backfilling'
          );

        DELETE FROM macro_module_frontiers;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260811_0252 is an irreversible Macro target-state hard cut")
