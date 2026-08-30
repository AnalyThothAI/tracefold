"""Fence and bind Telegram multi-target picker replies (#327).

Revision ID: 20260829_0332
Revises: 20260829_0331
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0332"
down_revision = "20260829_0331"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trading_manual_target_pickers (
          picker_id                 UUID PRIMARY KEY,
          sources_sha256            TEXT NOT NULL,
          sources                   JSONB NOT NULL,
          actor_user_id             BIGINT NOT NULL,
          chat_id                   BIGINT NOT NULL,
          source_message_id         BIGINT NOT NULL,
          interaction_message_id    BIGINT,
          reply_attempted_at_ms      BIGINT,
          state                     TEXT NOT NULL DEFAULT 'PENDING',
          created_at_ms             BIGINT NOT NULL,
          updated_at_ms             BIGINT NOT NULL,
          CONSTRAINT trading_manual_target_picker_sha_check CHECK (
            sources_sha256 ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT trading_manual_target_picker_sources_check CHECK (
            jsonb_typeof(sources) = 'array' AND jsonb_array_length(sources) BETWEEN 2 AND 4
          ),
          CONSTRAINT trading_manual_target_picker_identity_check CHECK (
            actor_user_id > 0 AND source_message_id > 0
            AND (interaction_message_id IS NULL OR interaction_message_id > 0)
          ),
          CONSTRAINT trading_manual_target_picker_state_check CHECK (
            state IN ('PENDING', 'SENDING', 'SENT')
          ),
          CONSTRAINT trading_manual_target_picker_shape_check CHECK (
            (state = 'PENDING' AND reply_attempted_at_ms IS NULL AND interaction_message_id IS NULL)
            OR (state = 'SENDING' AND reply_attempted_at_ms IS NOT NULL AND interaction_message_id IS NULL)
            OR (state = 'SENT' AND reply_attempted_at_ms IS NOT NULL AND interaction_message_id IS NOT NULL)
          ),
          CONSTRAINT trading_manual_target_picker_time_check CHECK (
            created_at_ms > 0 AND updated_at_ms >= created_at_ms
            AND (reply_attempted_at_ms IS NULL OR reply_attempted_at_ms >= created_at_ms)
          ),
          UNIQUE (chat_id, actor_user_id, source_message_id)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_trading_manual_target_picker_message
          ON trading_manual_target_pickers (chat_id, interaction_message_id)
         WHERE interaction_message_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_manual_target_picker_identity_mutation() RETURNS trigger
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
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_manual_target_pickers_identity "
        "BEFORE UPDATE ON trading_manual_target_pickers "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_manual_target_picker_identity_mutation()"
    )
    op.execute(
        "REVOKE ALL ON trading_manual_target_pickers FROM tracefold_workers, tracefold_serve, tracefold_nautilus"
    )
    op.execute("GRANT SELECT ON trading_manual_target_pickers TO tracefold_workers, tracefold_serve")
    op.execute(
        "GRANT INSERT (picker_id, sources_sha256, sources, actor_user_id, chat_id, source_message_id, "
        "state, created_at_ms, updated_at_ms) ON trading_manual_target_pickers TO tracefold_workers"
    )
    op.execute(
        "GRANT UPDATE (interaction_message_id, reply_attempted_at_ms, state, updated_at_ms) "
        "ON trading_manual_target_pickers TO tracefold_workers"
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0332 owns durable Telegram picker effects and cannot be downgraded")
