"""Hard-cut Story Push to Item Push with best-effort presentation.

Revision ID: 20260814_0270
Revises: 20260814_0269
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0270"
down_revision = "20260814_0269"
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
            RAISE EXCEPTION 'news_item_push_hard_cut_workers_active'
              USING ERRCODE = '55006';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        r"""
        DO $migration$
        DECLARE
          duplicate_item_id text;
        BEGIN
          IF EXISTS (
            SELECT 1
              FROM news_push_deliveries
             WHERE selected_item_id IS NULL
                OR btrim(selected_item_id) = ''
          ) THEN
            RAISE EXCEPTION 'news_item_push_legacy_selected_item_missing'
              USING ERRCODE = '23514';
          END IF;

          SELECT selected_item_id
            INTO duplicate_item_id
            FROM news_push_deliveries
           GROUP BY selected_item_id
          HAVING count(*) > 1
           ORDER BY selected_item_id
           LIMIT 1;

          IF duplicate_item_id IS NOT NULL THEN
            RAISE EXCEPTION
              'news_item_push_legacy_selected_item_duplicate:%',
              duplicate_item_id
              USING ERRCODE = '23505';
          END IF;
        END
        $migration$;

        WITH cutover AS (
          SELECT (extract(epoch FROM transaction_timestamp()) * 1000)::bigint
                   AS at_ms
        )
        UPDATE news_push_deliveries delivery
           SET status = 'terminal',
               next_attempt_at_ms = NULL,
               lease_owner = NULL,
               lease_token = NULL,
               lease_expires_at_ms = NULL,
               last_error = 'news_item_push_legacy_policy_retired',
               updated_at_ms = greatest(delivery.updated_at_ms, cutover.at_ms)
          FROM cutover
         WHERE delivery.status NOT IN ('sent', 'terminal');

        DROP INDEX IF EXISTS ix_news_push_deliveries_due;
        DROP INDEX IF EXISTS idx_news_push_deliveries_selected_item;
        DROP INDEX IF EXISTS ix_news_push_deliveries_oldest_waiting;
        DROP INDEX IF EXISTS ix_news_push_deliveries_translation_attempted;
        DROP INDEX IF EXISTS ix_news_push_deliveries_completed_at;

        ALTER TABLE news_push_deliveries
          DROP CONSTRAINT news_push_deliveries_pkey,
          DROP CONSTRAINT news_push_deliveries_story_id_check,
          DROP CONSTRAINT news_push_deliveries_selected_item_id_check,
          DROP CONSTRAINT news_push_deliveries_delivery_payload_check,
          DROP CONSTRAINT news_push_deliveries_payload_fingerprint_check,
          DROP CONSTRAINT news_push_deliveries_translation_status_check,
          DROP CONSTRAINT news_push_deliveries_status_check,
          DROP CONSTRAINT news_push_deliveries_attempts_check,
          DROP CONSTRAINT news_push_deliveries_next_attempt_at_ms_check,
          DROP CONSTRAINT news_push_deliveries_lease_check,
          DROP CONSTRAINT news_push_deliveries_translation_prompt_version_check,
          DROP CONSTRAINT news_push_deliveries_translation_attempted_at_ms_check,
          DROP CONSTRAINT news_push_deliveries_translation_duration_ms_check,
          DROP CONSTRAINT news_push_deliveries_translation_fallback_code_check;

        ALTER TABLE news_push_deliveries
          RENAME COLUMN selected_item_id TO item_id;
        ALTER TABLE news_push_deliveries
          RENAME COLUMN delivery_payload TO legacy_delivery_payload;

        ALTER TABLE news_push_deliveries
          DROP COLUMN story_id,
          DROP COLUMN payload_fingerprint,
          DROP COLUMN translation_status,
          DROP COLUMN delivery_attempts,
          DROP COLUMN next_attempt_at_ms,
          DROP COLUMN lease_owner,
          DROP COLUMN lease_token,
          DROP COLUMN lease_expires_at_ms,
          DROP COLUMN translation_prompt_version,
          DROP COLUMN translation_attempted_at_ms,
          DROP COLUMN translation_duration_ms,
          DROP COLUMN translation_fallback_code,
          ADD COLUMN presentation_snapshot jsonb,
          ADD COLUMN attempted_at_ms bigint;

        ALTER TABLE news_push_deliveries
          ADD CONSTRAINT news_push_deliveries_pkey PRIMARY KEY (item_id),
          ADD CONSTRAINT news_push_deliveries_item_id_check CHECK (
            item_id ~ '^news_item_[0-9a-f]{32}$'
          ),
          ADD CONSTRAINT news_push_deliveries_legacy_payload_check CHECK (
            legacy_delivery_payload IS NULL
            OR jsonb_typeof(legacy_delivery_payload) = 'object'
          ),
          ADD CONSTRAINT news_push_deliveries_presentation_snapshot_check CHECK (
            presentation_snapshot IS NULL
            OR jsonb_typeof(presentation_snapshot) = 'object'
          ),
          ADD CONSTRAINT news_push_deliveries_attempted_at_ms_check CHECK (
            attempted_at_ms IS NULL OR attempted_at_ms >= 0
          ),
          ADD CONSTRAINT news_push_deliveries_status_check CHECK (
            status IN ('pending', 'sending', 'sent', 'terminal')
          ),
          ADD CONSTRAINT news_push_deliveries_current_source_check CHECK (
            source_payload ->> 'schema_version'
              IS DISTINCT FROM 'news_item_push_v1'
            OR (
              source_payload ?& ARRAY[
                'schema_version',
                'item_id',
                'provider_event_id',
                'live_observed_at_ms',
                'original_title',
                'reporting_origin',
                'provider_published_at_ms',
                'strategy_labels',
                'assets'
              ]
              AND source_payload - ARRAY[
                'schema_version',
                'item_id',
                'provider_event_id',
                'live_observed_at_ms',
                'original_title',
                'reporting_origin',
                'provider_published_at_ms',
                'source_url',
                'strategy_labels',
                'assets',
                'score',
                'signal',
                'grade'
              ] = '{}'::jsonb
              AND source_payload ->> 'item_id' = item_id
              AND jsonb_typeof(source_payload -> 'live_observed_at_ms') = 'number'
              AND jsonb_typeof(source_payload -> 'provider_published_at_ms') = 'number'
              AND jsonb_typeof(source_payload -> 'strategy_labels') = 'array'
              AND jsonb_typeof(source_payload -> 'assets') = 'array'
              AND jsonb_array_length(source_payload -> 'strategy_labels') <= 32
              AND jsonb_array_length(source_payload -> 'assets') <= 32
            )
          ),
          ADD CONSTRAINT news_push_deliveries_current_state_check CHECK (
            source_payload ->> 'schema_version'
              IS DISTINCT FROM 'news_item_push_v1'
            OR (
              legacy_delivery_payload IS NULL
              AND (
                (
                  status = 'pending'
                  AND presentation_snapshot IS NULL
                  AND attempted_at_ms IS NULL
                  AND receipt IS NULL
                  AND last_error IS NULL
                  AND sent_at_ms IS NULL
                )
                OR (
                  status = 'sending'
                  AND presentation_snapshot IS NOT NULL
                  AND attempted_at_ms IS NOT NULL
                  AND receipt IS NULL
                  AND last_error IS NULL
                  AND sent_at_ms IS NULL
                )
                OR (
                  status = 'sent'
                  AND presentation_snapshot IS NOT NULL
                  AND attempted_at_ms IS NOT NULL
                  AND receipt IS NOT NULL
                  AND last_error IS NULL
                  AND sent_at_ms IS NOT NULL
                )
                OR (
                  status = 'terminal'
                  AND presentation_snapshot IS NOT NULL
                  AND attempted_at_ms IS NOT NULL
                  AND receipt IS NULL
                  AND last_error ~ '^[a-z0-9_]{1,120}$'
                  AND sent_at_ms IS NULL
                )
              )
            )
          );

        ALTER TABLE news_push_state
          DROP CONSTRAINT news_push_state_delivery_counts_check,
          DROP CONSTRAINT news_push_state_enabled_epoch_check;
        ALTER TABLE news_push_state
          RENAME COLUMN enabled TO delivery_available;
        ALTER TABLE news_push_state
          ADD COLUMN sending_count bigint NOT NULL DEFAULT 0;

        UPDATE news_push_state
           SET delivery_available = false,
               enablement_epoch_at_ms = NULL,
               total_count = 0,
               pending_count = 0,
               sending_count = 0,
               sent_count = 0,
               terminal_count = 0,
               latest_sent_at_ms = NULL,
               latest_error = NULL,
               latest_error_at_ms = NULL,
               updated_at_ms = greatest(
                 updated_at_ms,
                 (extract(epoch FROM transaction_timestamp()) * 1000)::bigint
               )
         WHERE singleton_key = 'current';

        ALTER TABLE news_push_state
          DROP COLUMN suppressed_count,
          DROP COLUMN retry_count,
          ADD CONSTRAINT news_push_state_delivery_counts_check CHECK (
            total_count >= 0
            AND pending_count >= 0
            AND sending_count >= 0
            AND sent_count >= 0
            AND terminal_count >= 0
            AND total_count = pending_count + sending_count
                              + sent_count + terminal_count
          ),
          ADD CONSTRAINT news_push_state_delivery_availability_epoch_check CHECK (
            NOT delivery_available OR enablement_epoch_at_ms IS NOT NULL
          );

        CREATE INDEX ix_news_push_deliveries_pending
          ON news_push_deliveries(live_observed_at_ms, item_id)
          WHERE status = 'pending'
            AND source_payload ->> 'schema_version' = 'news_item_push_v1';

        CREATE INDEX ix_news_push_deliveries_translation_attempted
          ON news_push_deliveries(attempted_at_ms, item_id)
          INCLUDE (presentation_snapshot, status)
          WHERE attempted_at_ms IS NOT NULL
            AND source_payload ->> 'schema_version' = 'news_item_push_v1';

        CREATE INDEX ix_news_push_deliveries_completed
          ON news_push_deliveries(
            (CASE WHEN status = 'sent' THEN sent_at_ms ELSE updated_at_ms END),
            item_id
          )
          INCLUDE (
            status,
            sent_at_ms,
            updated_at_ms,
            live_observed_at_ms,
            presentation_snapshot
          )
          WHERE status IN ('sent', 'terminal')
            AND source_payload ->> 'schema_version' = 'news_item_push_v1';

        ANALYZE news_push_state;
        ANALYZE news_push_deliveries;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260814_0270 is an irreversible News Item Push hard cut")
