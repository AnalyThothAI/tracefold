"""Separate projection eligibility from freshness deadlines.

Revision ID: 20260731_0230
Revises: 20260731_0229
"""

from __future__ import annotations

from alembic import op

revision = "20260731_0230"
down_revision = "20260731_0229"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE news_projection_frontiers
        SET next_attempt_at_ms = deadline_at_ms,
            deadline_at_ms = deadline_at_ms + 60000,
            updated_at_ms = (
              extract(epoch FROM clock_timestamp()) * 1000
            )::bigint
        WHERE status = 'dirty'
          AND next_attempt_at_ms IS NULL
          AND first_dirty_at_ms IS NOT NULL
          AND deadline_at_ms IS NOT NULL
          AND deadline_at_ms - first_dirty_at_ms > 60000;

        DROP INDEX IF EXISTS idx_radar_projection_frontiers_due;
        DROP INDEX IF EXISTS idx_macro_module_frontiers_due;
        DROP INDEX IF EXISTS idx_news_projection_frontiers_due;
        DROP INDEX IF EXISTS idx_token_profile_projection_frontiers_due;

        CREATE INDEX idx_radar_projection_frontiers_eligible
          ON radar_projection_frontiers(
            (
              COALESCE(
                next_attempt_at_ms,
                first_dirty_at_ms,
                deadline_at_ms
              )
            ),
            deadline_at_ms,
            window_key,
            venue,
            target_type,
            target_id
          )
          WHERE status IN ('dirty', 'retry_wait');

        CREATE INDEX idx_macro_module_frontiers_eligible
          ON macro_module_frontiers(
            (
              COALESCE(
                next_attempt_at_ms,
                first_dirty_at_ms,
                deadline_at_ms
              )
            ),
            deadline_at_ms,
            module_id
          )
          WHERE status IN ('dirty', 'retry_wait');

        CREATE INDEX idx_news_projection_frontiers_eligible
          ON news_projection_frontiers(
            (
              COALESCE(
                next_attempt_at_ms,
                first_dirty_at_ms,
                deadline_at_ms
              )
            ),
            deadline_at_ms,
            bucket_id
          )
          WHERE status IN ('dirty', 'retry_wait');

        CREATE INDEX idx_token_profile_projection_frontiers_eligible
          ON token_profile_projection_frontiers(
            (
              COALESCE(
                next_attempt_at_ms,
                first_dirty_at_ms,
                deadline_at_ms
              )
            ),
            deadline_at_ms,
            target_type,
            target_id
          )
          WHERE status IN ('dirty', 'retry_wait');
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260731_0230 is an irreversible projection eligibility hard cut")
