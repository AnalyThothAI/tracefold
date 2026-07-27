"""Hard-cut Watchlist, Notifications, and scoped Token Radar state."""

from __future__ import annotations

from alembic import op

revision = "20260727_0206"
down_revision = "20260727_0205"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30min'")
    _drop_notification_state()
    _drop_watchlist_fact_annotations()
    _hard_cut_token_radar_scope()


def _drop_notification_state() -> None:
    op.execute("DROP TABLE notification_deliveries")
    op.execute("DROP TABLE notification_reads")
    op.execute("DROP TABLE notifications")
    op.execute("DROP TABLE account_token_alerts")


def _drop_watchlist_fact_annotations() -> None:
    op.execute(
        """
        ALTER TABLE events
          DROP COLUMN matched_handles_json,
          DROP COLUMN is_watched,
          DROP COLUMN matched_at_ms
        """
    )
    op.execute("ALTER TABLE event_entities DROP COLUMN is_watched")


def _hard_cut_token_radar_scope() -> None:
    # These rows are deterministic read models. Rebuild them from retained
    # PostgreSQL facts instead of carrying old scoped/watched semantics forward.
    _enqueue_token_radar_rebuild_targets()
    op.execute("DELETE FROM token_radar_current_rows")
    op.execute("DELETE FROM token_radar_target_features")
    op.execute("DELETE FROM token_radar_publication_state")
    op.execute("DELETE FROM token_radar_target_first_seen")
    op.execute("DELETE FROM token_radar_rank_source_events")

    _drop_scope_constraints_and_indexes()

    op.execute(
        """
        ALTER TABLE token_radar_target_features
          DROP COLUMN scope,
          DROP COLUMN social_heat_watched_mentions
        """
    )
    op.execute(
        """
        ALTER TABLE token_radar_target_features
          RENAME COLUMN cohort_public_followup_authors TO cohort_followup_authors
        """
    )
    op.execute("ALTER TABLE token_radar_current_rows DROP COLUMN scope")
    op.execute("ALTER TABLE token_radar_publication_state DROP COLUMN scope")
    op.execute("ALTER TABLE token_radar_target_first_seen DROP COLUMN scope")
    op.execute("ALTER TABLE token_radar_rank_source_events DROP COLUMN is_watched")

    op.execute(
        """
        ALTER TABLE token_radar_target_features
          ADD PRIMARY KEY (
            projection_version,
            "window",
            lane,
            target_type_key,
            identity_id
          )
        """
    )
    op.execute(
        """
        ALTER TABLE token_radar_publication_state
          ADD PRIMARY KEY (projection_version, "window", venue)
        """
    )
    op.execute(
        """
        ALTER TABLE token_radar_target_first_seen
          ADD PRIMARY KEY (
            projection_version,
            "window",
            venue,
            target_type_key,
            identity_id
          )
        """
    )

    _create_token_radar_indexes()


