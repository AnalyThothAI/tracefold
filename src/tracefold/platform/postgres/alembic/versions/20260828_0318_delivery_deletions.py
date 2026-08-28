"""Add an auditable provider-delete intent for confirmed untradeable single-name cards.

Revision ID: 20260828_0318
Revises: 20260828_0317
"""

from __future__ import annotations

from alembic import op

revision = "20260828_0318"
down_revision = "20260828_0317"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE news_deliveries
          ADD COLUMN delete_state TEXT,
          ADD COLUMN delete_evidence JSONB,
          ADD COLUMN delete_reason TEXT,
          ADD COLUMN delete_error_code TEXT,
          ADD COLUMN delete_attempted_at_ms BIGINT,
          ADD COLUMN delete_settled_at_ms BIGINT,
          ADD CONSTRAINT news_deliveries_delete_state_check CHECK (
            delete_state IS NULL OR delete_state IN ('deleting', 'deleted', 'ambiguous')
          ),
          ADD CONSTRAINT news_deliveries_delete_shape_check CHECK (
            (
              delete_state IS NULL
              AND delete_evidence IS NULL
              AND delete_reason IS NULL
              AND delete_error_code IS NULL
              AND delete_attempted_at_ms IS NULL
              AND delete_settled_at_ms IS NULL
            ) OR (
              delete_state = 'deleting'
              AND delete_evidence IS NOT NULL
              AND delete_reason IS NOT NULL
              AND delete_error_code IS NULL
              AND delete_attempted_at_ms IS NOT NULL
              AND delete_settled_at_ms IS NULL
            ) OR (
              delete_state = 'deleted'
              AND delete_evidence IS NOT NULL
              AND delete_reason IS NOT NULL
              AND delete_error_code IS NULL
              AND delete_attempted_at_ms IS NOT NULL
              AND delete_settled_at_ms IS NOT NULL
            ) OR (
              delete_state = 'ambiguous'
              AND delete_evidence IS NOT NULL
              AND delete_reason IS NOT NULL
              AND delete_error_code IS NOT NULL
              AND delete_attempted_at_ms IS NOT NULL
              AND delete_settled_at_ms IS NOT NULL
            )
          )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_deliveries_deleting
          ON news_deliveries (delete_attempted_at_ms, event_id)
          WHERE delete_state = 'deleting'
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260828_0318 owns durable provider delete intent and cannot be downgraded")
