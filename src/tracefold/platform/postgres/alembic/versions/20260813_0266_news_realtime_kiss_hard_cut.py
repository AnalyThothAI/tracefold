"""Hard-cut News to isolated WSS, auditable recovery, and direct Story outbox.

Revision ID: 20260813_0266
Revises: 20260813_0265
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0266"
down_revision = "20260813_0265"
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
            RAISE EXCEPTION 'news_realtime_hard_cut_workers_active'
              USING ERRCODE = '55006';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        r"""
        CREATE TABLE news_opennews_incidents (
          incident_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          source_id text NOT NULL REFERENCES news_sources(source_id)
            ON DELETE RESTRICT,
          cause_class text NOT NULL,
          opened_at_ms bigint NOT NULL,
          reconnected_at_ms bigint,
          closed_at_ms bigint,
          planned boolean NOT NULL DEFAULT false,
          close_code integer,
          recovery_status text NOT NULL DEFAULT 'pending',
          recovery_from_at_ms bigint,
          recovery_to_at_ms bigint,
          recovered_count integer NOT NULL DEFAULT 0,
          last_error_code text,
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT news_opennews_incidents_cause_check CHECK (
            cause_class IN (
              'planned_shutdown', 'network_connect', 'authentication',
              'provider_close', 'protocol_error', 'idle_timeout',
              'database_backpressure', 'buffer_overflow', 'process_outage',
              'legacy_unknown', 'unknown'
            )
          ),
          CONSTRAINT news_opennews_incidents_clocks_check CHECK (
            opened_at_ms >= 0
            AND (reconnected_at_ms IS NULL OR reconnected_at_ms >= opened_at_ms)
            AND (closed_at_ms IS NULL OR closed_at_ms >= opened_at_ms)
            AND created_at_ms >= 0
            AND updated_at_ms >= 0
          ),
          CONSTRAINT news_opennews_incidents_close_code_check CHECK (
            close_code IS NULL OR close_code BETWEEN 0 AND 65535
          ),
          CONSTRAINT news_opennews_incidents_recovery_check CHECK (
            recovery_status IN (
              'pending', 'running', 'recovered', 'partial',
              'unavailable', 'not_required'
            )
            AND (recovery_from_at_ms IS NULL OR recovery_from_at_ms >= 0)
            AND (recovery_to_at_ms IS NULL OR recovery_to_at_ms >= 0)
            AND (
              recovery_from_at_ms IS NULL OR recovery_to_at_ms IS NULL
              OR recovery_to_at_ms >= recovery_from_at_ms
            )
            AND recovered_count >= 0
          ),
          CONSTRAINT news_opennews_incidents_error_check CHECK (
            last_error_code IS NULL
            OR last_error_code ~ '^[a-z0-9_]{1,120}$'
          )
        );

        CREATE UNIQUE INDEX ux_news_opennews_incidents_active
          ON news_opennews_incidents(source_id)
          WHERE closed_at_ms IS NULL AND planned = false;
        CREATE INDEX ix_news_opennews_incidents_recent
          ON news_opennews_incidents(opened_at_ms DESC, incident_id DESC);
        CREATE INDEX ix_news_opennews_incidents_recovery
          ON news_opennews_incidents(recovery_status, opened_at_ms, incident_id)
          WHERE planned = false AND recovery_status IN ('pending', 'partial', 'unavailable');

        WITH legacy AS (
          SELECT source_id,
                 coverage_unknown_since_at_ms AS opened_at_ms,
                 live_connected,
                 last_connected_at_ms,
                 last_error
            FROM news_sources
           WHERE source_kind = 'opennews'
             AND coverage_unknown_since_at_ms IS NOT NULL
        )
        INSERT INTO news_opennews_incidents (
          source_id, cause_class, opened_at_ms, reconnected_at_ms,
          closed_at_ms, planned, recovery_status, recovery_from_at_ms,
          recovery_to_at_ms, recovered_count, last_error_code,
          created_at_ms, updated_at_ms
        )
        SELECT source_id, 'legacy_unknown', opened_at_ms,
               CASE WHEN live_connected THEN last_connected_at_ms END,
               CASE WHEN live_connected THEN last_connected_at_ms END,
               false, 'unavailable', opened_at_ms,
               CASE WHEN live_connected THEN last_connected_at_ms END,
               0, 'legacy_coverage_unknown', opened_at_ms,
               coalesce(last_connected_at_ms, opened_at_ms)
          FROM legacy;

        ALTER TABLE news_sources
          DROP CONSTRAINT news_sources_strategy_clocks_check,
          DROP CONSTRAINT news_sources_strategy_coverage_check,
          DROP COLUMN last_overflow_at_ms,
          DROP COLUMN strategy_coverage_started_at_ms,
          DROP COLUMN coverage_unknown_since_at_ms,
          ADD COLUMN strategy_history_status text NOT NULL DEFAULT 'unknown',
          ADD COLUMN last_history_check_at_ms bigint;

        ALTER TABLE news_sources
          ADD CONSTRAINT news_sources_strategy_clocks_check CHECK (
            (last_connected_at_ms IS NULL OR last_connected_at_ms >= 0)
            AND (last_disconnected_at_ms IS NULL OR last_disconnected_at_ms >= 0)
            AND (
              last_accepted_strategy_trigger_at_ms IS NULL
              OR last_accepted_strategy_trigger_at_ms >= 0
            )
          ),
          ADD CONSTRAINT news_sources_strategy_history_check CHECK (
            strategy_history_status IN ('unknown', 'available', 'unavailable', 'partial')
            AND (last_history_check_at_ms IS NULL OR last_history_check_at_ms >= 0)
          );

        ALTER TABLE news_items
          ADD COLUMN first_ingest_mode text;
        UPDATE news_items item
           SET first_ingest_mode = 'live'
          FROM news_sources source
         WHERE source.source_id = item.source_id
           AND source.source_kind = 'opennews';
        ALTER TABLE news_items
          ADD CONSTRAINT news_items_first_ingest_mode_check CHECK (
            first_ingest_mode IS NULL OR first_ingest_mode IN ('live', 'recovery')
          ),
          ADD CONSTRAINT news_items_opennews_ingest_mode_check CHECK (
            source_id <> 'news-opennews' OR first_ingest_mode IS NOT NULL
          );

        DROP INDEX IF EXISTS ix_news_items_member_provider_score;
        ALTER TABLE news_items
          DROP CONSTRAINT news_items_provider_score_updated_at_ms_check,
          DROP CONSTRAINT news_items_push_eligibility_updated_at_ms_check,
          DROP COLUMN provider_score_updated_at_ms,
          DROP COLUMN push_eligibility_updated_at_ms;

        DROP INDEX IF EXISTS ix_news_push_deliveries_oldest_waiting;
        DROP INDEX IF EXISTS ix_news_push_deliveries_completed_at;
        ALTER TABLE news_push_deliveries
          DROP CONSTRAINT news_push_deliveries_provider_score_check,
          DROP CONSTRAINT news_push_deliveries_threshold_observed_at_ms_check,
          DROP COLUMN provider_score,
          DROP COLUMN threshold_observed_at_ms,
          ADD COLUMN live_observed_at_ms bigint;
        UPDATE news_push_deliveries
           SET live_observed_at_ms = created_at_ms;
        ALTER TABLE news_push_deliveries
          ALTER COLUMN live_observed_at_ms SET NOT NULL,
          ADD CONSTRAINT news_push_deliveries_live_observed_at_ms_check
            CHECK (live_observed_at_ms >= 0);

        ALTER TABLE news_push_state
          DROP CONSTRAINT news_push_state_reconcile_cycle_cursor_check,
          DROP CONSTRAINT news_push_state_reconcile_cycle_started_at_ms_check,
          DROP CONSTRAINT news_push_state_baseline_at_ms_check,
          DROP COLUMN reconcile_cursor_story_id,
          DROP COLUMN reconcile_cycle_started_at_ms;
        ALTER TABLE news_push_state
          RENAME COLUMN baseline_at_ms TO enablement_epoch_at_ms;
        ALTER TABLE news_push_state
          ADD COLUMN enabled boolean NOT NULL DEFAULT false,
          ADD CONSTRAINT news_push_state_enablement_epoch_check CHECK (
            enablement_epoch_at_ms IS NULL OR enablement_epoch_at_ms >= 0
          ),
          ADD CONSTRAINT news_push_state_enabled_epoch_check CHECK (
            NOT enabled OR enablement_epoch_at_ms IS NOT NULL
          );

        WITH cutover AS (
          SELECT (extract(epoch FROM transaction_timestamp()) * 1000)::bigint AS at_ms
        )
        UPDATE news_push_deliveries delivery
           SET status = 'terminal',
               next_attempt_at_ms = NULL,
               lease_owner = NULL,
               lease_token = NULL,
               lease_expires_at_ms = NULL,
               last_error = 'news_push_legacy_policy_retired',
               updated_at_ms = greatest(delivery.updated_at_ms, cutover.at_ms)
          FROM cutover
         WHERE delivery.status IN (
           'suppressed', 'pending_translation', 'pending_delivery', 'retry_wait'
         );

        WITH aggregate AS (
          SELECT count(*) AS total_count,
                 count(*) FILTER (WHERE status = 'suppressed') AS suppressed_count,
                 count(*) FILTER (
                   WHERE status IN ('pending_translation', 'pending_delivery')
                 ) AS pending_count,
                 count(*) FILTER (WHERE status = 'retry_wait') AS retry_count,
                 count(*) FILTER (WHERE status = 'sent') AS sent_count,
                 count(*) FILTER (WHERE status = 'terminal') AS terminal_count
            FROM news_push_deliveries
        ), cutover AS (
          SELECT (extract(epoch FROM transaction_timestamp()) * 1000)::bigint AS at_ms
        )
        UPDATE news_push_state state
           SET enabled = false,
               enablement_epoch_at_ms = NULL,
               total_count = aggregate.total_count,
               suppressed_count = aggregate.suppressed_count,
               pending_count = aggregate.pending_count,
               retry_count = aggregate.retry_count,
               sent_count = aggregate.sent_count,
               terminal_count = aggregate.terminal_count,
               updated_at_ms = greatest(state.updated_at_ms, cutover.at_ms)
          FROM aggregate CROSS JOIN cutover
         WHERE state.singleton_key = 'current';

        ALTER TABLE news_opennews_incidents OWNER TO tracefold_owner;
        GRANT SELECT ON news_opennews_incidents TO tracefold_serve;
        GRANT SELECT, INSERT, UPDATE, DELETE ON news_opennews_incidents TO tracefold_workers;
        GRANT USAGE, SELECT ON SEQUENCE news_opennews_incidents_incident_id_seq TO tracefold_workers;

        ANALYZE news_sources;
        ANALYZE news_items;
        ANALYZE news_opennews_incidents;
        ANALYZE news_push_deliveries;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260813_0266 is an irreversible News realtime KISS hard cut")
