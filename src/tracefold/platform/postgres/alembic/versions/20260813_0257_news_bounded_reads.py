"""Bound News feed and durable push-health reads.

Revision ID: 20260813_0257
Revises: 20260813_0256
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0257"
down_revision = "20260813_0256"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        CREATE INDEX ix_news_items_member_provider_score
          ON news_items (
            item_id,
            ((provider_metadata ->> 'score')::numeric)
          )
          INCLUDE (provider_metadata)
          WHERE jsonb_typeof(provider_metadata -> 'score') = 'number';

        ALTER TABLE news_push_state
          ADD COLUMN total_count bigint NOT NULL DEFAULT 0,
          ADD COLUMN suppressed_count bigint NOT NULL DEFAULT 0,
          ADD COLUMN pending_count bigint NOT NULL DEFAULT 0,
          ADD COLUMN retry_count bigint NOT NULL DEFAULT 0,
          ADD COLUMN sent_count bigint NOT NULL DEFAULT 0,
          ADD COLUMN terminal_count bigint NOT NULL DEFAULT 0,
          ADD COLUMN latest_sent_at_ms bigint,
          ADD COLUMN latest_error text,
          ADD COLUMN latest_error_at_ms bigint;

        ALTER TABLE news_push_deliveries
          ADD COLUMN translation_prompt_version text,
          ADD COLUMN translation_attempted_at_ms bigint,
          ADD COLUMN translation_duration_ms bigint,
          ADD COLUMN translation_fallback_code text;

        UPDATE news_push_deliveries
           SET translation_prompt_version = CASE
                 WHEN NULLIF(
                        btrim(delivery_payload #>> '{presentation,prompt_version}'),
                        ''
                      ) IS NOT NULL
                   THEN btrim(delivery_payload #>> '{presentation,prompt_version}')
                 ELSE NULL
               END,
               translation_attempted_at_ms = CASE
                 WHEN NULLIF(
                        btrim(delivery_payload #>> '{presentation,prompt_version}'),
                        ''
                      ) IS NOT NULL
                  AND jsonb_typeof(
                        delivery_payload #> '{presentation,translation_attempted_at_ms}'
                      ) = 'number'
                  AND (delivery_payload #>> '{presentation,translation_attempted_at_ms}')
                        ~ '^[0-9]{1,19}$'
                  AND (delivery_payload #>> '{presentation,translation_attempted_at_ms}')::numeric
                        <= 9223372036854775807
                   THEN (delivery_payload #>> '{presentation,translation_attempted_at_ms}')::bigint
                 ELSE NULL
               END,
               translation_duration_ms = CASE
                 WHEN NULLIF(
                        btrim(delivery_payload #>> '{presentation,prompt_version}'),
                        ''
                      ) IS NOT NULL
                  AND jsonb_typeof(
                        delivery_payload #> '{presentation,translation_attempted_at_ms}'
                      ) = 'number'
                  AND (delivery_payload #>> '{presentation,translation_attempted_at_ms}')
                        ~ '^[0-9]{1,19}$'
                  AND (delivery_payload #>> '{presentation,translation_attempted_at_ms}')::numeric
                        <= 9223372036854775807
                  AND jsonb_typeof(
                        delivery_payload #> '{presentation,translation_duration_ms}'
                      ) = 'number'
                  AND (delivery_payload #>> '{presentation,translation_duration_ms}')
                        ~ '^[0-9]{1,19}$'
                  AND (delivery_payload #>> '{presentation,translation_duration_ms}')::numeric
                        <= 9223372036854775807
                   THEN (delivery_payload #>> '{presentation,translation_duration_ms}')::bigint
                 ELSE NULL
               END,
               translation_fallback_code = CASE
                 WHEN NULLIF(
                        btrim(delivery_payload #>> '{presentation,prompt_version}'),
                        ''
                      ) IS NOT NULL
                   THEN NULLIF(
                     btrim(delivery_payload #>> '{presentation,fallback_code}'),
                     ''
                   )
                 ELSE NULL
               END
         WHERE delivery_payload IS NOT NULL;

        WITH aggregate AS (
          SELECT count(*) AS total_count,
                 count(*) FILTER (WHERE status = 'suppressed')
                   AS suppressed_count,
                 count(*) FILTER (
                   WHERE status IN ('pending_translation', 'pending_delivery')
                 ) AS pending_count,
                 count(*) FILTER (WHERE status = 'retry_wait') AS retry_count,
                 count(*) FILTER (WHERE status = 'sent') AS sent_count,
                 count(*) FILTER (WHERE status = 'terminal') AS terminal_count,
                 max(sent_at_ms) AS latest_sent_at_ms
            FROM news_push_deliveries
        ), latest_error AS (
          SELECT CASE
                   WHEN lower(btrim(last_error)) ~ '^[a-z0-9_]{1,120}$'
                     THEN lower(btrim(last_error))
                   ELSE 'news_story_push_delivery_error'
                 END AS latest_error,
                 updated_at_ms AS latest_error_at_ms
            FROM news_push_deliveries
           WHERE last_error IS NOT NULL
           ORDER BY updated_at_ms DESC, story_id
           LIMIT 1
        )
        UPDATE news_push_state state
           SET total_count = aggregate.total_count,
               suppressed_count = aggregate.suppressed_count,
               pending_count = aggregate.pending_count,
               retry_count = aggregate.retry_count,
               sent_count = aggregate.sent_count,
               terminal_count = aggregate.terminal_count,
               latest_sent_at_ms = aggregate.latest_sent_at_ms,
               latest_error = latest_error.latest_error,
               latest_error_at_ms = latest_error.latest_error_at_ms
          FROM aggregate
          LEFT JOIN latest_error ON true
         WHERE state.singleton_key = 'current';

        ALTER TABLE news_push_state
          ADD CONSTRAINT news_push_state_delivery_counts_check CHECK (
            total_count >= 0
            AND suppressed_count >= 0
            AND pending_count >= 0
            AND retry_count >= 0
            AND sent_count >= 0
            AND terminal_count >= 0
            AND total_count = suppressed_count + pending_count + retry_count
                              + sent_count + terminal_count
          ),
          ADD CONSTRAINT news_push_state_latest_sent_at_ms_check CHECK (
            latest_sent_at_ms IS NULL OR latest_sent_at_ms >= 0
          ),
          ADD CONSTRAINT news_push_state_latest_error_check CHECK (
            (
              latest_error IS NULL
              AND latest_error_at_ms IS NULL
            ) OR (
              latest_error ~ '^[a-z0-9_]{1,120}$'
              AND latest_error_at_ms >= 0
            )
          );

        ALTER TABLE news_push_deliveries
          ADD CONSTRAINT news_push_deliveries_translation_prompt_version_check
            CHECK (
              translation_prompt_version IS NULL
              OR NULLIF(btrim(translation_prompt_version), '') IS NOT NULL
            ),
          ADD CONSTRAINT news_push_deliveries_translation_attempted_at_ms_check
            CHECK (
              translation_attempted_at_ms IS NULL
              OR (
                translation_prompt_version IS NOT NULL
                AND translation_attempted_at_ms >= 0
              )
            ),
          ADD CONSTRAINT news_push_deliveries_translation_duration_ms_check
            CHECK (
              translation_duration_ms IS NULL
              OR (
                translation_attempted_at_ms IS NOT NULL
                AND translation_duration_ms >= 0
              )
            ),
          ADD CONSTRAINT news_push_deliveries_translation_fallback_code_check
            CHECK (
              translation_fallback_code IS NULL
              OR (
                translation_prompt_version IS NOT NULL
                AND NULLIF(btrim(translation_fallback_code), '') IS NOT NULL
              )
            );

        CREATE INDEX ix_news_push_deliveries_oldest_waiting
          ON news_push_deliveries(threshold_observed_at_ms, story_id)
          WHERE status IN (
            'pending_translation', 'pending_delivery', 'retry_wait'
          );

        CREATE INDEX ix_news_push_deliveries_translation_attempted
          ON news_push_deliveries(translation_attempted_at_ms, story_id)
          INCLUDE (
            translation_status,
            translation_duration_ms,
            translation_fallback_code
          )
          WHERE translation_prompt_version = 'title_zh_v2'
            AND translation_attempted_at_ms IS NOT NULL;

        CREATE INDEX ix_news_push_deliveries_completed_at
          ON news_push_deliveries (
            (CASE
              WHEN status = 'sent' THEN sent_at_ms
              ELSE updated_at_ms
            END),
            story_id
          )
          INCLUDE (
            status,
            sent_at_ms,
            updated_at_ms,
            threshold_observed_at_ms
          )
          WHERE translation_prompt_version = 'title_zh_v2'
            AND status IN ('sent', 'terminal');

        ANALYZE news_items;
        ANALYZE news_push_deliveries;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260813_0257 is an irreversible News read-index cut")
