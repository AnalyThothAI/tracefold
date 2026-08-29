"""Add an auditable provider-delete intent for confirmed untradeable single-name cards.

Revision ID: 20260828_0323
Revises: 20260828_0322
"""

from __future__ import annotations

from alembic import op

revision = "20260828_0323"
down_revision = "20260828_0322"
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
               AND column_name = 'delete_state'
          ) INTO preexisting;

          IF preexisting THEN
            IF (
              SELECT count(*)
                FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'news_deliveries'
                 AND (column_name, data_type) IN (
                   ('delete_state', 'text'), ('delete_evidence', 'jsonb'),
                   ('delete_reason', 'text'), ('delete_error_code', 'text'),
                   ('delete_attempted_at_ms', 'bigint'), ('delete_settled_at_ms', 'bigint')
                 )
            ) <> 6 OR (
              SELECT count(*) FROM pg_constraint
               WHERE conrelid = 'news_deliveries'::regclass
                 AND conname IN (
                   'news_deliveries_delete_state_check', 'news_deliveries_delete_shape_check'
                 )
            ) <> 2 OR NOT EXISTS (
              SELECT 1 FROM pg_indexes
               WHERE schemaname = 'public' AND tablename = 'news_deliveries'
                 AND indexname = 'ix_news_deliveries_deleting'
            ) THEN
              RAISE EXCEPTION 'news_delivery_delete_schema_collision';
            END IF;
          ELSE
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
              );
            CREATE INDEX ix_news_deliveries_deleting
              ON news_deliveries (delete_attempted_at_ms, event_id)
              WHERE delete_state = 'deleting';
          END IF;
        END
        $migration$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260828_0323 owns durable provider delete intent and cannot be downgraded")
