"""Persist truthful News Story and provider-score reliability clocks.

Revision ID: 20260806_0244
Revises: 20260806_0243
"""

from __future__ import annotations

from alembic import op

revision = "20260806_0244"
down_revision = "20260806_0243"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE news_items
          ADD COLUMN provider_score_updated_at_ms bigint;

        UPDATE news_items
           SET provider_score_updated_at_ms = updated_at_ms
         WHERE jsonb_typeof(provider_metadata -> 'score') = 'number';

        ALTER TABLE news_items
          ADD CONSTRAINT news_items_provider_score_updated_at_ms_check
          CHECK (
            provider_score_updated_at_ms IS NULL
            OR provider_score_updated_at_ms >= 0
          );

        ALTER TABLE news_projection_summary
          ADD COLUMN last_success_at_ms bigint;

        UPDATE news_projection_summary
           SET last_success_at_ms = CASE
                 WHEN last_error IS NULL
                   THEN COALESCE(last_attempt_at_ms, last_material_change_at_ms)
                 ELSE last_material_change_at_ms
               END;

        ALTER TABLE news_projection_summary
          ADD CONSTRAINT news_projection_summary_last_success_at_ms_check
          CHECK (last_success_at_ms IS NULL OR last_success_at_ms >= 0);

        ALTER TABLE news_push_deliveries
          DROP CONSTRAINT news_push_deliveries_translation_status_check;

        ALTER TABLE news_push_deliveries
          ADD CONSTRAINT news_push_deliveries_translation_status_check
          CHECK (
            translation_status IN (
              'not_requested', 'pending', 'attempted', 'translated',
              'not_needed', 'unavailable'
            )
          );

        ANALYZE news_items;
        ANALYZE news_projection_summary;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260806_0244 is an irreversible News reliability-clock hard cut")
