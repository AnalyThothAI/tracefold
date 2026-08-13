"""Hard-cut OpenNews to allowlisted Strategy triggers without replay.

Revision ID: 20260813_0265
Revises: 20260813_0264
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0265"
down_revision = "20260813_0264"
branch_labels = None
depends_on = None

_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS = (0x54524644, 0)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        f"""
        DO $migration$
        BEGIN
          IF NOT pg_try_advisory_xact_lock(
            {_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS[0]},
            {_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS[1]}
          ) THEN
            RAISE EXCEPTION 'strategy_hard_cut_workers_active'
              USING ERRCODE = '55006';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION news_strategy_provenance_valid(value jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        AS $function$
          SELECT CASE jsonb_typeof(value)
            WHEN 'array' THEN NOT EXISTS (
              SELECT 1
                FROM jsonb_array_elements(value) AS strategy(entry)
               WHERE jsonb_typeof(strategy.entry) IS DISTINCT FROM 'object'
                  OR jsonb_typeof(strategy.entry -> 'id') IS DISTINCT FROM 'string'
                  OR btrim(strategy.entry ->> 'id') = ''
                  OR char_length(strategy.entry ->> 'id') > 128
            )
            ELSE false
          END
        $function$;

        ALTER TABLE news_sources
          DROP CONSTRAINT news_sources_wire_control_check;

        ALTER TABLE news_sources
          ADD COLUMN last_connected_at_ms bigint,
          ADD COLUMN last_disconnected_at_ms bigint,
          ADD COLUMN last_overflow_at_ms bigint,
          ADD COLUMN strategy_coverage_started_at_ms bigint,
          ADD COLUMN coverage_unknown_since_at_ms bigint,
          ADD COLUMN last_accepted_strategy_trigger_at_ms bigint,
          ADD COLUMN observed_strategy_provenance jsonb NOT NULL DEFAULT '[]'::jsonb;

        ALTER TABLE news_sources
          DROP COLUMN last_recovery_at_ms,
          DROP COLUMN last_live_at_ms,
          ADD CONSTRAINT news_sources_strategy_clocks_check CHECK (
            (last_connected_at_ms IS NULL OR last_connected_at_ms >= 0)
            AND (last_disconnected_at_ms IS NULL OR last_disconnected_at_ms >= 0)
            AND (last_overflow_at_ms IS NULL OR last_overflow_at_ms >= 0)
            AND (
              strategy_coverage_started_at_ms IS NULL
              OR strategy_coverage_started_at_ms >= 0
            )
            AND (
              coverage_unknown_since_at_ms IS NULL
              OR coverage_unknown_since_at_ms >= 0
            )
            AND (
              last_accepted_strategy_trigger_at_ms IS NULL
              OR last_accepted_strategy_trigger_at_ms >= 0
            )
          ),
          ADD CONSTRAINT news_sources_strategy_provenance_check CHECK (
            news_strategy_provenance_valid(observed_strategy_provenance)
            AND jsonb_array_length(observed_strategy_provenance) <= 32
          );

        WITH cutover AS (
          SELECT (extract(epoch FROM transaction_timestamp()) * 1000)::bigint
                   AS at_ms
        )
        UPDATE news_sources source
           SET live_connected = false,
               last_fetch_started_at_ms = NULL,
               last_fetch_finished_at_ms = NULL,
               last_success_at_ms = NULL,
               last_http_status = NULL,
               consecutive_failures = 0,
               last_error = NULL,
               last_outcome = 'strategy_hard_cut',
               last_rejection_counts = '{}'::jsonb,
               last_items_seen = 0,
               last_items_accepted = 0,
               last_connected_at_ms = NULL,
               last_disconnected_at_ms = NULL,
               last_overflow_at_ms = NULL,
               strategy_coverage_started_at_ms = NULL,
               coverage_unknown_since_at_ms = NULL,
               last_accepted_strategy_trigger_at_ms = NULL,
               observed_strategy_provenance = '[]'::jsonb,
               updated_at_ms = cutover.at_ms
          FROM cutover
         WHERE source.source_kind = 'opennews';

        UPDATE news_sources
           SET enabled = false
         WHERE source_kind = 'opennews'
           AND source_id <> 'news-opennews';

        ALTER TABLE news_sources
          ADD CONSTRAINT news_sources_strategy_coverage_check CHECK (
            source_kind <> 'opennews'
            OR coverage_unknown_since_at_ms IS NULL
            OR (
              strategy_coverage_started_at_ms IS NOT NULL
              AND coverage_unknown_since_at_ms >= strategy_coverage_started_at_ms
            )
          ),
          ADD CONSTRAINT news_sources_wire_control_check CHECK (
            (
              source_kind = 'rss'
              AND feed_url ~ '^https://'
              AND refresh_interval_seconds >= 1
              AND next_fetch_at_ms >= 0
            )
            OR (
              source_kind = 'opennews'
              AND feed_url IS NULL
              AND refresh_interval_seconds IS NULL
              AND etag IS NULL
              AND last_modified IS NULL
              AND next_fetch_at_ms IS NULL
              AND claim_token IS NULL
              AND claim_lease_expires_at_ms IS NULL
              AND last_fetch_started_at_ms IS NULL
              AND last_fetch_finished_at_ms IS NULL
              AND last_http_status IS NULL
            )
          );

        WITH cutover AS (
          SELECT (extract(epoch FROM transaction_timestamp()) * 1000)::bigint
                   AS at_ms
        )
        UPDATE news_items item
           SET active = false,
               updated_at_ms = greatest(item.updated_at_ms, cutover.at_ms)
          FROM cutover, news_sources source
         WHERE source.source_id = item.source_id
           AND source.source_kind = 'opennews'
           AND item.active;

        ALTER TABLE news_items
          ADD CONSTRAINT news_items_active_opennews_strategy_provenance_check CHECK (
            source_id <> 'news-opennews'
            OR NOT active
            OR (
              COALESCE(
                news_strategy_provenance_valid(provider_metadata -> 'strategies'),
                false
              )
              AND jsonb_array_length(provider_metadata -> 'strategies')
                    BETWEEN 1 AND 32
            )
          );

        DELETE FROM news_brief_selection_current;
        DELETE FROM news_story_members;
        DELETE FROM news_stories;

        UPDATE news_projection_summary
           SET active_item_count = 0,
               active_story_count = 0,
               invalid_owner_count = 0,
               invalid_story_aggregate_count = 0,
               newest_item_at_ms = NULL,
               newest_story_at_ms = NULL,
               last_material_change_at_ms = NULL,
               input_fingerprint = NULL,
               projection_version = NULL,
               last_attempt_at_ms = NULL,
               last_error = NULL,
               last_success_at_ms = NULL,
               updated_at_ms = 0
         WHERE singleton_key = 'current';

        UPDATE news_brief_current
           SET slot_at_ms = NULL,
               slot_status = 'due',
               next_due_at_ms = 0,
               completed_at_ms = NULL,
               lease_owner = NULL,
               lease_token = NULL,
               lease_expires_at_ms = NULL,
               attempt_count = 0,
               failure_count = 0,
               model_outcome = NULL,
               pointer_action = 'none',
               last_error_code = NULL,
               last_attempt_at_ms = NULL,
               active_selection = NULL,
               served_payload = NULL,
               created_at_ms = 0,
               updated_at_ms = 0
         WHERE singleton_key = true;

        WITH cutover AS (
          SELECT (extract(epoch FROM transaction_timestamp()) * 1000)::bigint
                   AS at_ms
        )
        UPDATE news_push_deliveries delivery
           SET status = 'suppressed',
               next_attempt_at_ms = NULL,
               lease_owner = NULL,
               lease_token = NULL,
               lease_expires_at_ms = NULL,
               updated_at_ms = greatest(delivery.updated_at_ms, cutover.at_ms)
          FROM cutover
         WHERE delivery.status IN (
           'pending_translation', 'pending_delivery', 'retry_wait'
         );

        WITH aggregate AS (
          SELECT count(*) AS total_count,
                 count(*) FILTER (WHERE status = 'suppressed')
                   AS suppressed_count,
                 count(*) FILTER (
                   WHERE status IN ('pending_translation', 'pending_delivery')
                 ) AS pending_count,
                 count(*) FILTER (WHERE status = 'retry_wait') AS retry_count,
                 count(*) FILTER (WHERE status = 'sent') AS sent_count,
                 count(*) FILTER (WHERE status = 'terminal') AS terminal_count
            FROM news_push_deliveries
        ), cutover AS (
          SELECT (extract(epoch FROM transaction_timestamp()) * 1000)::bigint
                   AS at_ms
        )
        UPDATE news_push_state state
           SET total_count = aggregate.total_count,
               suppressed_count = aggregate.suppressed_count,
               pending_count = aggregate.pending_count,
               retry_count = aggregate.retry_count,
               sent_count = aggregate.sent_count,
               terminal_count = aggregate.terminal_count,
               reconcile_cursor_story_id = NULL,
               reconcile_cycle_started_at_ms = NULL,
               updated_at_ms = greatest(state.updated_at_ms, cutover.at_ms)
          FROM aggregate
          CROSS JOIN cutover
         WHERE state.singleton_key = 'current';

        ANALYZE news_sources;
        ANALYZE news_items;
        ANALYZE news_stories;
        ANALYZE news_push_deliveries;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260813_0265 is an irreversible OpenNews Strategy hard cut")
