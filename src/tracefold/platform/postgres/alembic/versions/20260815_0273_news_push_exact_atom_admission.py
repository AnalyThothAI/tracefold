"""Hard-cut Item Push to shared exact-atom admission.

Revision ID: 20260815_0273
Revises: 20260815_0272
"""

from __future__ import annotations

from alembic import op

revision = "20260815_0273"
down_revision = "20260815_0272"
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
            RAISE EXCEPTION 'news_push_exact_atom_hard_cut_workers_active'
              USING ERRCODE = '55006';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        r"""
        DROP INDEX IF EXISTS ix_news_push_deliveries_pending;
        DROP INDEX IF EXISTS ix_news_push_deliveries_attempted;
        DROP INDEX IF EXISTS ix_news_push_deliveries_completed;

        ALTER TABLE news_push_deliveries
          DROP CONSTRAINT news_push_deliveries_status_check,
          DROP CONSTRAINT news_push_deliveries_title_fingerprint_check,
          DROP CONSTRAINT news_push_deliveries_current_state_check;

        ALTER TABLE news_push_deliveries
          ADD COLUMN notification_fingerprint text,
          ADD COLUMN comparison_identity_version text,
          ADD COLUMN admission_policy_version text,
          ADD COLUMN adjudicated_at_ms bigint,
          ADD COLUMN admission_reason text,
          ADD COLUMN suppressed_by_item_id text;

        WITH cutover AS (
          SELECT (extract(epoch FROM transaction_timestamp()) * 1000)::bigint AS at_ms
        )
        UPDATE news_push_deliveries delivery
           SET status = 'terminal',
               last_error = 'news_push_exact_atom_policy_retired',
               attempted_at_ms = coalesce(delivery.attempted_at_ms, cutover.at_ms),
               updated_at_ms = greatest(delivery.updated_at_ms, cutover.at_ms)
          FROM cutover
         WHERE delivery.source_payload ->> 'schema_version' = 'news_item_push_v1'
           AND delivery.status NOT IN ('sent', 'terminal');

        ALTER TABLE news_push_deliveries
          ADD CONSTRAINT news_push_deliveries_status_check CHECK (
            status IN ('pending', 'sending', 'sent', 'terminal', 'suppressed')
          ),
          ADD CONSTRAINT news_push_deliveries_source_check CHECK (
            source_payload ->> 'schema_version' IS DISTINCT FROM 'news_item_push_v2'
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
          ADD CONSTRAINT news_push_deliveries_admission_check CHECK (
            (
              source_payload ->> 'schema_version' IS DISTINCT FROM 'news_item_push_v2'
              AND status IN ('sent', 'terminal')
              AND notification_fingerprint IS NULL
              AND comparison_identity_version IS NULL
              AND admission_policy_version IS NULL
              AND adjudicated_at_ms IS NULL
              AND admission_reason IS NULL
              AND suppressed_by_item_id IS NULL
            )
            OR (
              source_payload ->> 'schema_version' = 'news_item_push_v2'
              AND legacy_delivery_payload IS NULL
              AND legacy_presentation_snapshot IS NULL
              AND source_title_fingerprint ~ '^[0-9a-f]{64}$'
              AND source_title_fingerprint = encode(
                sha256(convert_to(source_payload ->> 'original_title', 'UTF8')),
                'hex'
              )
              AND notification_fingerprint ~ '^[0-9a-f]{64}$'
              AND comparison_identity_version ~ '^[a-z0-9_]{1,120}$'
              AND admission_policy_version ~ '^[a-z0-9_]{1,120}$'
              AND adjudicated_at_ms >= 0
              AND (
                (
                  status = 'suppressed'
                  AND admission_reason = 'exact_atom_suppressed'
                  AND suppressed_by_item_id IS NOT NULL
                  AND suppressed_by_item_id <> item_id
                  AND attempted_at_ms IS NULL
                  AND receipt IS NULL
                  AND last_error IS NULL
                  AND sent_at_ms IS NULL
                )
                OR (
                  status IN ('pending', 'sending', 'sent', 'terminal')
                  AND admission_reason = 'exact_atom_leader'
                  AND suppressed_by_item_id IS NULL
                  AND (
                    (
                      status = 'pending'
                      AND attempted_at_ms IS NULL
                      AND receipt IS NULL
                      AND last_error IS NULL
                      AND sent_at_ms IS NULL
                    )
                    OR (
                      status = 'sending'
                      AND attempted_at_ms IS NOT NULL
                      AND receipt IS NULL
                      AND last_error IS NULL
                      AND sent_at_ms IS NULL
                    )
                    OR (
                      status = 'sent'
                      AND attempted_at_ms IS NOT NULL
                      AND receipt IS NOT NULL
                      AND last_error IS NULL
                      AND sent_at_ms IS NOT NULL
                    )
                    OR (
                      status = 'terminal'
                      AND attempted_at_ms IS NOT NULL
                      AND receipt IS NULL
                      AND last_error ~ '^[a-z0-9_]{1,120}$'
                      AND sent_at_ms IS NULL
                    )
                  )
                )
              )
            )
          ),
          ADD CONSTRAINT news_push_deliveries_admission_identity_key UNIQUE (
            item_id,
            notification_fingerprint,
            comparison_identity_version,
            admission_policy_version
          ),
          ADD CONSTRAINT news_push_deliveries_suppressed_by_fkey FOREIGN KEY (
            suppressed_by_item_id,
            notification_fingerprint,
            comparison_identity_version,
            admission_policy_version
          ) REFERENCES news_push_deliveries (
            item_id,
            notification_fingerprint,
            comparison_identity_version,
            admission_policy_version
          );

        ALTER TABLE news_push_state
          DROP CONSTRAINT news_push_state_delivery_counts_check,
          ADD COLUMN suppressed_count bigint NOT NULL DEFAULT 0;

        UPDATE news_push_state
           SET delivery_available = false,
               enablement_epoch_at_ms = NULL,
               total_count = 0,
               suppressed_count = 0,
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
          ADD CONSTRAINT news_push_state_delivery_counts_check CHECK (
            total_count >= 0
            AND suppressed_count >= 0
            AND pending_count >= 0
            AND sending_count >= 0
            AND sent_count >= 0
            AND terminal_count >= 0
            AND total_count = suppressed_count + pending_count + sending_count
                              + sent_count + terminal_count
          );

        CREATE INDEX ix_news_push_deliveries_pending
          ON news_push_deliveries(live_observed_at_ms, item_id)
          INCLUDE (source_title_fingerprint)
          WHERE status = 'pending'
            AND source_payload ->> 'schema_version' = 'news_item_push_v2';

        CREATE INDEX ix_news_push_deliveries_attempted
          ON news_push_deliveries(attempted_at_ms, item_id)
          INCLUDE (status, source_title_fingerprint)
          WHERE attempted_at_ms IS NOT NULL
            AND source_payload ->> 'schema_version' = 'news_item_push_v2';

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
            source_title_fingerprint
          )
          WHERE status IN ('sent', 'terminal')
            AND source_payload ->> 'schema_version' = 'news_item_push_v2';

        CREATE INDEX ix_news_push_deliveries_exact_atom_leader
          ON news_push_deliveries(
            admission_policy_version,
            notification_fingerprint,
            ((source_payload ->> 'provider_published_at_ms')::bigint),
            item_id
          )
          WHERE status IN ('pending', 'sending', 'sent', 'terminal')
            AND source_payload ->> 'schema_version' = 'news_item_push_v2';

        CREATE INDEX ix_news_push_deliveries_suppressed_recent
          ON news_push_deliveries(adjudicated_at_ms DESC, item_id DESC)
          INCLUDE (
            suppressed_by_item_id,
            notification_fingerprint,
            comparison_identity_version,
            admission_policy_version
          )
          WHERE status = 'suppressed'
            AND source_payload ->> 'schema_version' = 'news_item_push_v2';

        ANALYZE news_push_state;
        ANALYZE news_push_deliveries;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260815_0273 is an irreversible News Push exact-atom hard cut")
