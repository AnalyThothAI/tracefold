"""Bind consumed Telegram target pickers to the exact manual session (#327).

Revision ID: 20260829_0334
Revises: 20260829_0333
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0334"
down_revision = "20260829_0333"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_trading_manual_target_picker_identity_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.picker_id IS DISTINCT FROM OLD.picker_id
            OR NEW.sources_sha256 IS DISTINCT FROM OLD.sources_sha256
            OR NEW.sources IS DISTINCT FROM OLD.sources
            OR NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id
            OR NEW.chat_id IS DISTINCT FROM OLD.chat_id
            OR NEW.source_message_id IS DISTINCT FROM OLD.source_message_id
            OR NEW.created_at_ms IS DISTINCT FROM OLD.created_at_ms
          THEN
            RAISE EXCEPTION 'trading_manual_target_picker_identity_mutation_forbidden';
          END IF;
          IF NEW.updated_at_ms < OLD.updated_at_ms THEN
            RAISE EXCEPTION 'trading_manual_target_picker_time_regression_forbidden';
          END IF;
          IF OLD.state = 'PENDING' THEN
            IF NOT (
              NEW.state = 'SENDING'
              AND NEW.reply_attempted_at_ms IS NOT NULL
              AND NEW.interaction_message_id IS NULL
              AND NEW.selected_symbol IS NULL
              AND NEW.consumed_session_id IS NULL
              AND NEW.consumed_at_ms IS NULL
            ) THEN
              RAISE EXCEPTION 'trading_manual_target_picker_transition_forbidden';
            END IF;
          ELSIF OLD.state = 'SENDING' THEN
            IF NOT (
              NEW.state = 'SENT'
              AND NEW.reply_attempted_at_ms IS NOT DISTINCT FROM OLD.reply_attempted_at_ms
              AND NEW.interaction_message_id IS NOT NULL
              AND NEW.selected_symbol IS NULL
              AND NEW.consumed_session_id IS NULL
              AND NEW.consumed_at_ms IS NULL
            ) THEN
              RAISE EXCEPTION 'trading_manual_target_picker_transition_forbidden';
            END IF;
          ELSIF OLD.state = 'SENT' THEN
            IF NOT (
              NEW.state = 'CONSUMED'
              AND NEW.reply_attempted_at_ms IS NOT DISTINCT FROM OLD.reply_attempted_at_ms
              AND NEW.interaction_message_id IS NOT DISTINCT FROM OLD.interaction_message_id
              AND NEW.selected_symbol IS NOT NULL
              AND NEW.consumed_session_id IS NOT NULL
              AND NEW.consumed_at_ms IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(NEW.sources) AS source
                 WHERE source ->> 'base_symbol' = NEW.selected_symbol
              )
              AND EXISTS (
                SELECT 1
                  FROM trading_manual_sessions session
                 WHERE session.session_id = NEW.consumed_session_id
                   AND session.actor_user_id = NEW.actor_user_id
                   AND session.chat_id = NEW.chat_id
                   AND session.source_message_id = NEW.source_message_id
                   AND session.source ->> 'base_symbol' = NEW.selected_symbol
              )
            ) THEN
              RAISE EXCEPTION 'trading_manual_target_picker_transition_forbidden';
            END IF;
          ELSE
            RAISE EXCEPTION 'trading_manual_target_picker_transition_forbidden';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0334 owns exact picker/session binding and cannot be downgraded")
