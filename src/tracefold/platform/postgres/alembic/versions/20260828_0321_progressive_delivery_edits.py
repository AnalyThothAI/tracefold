"""Add a durable intent and settlement ledger for in-place delivery edits.

Revision ID: 20260828_0321
Revises: 20260828_0320
"""

from __future__ import annotations

from alembic import op

revision = "20260828_0321"
down_revision = "20260828_0320"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $migration$
        DECLARE
          preexisting boolean;
        BEGIN
          SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'news_deliveries'
               AND column_name = 'edit_state'
          ) INTO preexisting;

          IF preexisting THEN
            IF (
              SELECT count(*)
                FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'news_deliveries'
                 AND (column_name, data_type) IN (
                   ('edit_state', 'text'), ('pending_card', 'jsonb'),
                   ('edit_error_code', 'text'), ('edit_attempted_at_ms', 'bigint'),
                   ('edit_settled_at_ms', 'bigint')
                 )
            ) <> 5 OR (
              SELECT count(*) FROM pg_constraint
               WHERE conrelid = 'news_deliveries'::regclass
                 AND conname IN (
                   'news_deliveries_edit_state_check', 'news_deliveries_edit_shape_check'
                 )
            ) <> 2 OR NOT EXISTS (
              SELECT 1 FROM pg_indexes
               WHERE schemaname = 'public' AND tablename = 'news_deliveries'
                 AND indexname = 'ix_news_deliveries_editing'
            ) THEN
              RAISE EXCEPTION 'news_delivery_edit_schema_collision';
            END IF;
          ELSE
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
                  edit_state = 'editing'
                  AND pending_card IS NOT NULL
                  AND edit_error_code IS NULL
                  AND edit_attempted_at_ms IS NOT NULL
                  AND edit_settled_at_ms IS NULL
                ) OR (
                  edit_state = 'edited'
                  AND pending_card IS NULL
                  AND edit_error_code IS NULL
                  AND edit_attempted_at_ms IS NOT NULL
                  AND edit_settled_at_ms IS NOT NULL
                ) OR (
                  edit_state = 'ambiguous'
                  AND pending_card IS NOT NULL
                  AND edit_error_code IS NOT NULL
                  AND edit_attempted_at_ms IS NOT NULL
                  AND edit_settled_at_ms IS NOT NULL
                )
              );
            CREATE INDEX ix_news_deliveries_editing
              ON news_deliveries (edit_attempted_at_ms, event_id)
              WHERE edit_state = 'editing';
          END IF;
        END
        $migration$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260828_0321 owns durable provider edit intent and cannot be downgraded")
