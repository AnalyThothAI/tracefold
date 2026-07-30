"""Hard-cut the asset-profile queue to heat tiers and terminal outcomes."""

from __future__ import annotations

from alembic import op

revision = "20260730_0220"
down_revision = "20260730_0219"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE asset_profile_refresh_targets
          ADD COLUMN heat_tier text NOT NULL DEFAULT 'cold',
          ADD COLUMN terminal_reason text;

        ALTER TABLE asset_profile_refresh_targets
          ADD CONSTRAINT asset_profile_refresh_targets_heat_tier_check
          CHECK (heat_tier IN ('hot', 'warm', 'cold'));

        UPDATE asset_profile_refresh_targets AS queue
        SET heat_tier = CASE
              WHEN EXISTS (
                SELECT 1
                FROM token_radar_current_rows AS radar
                WHERE radar.venue = 'all'
                  AND radar.target_type_key = 'Asset'
                  AND radar.identity_id = queue.target_id
              ) THEN 'hot'
              WHEN EXISTS (
                SELECT 1
                FROM token_profile_current AS profile
                WHERE profile.target_type = 'Asset'
                  AND profile.target_id = queue.target_id
              ) THEN 'warm'
              ELSE 'cold'
            END,
            priority = CASE
              WHEN EXISTS (
                SELECT 1
                FROM token_radar_current_rows AS radar
                WHERE radar.venue = 'all'
                  AND radar.target_type_key = 'Asset'
                  AND radar.identity_id = queue.target_id
              ) THEN 20
              WHEN EXISTS (
                SELECT 1
                FROM token_profile_current AS profile
                WHERE profile.target_type = 'Asset'
                  AND profile.target_id = queue.target_id
              ) THEN 60
              ELSE 100
            END;

        DROP INDEX idx_asset_profile_refresh_targets_due;
        CREATE INDEX idx_asset_profile_refresh_targets_due
          ON asset_profile_refresh_targets(
            provider, priority, due_at_ms, updated_at_ms,
            target_type, target_id, heat_tier
          )
          WHERE terminal_reason IS NULL;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260730_0220 is an irreversible asset-profile queue hard cut")
