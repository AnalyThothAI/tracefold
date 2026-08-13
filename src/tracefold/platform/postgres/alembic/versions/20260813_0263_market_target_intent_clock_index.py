"""Drive active market targets from the immutable Intent acquisition clock.

Revision ID: 20260813_0263
Revises: 20260813_0262
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0263"
down_revision = "20260813_0262"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
              FROM token_intents intent
              JOIN events event ON event.event_id = intent.event_id
             WHERE intent.created_at_ms IS DISTINCT FROM event.received_at_ms
          ) THEN
            RAISE EXCEPTION 'token_intent_acquisition_clock_mismatch';
          END IF;
        END
        $$;

        DROP INDEX idx_token_intents_event;

        CREATE INDEX idx_token_intents_market_targets_created
          ON token_intents(created_at_ms, intent_id)
          INCLUDE (event_id);

        ANALYZE token_intents;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260813_0263 is an irreversible market-target read cut")
