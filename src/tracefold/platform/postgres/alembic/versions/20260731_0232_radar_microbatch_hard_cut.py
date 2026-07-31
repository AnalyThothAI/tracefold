"""Replace Radar target/RankSet fan-out with claimed target micro-batches.

Revision ID: 20260731_0232
Revises: 20260731_0231
"""

from __future__ import annotations

from alembic import op

revision = "20260731_0232"
down_revision = "20260731_0231"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM radar_projection_frontiers
        WHERE target_type = 'RankSet';

        UPDATE radar_projection_frontiers
        SET status = 'dirty',
            claimed_by = NULL,
            claimed_until_ms = NULL,
            next_attempt_at_ms = NULL,
            last_error_code = 'migration_replay',
            updated_at_ms = (
              extract(epoch FROM clock_timestamp()) * 1000
            )::bigint
        WHERE status = 'running';

        ALTER TABLE radar_projection_frontiers
          DROP CONSTRAINT IF EXISTS radar_projection_frontiers_pending_check,
          DROP COLUMN IF EXISTS pending_first_dirty_at_ms,
          DROP COLUMN IF EXISTS pending_deadline_at_ms,
          DROP COLUMN IF EXISTS pending_input_fingerprint,
          DROP COLUMN IF EXISTS pending_projection_version,
          ADD COLUMN claimed_input_fingerprint text,
          ADD COLUMN claimed_projection_version text,
          ADD CONSTRAINT radar_projection_frontiers_claim_snapshot_check
            CHECK (
              (
                status = 'running'
                AND claimed_by IS NOT NULL
                AND claimed_until_ms IS NOT NULL
                AND claimed_input_fingerprint IS NOT NULL
                AND claimed_projection_version IS NOT NULL
              )
              OR (
                status <> 'running'
                AND claimed_by IS NULL
                AND claimed_until_ms IS NULL
                AND claimed_input_fingerprint IS NULL
                AND claimed_projection_version IS NULL
              )
            );

        DROP INDEX IF EXISTS idx_radar_projection_frontiers_eligible;
        CREATE INDEX idx_radar_projection_frontiers_microbatch_eligible
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

        CREATE INDEX idx_radar_projection_frontiers_expired_claim
          ON radar_projection_frontiers(
            claimed_until_ms,
            deadline_at_ms,
            window_key,
            venue,
            target_type,
            target_id
          )
          WHERE status = 'running';
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260731_0232 is an irreversible Radar micro-batch hard cut")
