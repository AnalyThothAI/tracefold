"""Persist News eligibility/reconcile clocks and cover market-target reads.

Revision ID: 20260813_0258
Revises: 20260813_0257
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0258"
down_revision = "20260813_0257"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        ALTER TABLE news_push_state
          ADD COLUMN reconcile_cursor_story_id text;

        ALTER TABLE news_items
          ADD COLUMN push_eligibility_updated_at_ms bigint;
        UPDATE news_items
           SET push_eligibility_updated_at_ms = coalesce(
                 provider_score_updated_at_ms,
                 first_observed_at_ms
               )
         WHERE jsonb_typeof(provider_metadata -> 'score') = 'number';
        ALTER TABLE news_items
          ADD CONSTRAINT news_items_push_eligibility_updated_at_ms_check
          CHECK (
            push_eligibility_updated_at_ms IS NULL
            OR push_eligibility_updated_at_ms >= 0
          );

        DROP INDEX idx_events_received;
        CREATE INDEX idx_events_received
          ON events (received_at_ms, event_id);

        DROP INDEX ux_token_intent_current_resolution;
        CREATE UNIQUE INDEX ux_token_intent_current_resolution
          ON token_intent_resolutions (intent_id)
          INCLUDE (resolution_status, target_type, target_id)
          WHERE is_current = true;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260813_0258 is an irreversible reconcile and covering-index cut")
