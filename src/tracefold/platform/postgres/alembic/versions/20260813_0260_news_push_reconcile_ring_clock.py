"""Persist the minimum News Push reconcile-ring clock.

Revision ID: 20260813_0260
Revises: 20260813_0259
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0260"
down_revision = "20260813_0259"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    # Alembic applies 0260 and the production-scale 0261 Event rewrite in one
    # transaction when upgrading from 0259 to head. This first revision owns
    # the whole cut's transaction timer, so it must preserve 0261's envelope.
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        ALTER TABLE news_push_state
          ADD COLUMN reconcile_cycle_started_at_ms bigint;

        UPDATE news_push_state
           SET reconcile_cycle_started_at_ms = updated_at_ms
         WHERE reconcile_cursor_story_id IS NOT NULL;

        ALTER TABLE news_push_state
          ADD CONSTRAINT news_push_state_reconcile_cycle_started_at_ms_check
          CHECK (
            reconcile_cycle_started_at_ms IS NULL
            OR reconcile_cycle_started_at_ms >= 0
          ),
          ADD CONSTRAINT news_push_state_reconcile_cycle_cursor_check
          CHECK (
            reconcile_cursor_story_id IS NULL
            OR reconcile_cycle_started_at_ms IS NOT NULL
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260813_0260 is an irreversible News Push reconcile-ring clock cut")
