"""Make Telegram target-picking monotonic, replay-safe, and reusable (#327).

Revision ID: 20260829_0333
Revises: 20260829_0332
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0333"
down_revision = "20260829_0332"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE trading_manual_target_pickers
          ADD COLUMN selected_symbol TEXT,
          ADD COLUMN consumed_session_id UUID REFERENCES trading_manual_sessions(session_id) ON DELETE RESTRICT,
          ADD COLUMN consumed_at_ms BIGINT
        """
    )
    op.execute(
        """
        DO $$
        DECLARE
          old_unique_name TEXT;
        BEGIN
          SELECT constraint_row.conname
            INTO old_unique_name
            FROM pg_constraint constraint_row
           WHERE constraint_row.conrelid = 'trading_manual_target_pickers'::regclass
             AND constraint_row.contype = 'u'
             AND pg_get_constraintdef(constraint_row.oid)
                   LIKE 'UNIQUE (chat_id, actor_user_id, source_message_id)%'
           LIMIT 1;
          IF old_unique_name IS NOT NULL THEN
            EXECUTE format(
              'ALTER TABLE trading_manual_target_pickers DROP CONSTRAINT %I',
              old_unique_name
            );
          END IF;
        END;
        $$
        """
    )
    op.execute("ALTER TABLE trading_manual_target_pickers DROP CONSTRAINT trading_manual_target_picker_state_check")
    op.execute("ALTER TABLE trading_manual_target_pickers DROP CONSTRAINT trading_manual_target_picker_shape_check")
    op.execute("ALTER TABLE trading_manual_target_pickers DROP CONSTRAINT trading_manual_target_picker_time_check")
    op.execute(
        """
        ALTER TABLE trading_manual_target_pickers
          ADD CONSTRAINT trading_manual_target_picker_state_check CHECK (
            state IN ('PENDING', 'SENDING', 'SENT', 'CONSUMED')
          ),
          ADD CONSTRAINT trading_manual_target_picker_selected_symbol_check CHECK (
            selected_symbol IS NULL OR selected_symbol ~ '^[A-Z0-9][A-Z0-9.-]{0,19}$'
          ),
          ADD CONSTRAINT trading_manual_target_picker_shape_check CHECK (
            (state = 'PENDING'
              AND reply_attempted_at_ms IS NULL AND interaction_message_id IS NULL
              AND selected_symbol IS NULL AND consumed_session_id IS NULL AND consumed_at_ms IS NULL)
            OR (state = 'SENDING'
              AND reply_attempted_at_ms IS NOT NULL AND interaction_message_id IS NULL
              AND selected_symbol IS NULL AND consumed_session_id IS NULL AND consumed_at_ms IS NULL)
            OR (state = 'SENT'
              AND reply_attempted_at_ms IS NOT NULL AND interaction_message_id IS NOT NULL
              AND selected_symbol IS NULL AND consumed_session_id IS NULL AND consumed_at_ms IS NULL)
            OR (state = 'CONSUMED'
              AND reply_attempted_at_ms IS NOT NULL AND interaction_message_id IS NOT NULL
              AND selected_symbol IS NOT NULL AND consumed_session_id IS NOT NULL AND consumed_at_ms IS NOT NULL)
          ),
          ADD CONSTRAINT trading_manual_target_picker_time_check CHECK (
            created_at_ms > 0 AND updated_at_ms >= created_at_ms
            AND (reply_attempted_at_ms IS NULL OR reply_attempted_at_ms >= created_at_ms)
            AND (consumed_at_ms IS NULL OR consumed_at_ms >= reply_attempted_at_ms)
          )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_trading_manual_active_target_picker
          ON trading_manual_target_pickers (chat_id, actor_user_id, source_message_id)
         WHERE state <> 'CONSUMED'
        """
    )
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
    op.execute(
        "GRANT UPDATE (selected_symbol, consumed_session_id, consumed_at_ms) "
        "ON trading_manual_target_pickers TO tracefold_workers"
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0333 hardens durable picker lifecycle and cannot be downgraded")
