"""Add a durable intent and settlement ledger for in-place delivery edits.

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
    op.execute(
        """
        ALTER TABLE news_deliveries
          ADD COLUMN edit_state TEXT,
          ADD COLUMN pending_card JSONB,
          ADD COLUMN edit_error_code TEXT,
          ADD COLUMN edit_attempted_at_ms BIGINT,
          ADD COLUMN edit_settled_at_ms BIGINT,
          ADD CONSTRAINT news_deliveries_edit_state_check CHECK (
            edit_state IS NULL OR edit_state IN ('editing', 'edited', 'ambiguous')
          ),
          ADD CONSTRAINT news_deliveries_edit_shape_check CHECK (
            (
              edit_state IS NULL
              AND pending_card IS NULL
              AND edit_error_code IS NULL
              AND edit_attempted_at_ms IS NULL
              AND edit_settled_at_ms IS NULL
            ) OR (
              edit_state IS NOT NULL
              AND edit_state = 'editing'
              AND pending_card IS NOT NULL
              AND edit_error_code IS NULL
              AND edit_attempted_at_ms IS NOT NULL
              AND edit_settled_at_ms IS NULL
            ) OR (
              edit_state IS NOT NULL
              AND edit_state = 'edited'
              AND pending_card IS NULL
              AND edit_error_code IS NULL
              AND edit_attempted_at_ms IS NOT NULL
              AND edit_settled_at_ms IS NOT NULL
            ) OR (
              edit_state IS NOT NULL
              AND edit_state = 'ambiguous'
              AND pending_card IS NOT NULL
              AND edit_error_code IS NOT NULL
              AND edit_attempted_at_ms IS NOT NULL
              AND edit_settled_at_ms IS NOT NULL
            )
          )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_deliveries_editing
          ON news_deliveries (edit_attempted_at_ms, event_id)
          WHERE edit_state = 'editing'
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260828_0317 owns durable provider edit intent and cannot be downgraded")