def _enqueue_token_radar_rebuild_targets() -> None:
    op.execute(
        """
        WITH migration_clock AS (
          SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms
        ), rebuild_targets AS MATERIALIZED (
          SELECT target_type_key, identity_id
          FROM token_radar_target_features
          WHERE btrim(target_type_key) <> ''
            AND btrim(identity_id) <> ''
          UNION
          SELECT target_type_key, identity_id
          FROM token_radar_current_rows
          WHERE btrim(target_type_key) <> ''
            AND btrim(identity_id) <> ''
          UNION
          SELECT target_type_key, identity_id
          FROM token_radar_target_first_seen
          WHERE btrim(target_type_key) <> ''
            AND btrim(identity_id) <> ''
          UNION
          SELECT target_type_key, identity_id
          FROM token_radar_rank_source_events
          WHERE btrim(target_type_key) <> ''
            AND btrim(identity_id) <> ''
        )
        INSERT INTO token_radar_dirty_targets(
          target_type_key,
          identity_id,
          dirty_reason,
          market_dirty,
          repair_dirty,
          payload_hash,
          due_at_ms,
          leased_until_ms,
          lease_owner,
          attempt_count,
          last_error,
          first_dirty_at_ms,
          updated_at_ms
        )
        SELECT
          rebuild_targets.target_type_key,
          rebuild_targets.identity_id,
          'schema_hard_cut_0206',
          false,
          true,
          'schema-hard-cut-0206:' || md5(
            rebuild_targets.target_type_key || ':' || rebuild_targets.identity_id
          ),
          migration_clock.now_ms,
          NULL,
          NULL,
          0,
          NULL,
          migration_clock.now_ms,
          migration_clock.now_ms
        FROM rebuild_targets
        CROSS JOIN migration_clock
        ON CONFLICT(target_type_key, identity_id) DO UPDATE SET
          dirty_reason = CASE
            WHEN token_radar_dirty_targets.dirty_reason = EXCLUDED.dirty_reason
              THEN token_radar_dirty_targets.dirty_reason
            ELSE 'mixed'
          END,
          market_dirty = token_radar_dirty_targets.market_dirty,
          repair_dirty = true,
          payload_hash = EXCLUDED.payload_hash,
          due_at_ms = LEAST(token_radar_dirty_targets.due_at_ms, EXCLUDED.due_at_ms),
          leased_until_ms = NULL,
          lease_owner = NULL,
          attempt_count = 0,
          last_error = NULL,
          first_dirty_at_ms = LEAST(
            token_radar_dirty_targets.first_dirty_at_ms,
            EXCLUDED.first_dirty_at_ms
          ),
          updated_at_ms = EXCLUDED.updated_at_ms
        """
    )


def _drop_scope_constraints_and_indexes() -> None:
    op.execute(
        """
        DO $$
        DECLARE
          target_table TEXT;
          constraint_name TEXT;
          index_name TEXT;
        BEGIN
          FOREACH target_table IN ARRAY ARRAY[
            'token_radar_target_features',
            'token_radar_current_rows',
            'token_radar_publication_state',
            'token_radar_target_first_seen'
          ]
          LOOP
            FOR constraint_name IN
              SELECT conname
              FROM pg_constraint
              WHERE conrelid = format('public.%I', target_table)::regclass
                AND contype <> 'n'
                AND pg_get_constraintdef(oid) ~ '\\mscope\\M'
            LOOP
              EXECUTE format(
                'ALTER TABLE %I DROP CONSTRAINT %I',
                target_table,
                constraint_name
              );
            END LOOP;

            FOR index_name IN
              SELECT indexname
              FROM pg_indexes
              WHERE schemaname = 'public'
                AND tablename = target_table
                AND indexdef ~ '\\mscope\\M'
            LOOP
              EXECUTE format('DROP INDEX IF EXISTS %I', index_name);
            END LOOP;
          END LOOP;
        END $$;
        """
    )


def _create_token_radar_indexes() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_token_radar_target_features_rank
          ON token_radar_target_features(
            projection_version,
            "window",
            lane DESC,
            rank_score DESC,
            latest_event_received_at_ms DESC,
            identity_id ASC
          )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_token_radar_target_features_freshness
          ON token_radar_target_features(
            projection_version,
            target_type_key,
            identity_id,
            latest_market_observed_at_ms DESC
          )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_token_radar_target_features_window_freshness
          ON token_radar_target_features(
            projection_version,
            "window",
            latest_event_received_at_ms DESC
          )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_token_radar_current_rows_venue_rank
          ON token_radar_current_rows(
            projection_version,
            "window",
            venue,
            lane,
            rank
          )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_token_radar_current_rows_venue_target
          ON token_radar_current_rows(
            projection_version,
            "window",
            venue,
            lane,
            target_type_key,
            identity_id
          )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_token_radar_current_rows_generation
          ON token_radar_current_rows(
            projection_version,
            "window",
            venue,
            generation_id,
            lane,
            rank
          )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_token_radar_current_rows_target
          ON token_radar_current_rows(target_type, target_id, computed_at_ms DESC)
          WHERE target_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_token_radar_publication_state_current
          ON token_radar_publication_state(
            projection_version,
            "window",
            venue,
            latest_attempt_status,
            current_generation_id
          )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_token_radar_first_seen_updated
          ON token_radar_target_first_seen(updated_at_ms DESC)
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260727_0206 is an irreversible destructive hard-cut migration; "
        "restore a pre-migration backup instead of downgrading."
    )
