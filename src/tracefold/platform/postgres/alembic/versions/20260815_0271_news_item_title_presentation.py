"""Hard-cut Item title presentation out of Push ownership.

Revision ID: 20260815_0271
Revises: 20260814_0270
"""

from __future__ import annotations

from alembic import op

revision = "20260815_0271"
down_revision = "20260814_0270"
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
            RAISE EXCEPTION 'news_item_title_presentation_hard_cut_workers_active'
              USING ERRCODE = '55006';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        r"""
        CREATE TABLE news_item_title_presentations (
          item_id text NOT NULL,
          source_title_fingerprint text NOT NULL,
          original_title text NOT NULL,
          state text NOT NULL,
          display_title text,
          outcome text,
          provider text,
          policy_version text,
          fallback_code text,
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL,
          attempted_at_ms bigint,
          resolved_at_ms bigint,
          duration_ms bigint,
          CONSTRAINT news_item_title_presentations_pkey
            PRIMARY KEY (item_id, source_title_fingerprint),
          CONSTRAINT news_item_title_presentations_item_fkey
            FOREIGN KEY (item_id) REFERENCES news_items(item_id)
            ON DELETE CASCADE,
          CONSTRAINT news_item_title_presentations_item_check CHECK (
            item_id ~ '^news_item_[0-9a-f]{32}$'
          ),
          CONSTRAINT news_item_title_presentations_title_check CHECK (
            original_title <> ''
          ),
          CONSTRAINT news_item_title_presentations_fingerprint_check CHECK (
            source_title_fingerprint ~ '^[0-9a-f]{64}$'
            AND source_title_fingerprint = encode(
              sha256(convert_to(original_title, 'UTF8')),
              'hex'
            )
          ),
          CONSTRAINT news_item_title_presentations_clock_check CHECK (
            created_at_ms >= 0
            AND updated_at_ms >= created_at_ms
            AND (attempted_at_ms IS NULL OR attempted_at_ms >= created_at_ms)
            AND (resolved_at_ms IS NULL OR resolved_at_ms >= created_at_ms)
            AND (duration_ms IS NULL OR duration_ms >= 0)
          ),
          CONSTRAINT news_item_title_presentations_state_check CHECK (
            state IN ('pending', 'resolving', 'resolved')
          ),
          CONSTRAINT news_item_title_presentations_shape_check CHECK (
            (
              state = 'pending'
              AND display_title IS NULL
              AND outcome IS NULL
              AND provider IS NULL
              AND policy_version IS NULL
              AND fallback_code IS NULL
              AND attempted_at_ms IS NULL
              AND resolved_at_ms IS NULL
              AND duration_ms IS NULL
            )
            OR (
              state = 'resolving'
              AND display_title IS NULL
              AND outcome IS NULL
              AND provider IS NULL
              AND policy_version IS NULL
              AND fallback_code IS NULL
              AND attempted_at_ms IS NOT NULL
              AND resolved_at_ms IS NULL
              AND duration_ms IS NULL
            )
            OR (
              state = 'resolved'
              AND display_title IS NOT NULL
              AND display_title <> ''
              AND outcome IN ('translated', 'not_needed', 'fallback')
              AND policy_version ~ '^[a-z0-9_]{1,120}$'
              AND resolved_at_ms IS NOT NULL
              AND duration_ms IS NOT NULL
              AND (
                (
                  outcome = 'translated'
                  AND provider IN ('deepl', 'deepseek')
                  AND fallback_code IS NULL
                )
                OR (
                  outcome = 'not_needed'
                  AND provider IS NULL
                  AND fallback_code IS NULL
                  AND display_title = original_title
                  AND attempted_at_ms IS NULL
                  AND duration_ms = 0
                )
                OR (
                  outcome = 'fallback'
                  AND provider IS NULL
                  AND fallback_code ~ '^[a-z0-9_]{1,120}$'
                  AND display_title = original_title
                )
              )
            )
          )
        );

        CREATE INDEX ix_news_item_title_presentations_pending
          ON news_item_title_presentations(
            created_at_ms, item_id, source_title_fingerprint
          )
          WHERE state = 'pending';

        CREATE INDEX ix_news_item_title_presentations_resolving
          ON news_item_title_presentations(
            attempted_at_ms, item_id, source_title_fingerprint
          )
          WHERE state = 'resolving';

        CREATE INDEX ix_news_item_title_presentations_resolved
          ON news_item_title_presentations(
            resolved_at_ms, item_id, source_title_fingerprint
          )
          INCLUDE (outcome, provider, duration_ms, fallback_code)
          WHERE state = 'resolved';

        ALTER TABLE news_item_title_presentations OWNER TO tracefold_owner;
        GRANT SELECT ON news_item_title_presentations TO tracefold_serve;
        GRANT SELECT, INSERT, UPDATE, DELETE
          ON news_item_title_presentations TO tracefold_workers;

        DROP INDEX IF EXISTS ix_news_push_deliveries_pending;
        DROP INDEX IF EXISTS ix_news_push_deliveries_translation_attempted;
        DROP INDEX IF EXISTS ix_news_push_deliveries_completed;

        ALTER TABLE news_push_deliveries
          DROP CONSTRAINT news_push_deliveries_current_source_check,
          DROP CONSTRAINT news_push_deliveries_current_state_check,
          DROP CONSTRAINT news_push_deliveries_presentation_snapshot_check;

        ALTER TABLE news_push_deliveries
          RENAME COLUMN presentation_snapshot TO legacy_presentation_snapshot;

        ALTER TABLE news_push_deliveries
          ADD COLUMN source_title_fingerprint text;

        WITH cutover AS (
          SELECT (extract(epoch FROM transaction_timestamp()) * 1000)::bigint
                   AS at_ms
        )
        UPDATE news_push_deliveries delivery
           SET status = 'terminal',
               last_error = 'news_item_title_presentation_policy_retired',
               updated_at_ms = greatest(delivery.updated_at_ms, cutover.at_ms)
          FROM cutover
         WHERE delivery.status NOT IN ('sent', 'terminal');

        ALTER TABLE news_push_deliveries
          ADD CONSTRAINT news_push_deliveries_legacy_presentation_check CHECK (
            legacy_presentation_snapshot IS NULL
            OR jsonb_typeof(legacy_presentation_snapshot) = 'object'
          ),
          ADD CONSTRAINT news_push_deliveries_title_fingerprint_check CHECK (
            source_title_fingerprint IS NULL
            OR (
              source_title_fingerprint ~ '^[0-9a-f]{64}$'
              AND source_payload ->> 'schema_version' = 'news_item_push_v1'
              AND source_payload ?& ARRAY[
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
              AND source_title_fingerprint = encode(
                sha256(convert_to(source_payload ->> 'original_title', 'UTF8')),
                'hex'
              )
              AND jsonb_typeof(source_payload -> 'live_observed_at_ms') = 'number'
              AND jsonb_typeof(source_payload -> 'provider_published_at_ms') = 'number'
              AND jsonb_typeof(source_payload -> 'strategy_labels') = 'array'
              AND jsonb_typeof(source_payload -> 'assets') = 'array'
              AND jsonb_array_length(source_payload -> 'strategy_labels') <= 32
              AND jsonb_array_length(source_payload -> 'assets') <= 32
            )
          ),
          ADD CONSTRAINT news_push_deliveries_current_state_check CHECK (
            (
              source_title_fingerprint IS NULL
              AND status IN ('sent', 'terminal')
            )
            OR (
              source_title_fingerprint IS NOT NULL
              AND legacy_presentation_snapshot IS NULL
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
          ),
          ADD CONSTRAINT news_push_deliveries_presentation_fkey
            FOREIGN KEY (item_id, source_title_fingerprint)
            REFERENCES news_item_title_presentations(
              item_id, source_title_fingerprint
            );

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

        CREATE INDEX ix_news_push_deliveries_pending
          ON news_push_deliveries(live_observed_at_ms, item_id)
          INCLUDE (source_title_fingerprint)
          WHERE status = 'pending'
            AND source_title_fingerprint IS NOT NULL;

        CREATE INDEX ix_news_push_deliveries_attempted
          ON news_push_deliveries(attempted_at_ms, item_id)
          INCLUDE (status, source_title_fingerprint)
          WHERE attempted_at_ms IS NOT NULL
            AND source_title_fingerprint IS NOT NULL;

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
            AND source_title_fingerprint IS NOT NULL;

        ANALYZE news_item_title_presentations;
        ANALYZE news_push_state;
        ANALYZE news_push_deliveries;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260815_0271 is an irreversible News Item Title Presentation hard cut")
