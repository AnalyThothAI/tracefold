"""Split Radar target features from coalesced rank-set publication."""

from __future__ import annotations

from alembic import op

revision = "20260731_0229"
down_revision = "20260731_0228"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE radar_projection_frontiers
          ADD COLUMN pending_first_dirty_at_ms bigint,
          ADD COLUMN pending_deadline_at_ms bigint,
          ADD COLUMN pending_input_fingerprint text,
          ADD COLUMN pending_projection_version text,
          ADD CONSTRAINT radar_projection_frontiers_pending_check
            CHECK (
              (
                pending_first_dirty_at_ms IS NULL
                AND pending_deadline_at_ms IS NULL
                AND pending_input_fingerprint IS NULL
                AND pending_projection_version IS NULL
              )
              OR (
                status = 'running'
                AND pending_first_dirty_at_ms IS NOT NULL
                AND pending_deadline_at_ms IS NOT NULL
                AND pending_input_fingerprint IS NOT NULL
                AND pending_projection_version IS NOT NULL
              )
            );

        DELETE FROM radar_projection_frontiers
        WHERE target_type <> 'RankSet'
          AND venue <> 'all';

        WITH clock AS (
          SELECT (extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms
        ),
        token_sets AS (
          SELECT DISTINCT "window" AS window_key, venue
          FROM token_radar_publication_state
          WHERE projection_version = 'token-radar-v15-provider-neutral'
        )
        INSERT INTO radar_projection_frontiers(
          target_type, target_id, window_key, venue, status,
          first_dirty_at_ms, deadline_at_ms, next_attempt_at_ms,
          attempt_count, transient_failure_count, input_fingerprint,
          projection_version, claimed_by, claimed_until_ms,
          last_error_code, updated_at_ms
        )
        SELECT
          'RankSet', 'token', token_sets.window_key, token_sets.venue,
          'dirty', clock.now_ms, clock.now_ms, NULL, 0, 0,
          'migration:20260731_0229:token:'
            || token_sets.window_key || ':' || token_sets.venue,
          'token-radar-v15-provider-neutral',
          NULL, NULL, NULL, clock.now_ms
        FROM token_sets
        CROSS JOIN clock
        ON CONFLICT(target_type, target_id, window_key, venue)
        DO NOTHING;

        WITH clock AS (
          SELECT (extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms
        )
        INSERT INTO radar_projection_frontiers(
          target_type, target_id, window_key, venue, status,
          first_dirty_at_ms, deadline_at_ms, next_attempt_at_ms,
          attempt_count, transient_failure_count, input_fingerprint,
          projection_version, claimed_by, claimed_until_ms,
          last_error_code, updated_at_ms
        )
        SELECT
          'RankSet', 'stocks', publication.window_key, 'all',
          'dirty', clock.now_ms, clock.now_ms, NULL, 0, 0,
          'migration:20260731_0229:stocks:' || publication.window_key,
          'token-radar-v15-provider-neutral', NULL, NULL, NULL, clock.now_ms
        FROM stocks_radar_publication_state publication
        CROSS JOIN clock
        ON CONFLICT(target_type, target_id, window_key, venue)
        DO NOTHING;
        """
    )


def downgrade() -> None:
    raise NotImplementedError("hard cut migration")
