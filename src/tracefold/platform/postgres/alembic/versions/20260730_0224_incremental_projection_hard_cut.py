"""Delete obsolete projection queues and add durable provider circuit state."""

from __future__ import annotations

from alembic import op

revision = "20260730_0224"
down_revision = "20260730_0223"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_circuit_state (
          provider text PRIMARY KEY,
          status text NOT NULL,
          consecutive_failures integer NOT NULL DEFAULT 0,
          opened_at_ms bigint,
          next_probe_at_ms bigint,
          last_error text,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT provider_circuit_state_status_check
            CHECK (status IN ('closed', 'open')),
          CONSTRAINT provider_circuit_state_failure_count_check
            CHECK (consecutive_failures >= 0),
          CONSTRAINT provider_circuit_state_open_fields_check
            CHECK (
              (status = 'closed')
              OR (opened_at_ms IS NOT NULL AND next_probe_at_ms IS NOT NULL)
            )
        );

        CREATE INDEX idx_provider_circuit_state_probe
          ON provider_circuit_state(next_probe_at_ms, provider)
          WHERE status = 'open';

        DELETE FROM token_profile_current current
        WHERE NOT EXISTS (
          SELECT 1
          FROM token_radar_current_rows radar
          WHERE radar.projection_version = 'token-radar-v15-provider-neutral'
            AND radar."window" IN ('5m', '1h', '4h', '24h')
            AND radar.venue IN ('all', 'sol', 'eth', 'base', 'bsc', 'cex')
            AND radar.target_type_key = current.target_type
            AND radar.identity_id = current.target_id
        );

        DELETE FROM asset_profile_refresh_targets queue
        WHERE NOT EXISTS (
          SELECT 1
          FROM token_radar_current_rows radar
          WHERE radar.projection_version = 'token-radar-v15-provider-neutral'
            AND radar."window" IN ('5m', '1h', '4h', '24h')
            AND radar.venue IN ('all', 'sol', 'eth', 'base', 'bsc', 'cex')
            AND radar.target_type_key = queue.target_type
            AND radar.identity_id = queue.target_id
        );

        DELETE FROM token_image_source_dirty_targets queue
        WHERE NOT EXISTS (
          SELECT 1
          FROM token_radar_current_rows radar
          WHERE radar.projection_version = 'token-radar-v15-provider-neutral'
            AND radar."window" IN ('5m', '1h', '4h', '24h')
            AND radar.venue IN ('all', 'sol', 'eth', 'base', 'bsc', 'cex')
            AND radar.target_type_key = queue.target_type
            AND radar.identity_id = queue.target_id
        );

        DELETE FROM asset_profiles source
        WHERE NOT EXISTS (
          SELECT 1
          FROM token_radar_current_rows radar
          WHERE radar.projection_version = 'token-radar-v15-provider-neutral'
            AND radar."window" IN ('5m', '1h', '4h', '24h')
            AND radar.venue IN ('all', 'sol', 'eth', 'base', 'bsc', 'cex')
            AND radar.target_type_key = 'Asset'
            AND radar.identity_id = source.asset_id
        );

        DELETE FROM cex_token_profiles source
        WHERE NOT EXISTS (
          SELECT 1
          FROM token_radar_current_rows radar
          WHERE radar.projection_version = 'token-radar-v15-provider-neutral'
            AND radar."window" IN ('5m', '1h', '4h', '24h')
            AND radar.venue IN ('all', 'sol', 'eth', 'base', 'bsc', 'cex')
            AND radar.target_type_key = 'CexToken'
            AND radar.identity_id = source.cex_token_id
        );

        DROP TABLE token_profile_current_dirty_targets;
        DROP TABLE token_radar_dirty_targets;
        DROP TABLE token_radar_rank_source_events;

        ALTER TABLE provider_circuit_state OWNER TO tracefold_owner;
        GRANT SELECT ON provider_circuit_state TO tracefold_serve;
        GRANT SELECT, INSERT, UPDATE, DELETE
          ON provider_circuit_state TO tracefold_workers;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260730_0224 is an irreversible incremental projection hard cut")
