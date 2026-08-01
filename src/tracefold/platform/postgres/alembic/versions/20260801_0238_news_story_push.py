"""Add durable News Story push state.

Revision ID: 20260801_0238
Revises: 20260801_0237
"""

from __future__ import annotations

from alembic import op

revision = "20260801_0238"
down_revision = "20260801_0237"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE news_push_state (
          singleton_key text PRIMARY KEY,
          baseline_at_ms bigint,
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT news_push_state_singleton_check
            CHECK (singleton_key = 'current'),
          CONSTRAINT news_push_state_baseline_at_ms_check
            CHECK (baseline_at_ms IS NULL OR baseline_at_ms >= 0),
          CONSTRAINT news_push_state_created_at_ms_check
            CHECK (created_at_ms >= 0),
          CONSTRAINT news_push_state_updated_at_ms_check
            CHECK (updated_at_ms >= 0)
        );

        INSERT INTO news_push_state (
          singleton_key, baseline_at_ms, created_at_ms, updated_at_ms
        ) VALUES ('current', NULL, 0, 0);

        CREATE TABLE news_push_deliveries (
          story_id text PRIMARY KEY,
          selected_item_id text NOT NULL,
          provider_score double precision NOT NULL,
          threshold_observed_at_ms bigint NOT NULL,
          source_payload jsonb NOT NULL,
          delivery_payload jsonb,
          payload_fingerprint text,
          translation_status text NOT NULL,
          status text NOT NULL,
          delivery_attempts integer NOT NULL DEFAULT 0,
          next_attempt_at_ms bigint,
          lease_owner text,
          lease_token text,
          lease_expires_at_ms bigint,
          receipt jsonb,
          last_error text,
          sent_at_ms bigint,
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT news_push_deliveries_story_id_check
            CHECK (story_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_push_deliveries_selected_item_id_check
            CHECK (btrim(selected_item_id) <> ''),
          CONSTRAINT news_push_deliveries_provider_score_check
            CHECK (provider_score > 70),
          CONSTRAINT news_push_deliveries_threshold_observed_at_ms_check
            CHECK (threshold_observed_at_ms >= 0),
          CONSTRAINT news_push_deliveries_source_payload_check
            CHECK (jsonb_typeof(source_payload) = 'object'),
          CONSTRAINT news_push_deliveries_delivery_payload_check
            CHECK (
              delivery_payload IS NULL
              OR jsonb_typeof(delivery_payload) = 'object'
            ),
          CONSTRAINT news_push_deliveries_payload_fingerprint_check
            CHECK (
              (delivery_payload IS NULL AND payload_fingerprint IS NULL)
              OR (
                delivery_payload IS NOT NULL
                AND payload_fingerprint ~ '^[0-9a-f]{64}$'
              )
            ),
          CONSTRAINT news_push_deliveries_translation_status_check
            CHECK (
              translation_status IN (
                'not_requested', 'pending', 'translated',
                'not_needed', 'unavailable'
              )
            ),
          CONSTRAINT news_push_deliveries_status_check
            CHECK (
              status IN (
                'suppressed', 'pending_translation', 'pending_delivery',
                'retry_wait', 'sent', 'terminal'
              )
            ),
          CONSTRAINT news_push_deliveries_attempts_check
            CHECK (delivery_attempts >= 0),
          CONSTRAINT news_push_deliveries_next_attempt_at_ms_check
            CHECK (next_attempt_at_ms IS NULL OR next_attempt_at_ms >= 0),
          CONSTRAINT news_push_deliveries_lease_check
            CHECK (
              (lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at_ms IS NULL)
              OR (
                NULLIF(btrim(lease_owner), '') IS NOT NULL
                AND NULLIF(btrim(lease_token), '') IS NOT NULL
                AND lease_expires_at_ms >= 0
              )
            ),
          CONSTRAINT news_push_deliveries_receipt_check
            CHECK (receipt IS NULL OR jsonb_typeof(receipt) = 'object'),
          CONSTRAINT news_push_deliveries_sent_at_ms_check
            CHECK (sent_at_ms IS NULL OR sent_at_ms >= 0),
          CONSTRAINT news_push_deliveries_created_at_ms_check
            CHECK (created_at_ms >= 0),
          CONSTRAINT news_push_deliveries_updated_at_ms_check
            CHECK (updated_at_ms >= 0)
        );

        CREATE INDEX ix_news_push_deliveries_due
          ON news_push_deliveries(next_attempt_at_ms, created_at_ms, story_id)
          WHERE status IN (
            'pending_translation', 'pending_delivery', 'retry_wait'
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260801_0238 is an irreversible News Story push migration")
