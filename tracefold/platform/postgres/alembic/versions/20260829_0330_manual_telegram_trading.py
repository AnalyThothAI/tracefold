"""Telegram-first manual Trading sessions, event ledger, and intent fence (#327).

Revision ID: 20260829_0330
Revises: 20260829_0329
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0330"
down_revision = "20260830_0332"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trading_account_bindings (
          account_ref             TEXT PRIMARY KEY,
          account_lane            TEXT NOT NULL,
          venue                   TEXT NOT NULL,
          credential_fingerprint  TEXT NOT NULL UNIQUE,
          provider_account_fingerprint TEXT NOT NULL UNIQUE,
          created_at_ms           BIGINT NOT NULL,
          CONSTRAINT trading_account_binding_ref_check
            CHECK (account_ref ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
          CONSTRAINT trading_account_binding_lane_check CHECK (account_lane IN ('manual', 'auto')),
          CONSTRAINT trading_account_binding_venue_check CHECK (venue = 'binance_usdm_demo'),
          CONSTRAINT trading_account_binding_fingerprint_check
            CHECK (credential_fingerprint ~ '^[0-9a-f]{64}$'
              AND provider_account_fingerprint ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_account_binding_time_check CHECK (created_at_ms > 0),
          UNIQUE (account_lane, venue)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_manual_account_snapshots (
          account_ref       TEXT PRIMARY KEY
            REFERENCES trading_account_bindings(account_ref) ON DELETE RESTRICT,
          venue             TEXT NOT NULL,
          equity_usd        NUMERIC NOT NULL,
          observed_at_ms    BIGINT NOT NULL,
          updated_at_ms     BIGINT NOT NULL,
          CONSTRAINT trading_manual_account_snapshot_venue_check CHECK (venue = 'binance_usdm_demo'),
          CONSTRAINT trading_manual_account_snapshot_equity_check CHECK (equity_usd > 0),
          CONSTRAINT trading_manual_account_snapshot_time_check CHECK (
            observed_at_ms > 0 AND updated_at_ms >= observed_at_ms
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_manual_runtime (
          id                       SMALLINT PRIMARY KEY DEFAULT 1,
          next_telegram_update_id  BIGINT NOT NULL DEFAULT 0,
          updated_at_ms            BIGINT NOT NULL DEFAULT 0,
          CONSTRAINT trading_manual_runtime_singleton_check CHECK (id = 1),
          CONSTRAINT trading_manual_runtime_cursor_check CHECK (next_telegram_update_id >= 0),
          CONSTRAINT trading_manual_runtime_time_check CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute("INSERT INTO trading_manual_runtime (id) VALUES (1)")
    op.execute(
        """
        CREATE TABLE trading_manual_telegram_updates (
          update_id           BIGINT PRIMARY KEY,
          callback_query_id   TEXT NOT NULL UNIQUE,
          actor_user_id       BIGINT NOT NULL,
          chat_id             BIGINT NOT NULL,
          message_id          BIGINT NOT NULL,
          callback_data       TEXT NOT NULL,
          authorized          BOOLEAN NOT NULL,
          state               TEXT NOT NULL DEFAULT 'RECEIVED',
          result_code         TEXT,
          received_at_ms      BIGINT NOT NULL,
          settled_at_ms       BIGINT,
          CONSTRAINT trading_manual_update_identity_check CHECK (
            update_id >= 0 AND length(callback_query_id) BETWEEN 1 AND 128
            AND actor_user_id > 0 AND message_id > 0
            AND octet_length(callback_data) BETWEEN 1 AND 64
          ),
          CONSTRAINT trading_manual_update_state_check CHECK (state IN ('RECEIVED', 'SETTLED')),
          CONSTRAINT trading_manual_update_result_check CHECK (
            (state = 'RECEIVED' AND result_code IS NULL AND settled_at_ms IS NULL)
            OR
            (state = 'SETTLED' AND result_code IS NOT NULL AND settled_at_ms IS NOT NULL)
          ),
          CONSTRAINT trading_manual_update_time_check CHECK (
            received_at_ms > 0 AND (settled_at_ms IS NULL OR settled_at_ms >= received_at_ms)
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_manual_sessions (
          session_id              UUID PRIMARY KEY,
          source_sha256           TEXT NOT NULL,
          source                  JSONB NOT NULL,
          actor_user_id           BIGINT NOT NULL,
          chat_id                 BIGINT NOT NULL,
          source_message_id       BIGINT NOT NULL,
          interaction_message_id  BIGINT,
          interaction_reply_attempted_at_ms BIGINT,
          last_effect_update_id   BIGINT,
          last_effect_result_code TEXT,
          state                   TEXT NOT NULL,
          preset                  TEXT,
          account_snapshot        JSONB,
          recommended             JSONB,
          selected                JSONB,
          preview                 JSONB,
          guard                   JSONB,
          intent_id               TEXT UNIQUE,
          version                 INTEGER NOT NULL DEFAULT 1,
          created_at_ms           BIGINT NOT NULL,
          updated_at_ms           BIGINT NOT NULL,
          CONSTRAINT trading_manual_session_source_sha_check CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_manual_session_actor_check CHECK (
            actor_user_id > 0 AND source_message_id > 0
            AND (interaction_message_id IS NULL OR interaction_message_id > 0)
            AND (interaction_reply_attempted_at_ms IS NULL OR interaction_reply_attempted_at_ms > 0)
          ),
          CONSTRAINT trading_manual_session_state_check CHECK (state IN (
            'AWAITING_STRATEGY', 'PREVIEW', 'MODIFYING', 'HIGH_RISK_CONFIRMATION',
            'CONFIRMED', 'SUBMITTING', 'OPEN', 'AMBIGUOUS', 'EXPOSED',
            'REJECTED', 'CANCELLED', 'CLOSED'
          )),
          CONSTRAINT trading_manual_session_preset_check CHECK (
            preset IS NULL OR preset IN ('tight_stop', 'wide_stop')
          ),
          CONSTRAINT trading_manual_session_intent_check CHECK (
            intent_id IS NULL OR intent_id ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT trading_manual_session_version_check CHECK (version > 0),
          CONSTRAINT trading_manual_session_effect_check CHECK (
            (last_effect_update_id IS NULL AND last_effect_result_code IS NULL)
            OR (last_effect_update_id IS NOT NULL AND last_effect_result_code IS NOT NULL
              AND last_effect_update_id >= 0 AND length(last_effect_result_code) BETWEEN 1 AND 80)
          ),
          CONSTRAINT trading_manual_session_time_check CHECK (
            created_at_ms > 0 AND updated_at_ms >= created_at_ms
          ),
          CONSTRAINT trading_manual_session_shape_check CHECK (
            (state = 'AWAITING_STRATEGY'
              AND preset IS NULL AND account_snapshot IS NULL AND recommended IS NULL
              AND selected IS NULL AND preview IS NULL AND guard IS NULL AND intent_id IS NULL)
            OR
            (state IN ('PREVIEW', 'MODIFYING', 'HIGH_RISK_CONFIRMATION')
              AND preset IS NOT NULL AND account_snapshot IS NOT NULL AND recommended IS NOT NULL
              AND selected IS NOT NULL AND preview IS NOT NULL AND guard IS NOT NULL AND intent_id IS NULL)
            OR
            (state IN ('CONFIRMED', 'SUBMITTING', 'OPEN', 'AMBIGUOUS', 'EXPOSED', 'REJECTED', 'CLOSED')
              AND preset IS NOT NULL AND account_snapshot IS NOT NULL AND recommended IS NOT NULL
              AND selected IS NOT NULL AND preview IS NOT NULL AND guard IS NOT NULL AND intent_id IS NOT NULL)
            OR
            (state = 'CANCELLED')
          )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_trading_manual_active_source_actor
          ON trading_manual_sessions (chat_id, actor_user_id, source_message_id)
         WHERE state NOT IN ('REJECTED', 'CANCELLED', 'CLOSED')
        """
    )
    op.execute(
        """
        CREATE TABLE trading_manual_events (
          event_id       TEXT PRIMARY KEY,
          session_id     UUID NOT NULL REFERENCES trading_manual_sessions(session_id) ON DELETE RESTRICT,
          event_index    INTEGER NOT NULL,
          event_kind     TEXT NOT NULL,
          payload        JSONB NOT NULL,
          payload_sha256 TEXT NOT NULL,
          created_at_ms  BIGINT NOT NULL,
          CONSTRAINT trading_manual_event_id_check CHECK (
            event_id ~ '^[0-9a-f]{64}$' AND payload_sha256 ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT trading_manual_event_index_check CHECK (event_index > 0),
          CONSTRAINT trading_manual_event_kind_check CHECK (event_kind IN (
            'SESSION_CREATED', 'STRATEGY_SELECTED', 'TRADE_MODIFIED',
            'HIGH_RISK_ACKNOWLEDGED', 'TRADE_CONFIRMED', 'TRADE_CANCELLED',
            'ORDER_FENCED', 'ORDER_SUBMITTED', 'ORDER_REJECTED', 'PROTECTION_REJECTED',
            'ORDER_AMBIGUOUS', 'ORDER_RECONCILED',
            'POSITION_OPENED', 'TP_CREATED', 'SL_CREATED', 'POSITION_CLOSED'
          )),
          CONSTRAINT trading_manual_event_time_check CHECK (created_at_ms > 0),
          UNIQUE (session_id, event_index)
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_manual_events_append_only "
        "BEFORE UPDATE OR DELETE ON trading_manual_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_trading_account_bindings_append_only "
        "BEFORE UPDATE OR DELETE ON trading_account_bindings "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )
    op.execute(
        """
        CREATE TABLE trading_manual_notifications (
          notification_id   TEXT PRIMARY KEY,
          event_id           TEXT NOT NULL UNIQUE
            REFERENCES trading_manual_events(event_id) ON DELETE RESTRICT,
          session_id         UUID NOT NULL
            REFERENCES trading_manual_sessions(session_id) ON DELETE RESTRICT,
          source_message_id  BIGINT NOT NULL,
          notification_kind TEXT NOT NULL,
          payload            JSONB NOT NULL,
          state              TEXT NOT NULL DEFAULT 'PENDING',
          provider_message_id BIGINT,
          attempted_at_ms    BIGINT,
          settled_at_ms      BIGINT,
          error_code         TEXT,
          interaction_state  TEXT NOT NULL DEFAULT 'PENDING',
          interaction_attempted_at_ms BIGINT,
          interaction_settled_at_ms BIGINT,
          interaction_error_code TEXT,
          reply_state        TEXT NOT NULL DEFAULT 'PENDING',
          reply_attempted_at_ms BIGINT,
          reply_settled_at_ms BIGINT,
          reply_error_code   TEXT,
          CONSTRAINT trading_manual_notification_id_check CHECK (notification_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_manual_notification_source_check CHECK (source_message_id > 0),
          CONSTRAINT trading_manual_notification_kind_check CHECK (notification_kind IN (
            'ORDER_REJECTED', 'PROTECTION_REJECTED', 'ORDER_AMBIGUOUS', 'POSITION_OPENED',
            'TP_CREATED', 'SL_CREATED', 'POSITION_CLOSED'
          )),
          CONSTRAINT trading_manual_notification_state_check CHECK (
            state IN ('PENDING', 'SENDING', 'SENT', 'AMBIGUOUS')
          ),
          CONSTRAINT trading_manual_notification_interaction_state_check CHECK (
            interaction_state IN ('PENDING', 'SENDING', 'SENT', 'AMBIGUOUS', 'SKIPPED')
          ),
          CONSTRAINT trading_manual_notification_reply_state_check CHECK (
            reply_state IN ('PENDING', 'SENDING', 'SENT', 'AMBIGUOUS')
          ),
          CONSTRAINT trading_manual_notification_shape_check CHECK (
            (state = 'PENDING' AND attempted_at_ms IS NULL AND settled_at_ms IS NULL
              AND provider_message_id IS NULL AND error_code IS NULL)
            OR (state = 'SENDING' AND attempted_at_ms IS NOT NULL AND settled_at_ms IS NULL
              AND provider_message_id IS NULL AND error_code IS NULL)
            OR (state = 'SENT' AND attempted_at_ms IS NOT NULL AND settled_at_ms IS NOT NULL
              AND provider_message_id IS NOT NULL AND error_code IS NULL)
            OR (state = 'AMBIGUOUS' AND attempted_at_ms IS NOT NULL AND settled_at_ms IS NOT NULL
              AND provider_message_id IS NULL AND error_code IS NOT NULL)
          ),
          CONSTRAINT trading_manual_notification_interaction_shape_check CHECK (
            (interaction_state IN ('PENDING', 'SKIPPED')
              AND interaction_attempted_at_ms IS NULL AND interaction_settled_at_ms IS NULL
              AND interaction_error_code IS NULL)
            OR (interaction_state = 'SENDING'
              AND interaction_attempted_at_ms IS NOT NULL AND interaction_settled_at_ms IS NULL
              AND interaction_error_code IS NULL)
            OR (interaction_state = 'SENT'
              AND interaction_attempted_at_ms IS NOT NULL AND interaction_settled_at_ms IS NOT NULL
              AND interaction_error_code IS NULL)
            OR (interaction_state = 'AMBIGUOUS'
              AND interaction_attempted_at_ms IS NOT NULL AND interaction_settled_at_ms IS NOT NULL
              AND interaction_error_code IS NOT NULL)
          ),
          CONSTRAINT trading_manual_notification_reply_shape_check CHECK (
            (reply_state = 'PENDING'
              AND reply_attempted_at_ms IS NULL AND reply_settled_at_ms IS NULL
              AND reply_error_code IS NULL)
            OR (reply_state = 'SENDING'
              AND reply_attempted_at_ms IS NOT NULL AND reply_settled_at_ms IS NULL
              AND reply_error_code IS NULL)
            OR (reply_state = 'SENT'
              AND reply_attempted_at_ms IS NOT NULL AND reply_settled_at_ms IS NOT NULL
              AND reply_error_code IS NULL AND provider_message_id IS NOT NULL)
            OR (reply_state = 'AMBIGUOUS'
              AND reply_attempted_at_ms IS NOT NULL AND reply_settled_at_ms IS NOT NULL
              AND reply_error_code IS NOT NULL AND provider_message_id IS NULL)
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_manual_intents (
          intent_id                    TEXT PRIMARY KEY,
          session_id                   UUID NOT NULL UNIQUE
            REFERENCES trading_manual_sessions(session_id) ON DELETE RESTRICT,
          account_ref                  TEXT NOT NULL
            REFERENCES trading_account_bindings(account_ref) ON DELETE RESTRICT,
          payload                      JSONB NOT NULL,
          state                        TEXT NOT NULL DEFAULT 'PENDING',
          execution_plan               JSONB,
          execution_setting_attempted_at_ms BIGINT,
          execution_setting_applied_at_ms BIGINT,
          entry_client_order_id        TEXT UNIQUE,
          entry_fenced_at_ms           BIGINT,
          entry_attempted_at_ms         BIGINT,
          entry_submitted_at_ms        BIGINT,
          entry_receipt                 JSONB,
          take_profit_client_order_id  TEXT UNIQUE,
          take_profit_fenced_at_ms      BIGINT,
          take_profit_attempted_at_ms    BIGINT,
          take_profit_submitted_at_ms   BIGINT,
          take_profit_receipt           JSONB,
          stop_loss_client_order_id    TEXT UNIQUE,
          stop_loss_fenced_at_ms        BIGINT,
          stop_loss_attempted_at_ms      BIGINT,
          stop_loss_submitted_at_ms     BIGINT,
          stop_loss_receipt             JSONB,
          outcome                      JSONB,
          updated_at_ms                BIGINT NOT NULL,
          CONSTRAINT trading_manual_intent_id_check CHECK (intent_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_manual_intent_state_check CHECK (state IN (
            'PENDING', 'SUBMITTING', 'AMBIGUOUS', 'OPEN', 'EXPOSED', 'TERMINAL'
          )),
          CONSTRAINT trading_manual_intent_fence_check CHECK (
            (state = 'PENDING' AND execution_plan IS NULL
              AND execution_setting_attempted_at_ms IS NULL
              AND execution_setting_applied_at_ms IS NULL
              AND entry_client_order_id IS NULL AND entry_fenced_at_ms IS NULL
              AND entry_attempted_at_ms IS NULL
              AND entry_submitted_at_ms IS NULL AND entry_receipt IS NULL AND outcome IS NULL)
            OR
            (state = 'TERMINAL' AND execution_plan IS NULL
              AND execution_setting_attempted_at_ms IS NULL
              AND execution_setting_applied_at_ms IS NULL
              AND entry_client_order_id IS NULL AND entry_fenced_at_ms IS NULL
              AND entry_attempted_at_ms IS NULL
              AND entry_submitted_at_ms IS NULL AND entry_receipt IS NULL)
            OR
            ((state IN ('SUBMITTING', 'AMBIGUOUS', 'OPEN', 'EXPOSED')
                OR (state = 'TERMINAL' AND execution_plan IS NOT NULL))
              AND execution_plan IS NOT NULL
              AND entry_client_order_id IS NOT NULL AND entry_fenced_at_ms IS NOT NULL)
          ),
          CONSTRAINT trading_manual_intent_outcome_check CHECK ((
            (state IN ('PENDING', 'SUBMITTING') AND outcome IS NULL)
            OR (
              state IN ('AMBIGUOUS', 'OPEN', 'EXPOSED', 'TERMINAL')
              AND jsonb_typeof(outcome) = 'object'
              AND outcome ->> 'outcome_version' = 'manual_trade_outcome_v1'
              AND outcome ?& ARRAY[
                'outcome_version', 'state', 'leg', 'error_code',
                'entry', 'take_profit', 'stop_loss'
              ]
              AND outcome - ARRAY[
                'outcome_version', 'state', 'leg', 'error_code',
                'entry', 'take_profit', 'stop_loss'
              ] = '{}'::jsonb
              AND (
                (state = 'OPEN' AND outcome ->> 'state' = 'open'
                  AND outcome -> 'leg' = 'null'::jsonb
                  AND outcome -> 'error_code' = 'null'::jsonb
                  AND jsonb_typeof(outcome -> 'entry') = 'object'
                  AND (outcome -> 'entry') ?& ARRAY[
                    'client_id', 'provider_id', 'status', 'executed_quantity', 'average_price'
                  ]
                  AND (outcome -> 'entry') - ARRAY[
                    'client_id', 'provider_id', 'status', 'executed_quantity', 'average_price'
                  ] = '{}'::jsonb
                  AND jsonb_typeof(outcome -> 'entry' -> 'client_id') = 'string'
                  AND jsonb_typeof(outcome -> 'entry' -> 'provider_id') = 'string'
                  AND jsonb_typeof(outcome -> 'entry' -> 'status') = 'string'
                  AND jsonb_typeof(outcome -> 'entry' -> 'executed_quantity') IN ('string', 'null')
                  AND jsonb_typeof(outcome -> 'entry' -> 'average_price') IN ('string', 'null')
                  AND jsonb_typeof(outcome -> 'take_profit') = 'object'
                  AND (outcome -> 'take_profit') ?& ARRAY[
                    'client_id', 'provider_id', 'status', 'executed_quantity', 'average_price'
                  ]
                  AND (outcome -> 'take_profit') - ARRAY[
                    'client_id', 'provider_id', 'status', 'executed_quantity', 'average_price'
                  ] = '{}'::jsonb
                  AND jsonb_typeof(outcome -> 'take_profit' -> 'client_id') = 'string'
                  AND jsonb_typeof(outcome -> 'take_profit' -> 'provider_id') = 'string'
                  AND jsonb_typeof(outcome -> 'take_profit' -> 'status') = 'string'
                  AND jsonb_typeof(outcome -> 'take_profit' -> 'executed_quantity') IN ('string', 'null')
                  AND jsonb_typeof(outcome -> 'take_profit' -> 'average_price') IN ('string', 'null')
                  AND jsonb_typeof(outcome -> 'stop_loss') = 'object'
                  AND (outcome -> 'stop_loss') ?& ARRAY[
                    'client_id', 'provider_id', 'status', 'executed_quantity', 'average_price'
                  ]
                  AND (outcome -> 'stop_loss') - ARRAY[
                    'client_id', 'provider_id', 'status', 'executed_quantity', 'average_price'
                  ] = '{}'::jsonb
                  AND jsonb_typeof(outcome -> 'stop_loss' -> 'client_id') = 'string'
                  AND jsonb_typeof(outcome -> 'stop_loss' -> 'provider_id') = 'string'
                  AND jsonb_typeof(outcome -> 'stop_loss' -> 'status') = 'string'
                  AND jsonb_typeof(outcome -> 'stop_loss' -> 'executed_quantity') IN ('string', 'null')
                  AND jsonb_typeof(outcome -> 'stop_loss' -> 'average_price') IN ('string', 'null'))
                OR
                (state = 'AMBIGUOUS' AND outcome ->> 'state' = 'ambiguous'
                  AND jsonb_typeof(outcome -> 'leg') = 'string'
                  AND outcome ->> 'leg' IN ('execution_setting', 'entry', 'take_profit', 'stop_loss')
                  AND jsonb_typeof(outcome -> 'error_code') = 'string'
                  AND length(outcome ->> 'error_code') BETWEEN 1 AND 160
                  AND outcome -> 'entry' = 'null'::jsonb
                  AND outcome -> 'take_profit' = 'null'::jsonb
                  AND outcome -> 'stop_loss' = 'null'::jsonb)
                OR
                (state = 'TERMINAL' AND outcome ->> 'state' = 'rejected'
                  AND jsonb_typeof(outcome -> 'leg') = 'string'
                  AND outcome ->> 'leg' IN ('execution_setting', 'entry', 'take_profit', 'stop_loss')
                  AND jsonb_typeof(outcome -> 'error_code') = 'string'
                  AND length(outcome ->> 'error_code') BETWEEN 1 AND 160
                  AND outcome -> 'entry' = 'null'::jsonb
                  AND outcome -> 'take_profit' = 'null'::jsonb
                  AND outcome -> 'stop_loss' = 'null'::jsonb)
                OR
                (state = 'EXPOSED' AND outcome ->> 'state' = 'exposed'
                  AND jsonb_typeof(outcome -> 'leg') = 'string'
                  AND outcome ->> 'leg' IN ('take_profit', 'stop_loss')
                  AND jsonb_typeof(outcome -> 'error_code') = 'string'
                  AND length(outcome ->> 'error_code') BETWEEN 1 AND 160
                  AND outcome -> 'entry' = 'null'::jsonb
                  AND outcome -> 'take_profit' = 'null'::jsonb
                  AND outcome -> 'stop_loss' = 'null'::jsonb)
              )
            )
          ) IS TRUE),
          CONSTRAINT trading_manual_intent_submission_check CHECK (
            (execution_setting_attempted_at_ms IS NULL
              OR execution_setting_attempted_at_ms >= entry_fenced_at_ms)
            AND (execution_setting_applied_at_ms IS NULL
              OR execution_setting_applied_at_ms >= entry_fenced_at_ms)
            AND (entry_attempted_at_ms IS NULL OR entry_attempted_at_ms >= entry_fenced_at_ms)
            AND (entry_submitted_at_ms IS NULL OR entry_submitted_at_ms >= entry_attempted_at_ms)
            AND (take_profit_attempted_at_ms IS NULL
              OR take_profit_attempted_at_ms >= take_profit_fenced_at_ms)
            AND (take_profit_submitted_at_ms IS NULL
              OR take_profit_submitted_at_ms >= take_profit_attempted_at_ms)
            AND (stop_loss_attempted_at_ms IS NULL
              OR stop_loss_attempted_at_ms >= stop_loss_fenced_at_ms)
            AND (stop_loss_submitted_at_ms IS NULL
              OR stop_loss_submitted_at_ms >= stop_loss_attempted_at_ms)
          ),
          CONSTRAINT trading_manual_intent_receipt_check CHECK (
            (entry_submitted_at_ms IS NULL OR entry_attempted_at_ms IS NOT NULL)
            AND (take_profit_submitted_at_ms IS NULL OR take_profit_attempted_at_ms IS NOT NULL)
            AND (stop_loss_submitted_at_ms IS NULL OR stop_loss_attempted_at_ms IS NOT NULL)
            AND
            (entry_receipt IS NULL) = (entry_submitted_at_ms IS NULL)
            AND (take_profit_receipt IS NULL) = (take_profit_submitted_at_ms IS NULL)
            AND (stop_loss_receipt IS NULL) = (stop_loss_submitted_at_ms IS NULL)
            AND (state <> 'OPEN' OR (
              execution_setting_applied_at_ms IS NOT NULL
              AND entry_receipt IS NOT NULL
              AND take_profit_receipt IS NOT NULL
              AND stop_loss_receipt IS NOT NULL
            ))
            AND (state <> 'EXPOSED' OR entry_receipt IS NOT NULL)
          ),
          CONSTRAINT trading_manual_intent_time_check CHECK (updated_at_ms > 0)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_trading_manual_active_account_symbol
          ON trading_manual_intents (account_ref, ((payload -> 'source' ->> 'base_symbol')))
         WHERE state IN ('SUBMITTING', 'AMBIGUOUS', 'OPEN', 'EXPOSED')
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_manual_identity_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_TABLE_NAME = 'trading_manual_sessions' THEN
            IF NEW.session_id IS DISTINCT FROM OLD.session_id
              OR NEW.source_sha256 IS DISTINCT FROM OLD.source_sha256
              OR NEW.source IS DISTINCT FROM OLD.source
              OR NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id
              OR NEW.chat_id IS DISTINCT FROM OLD.chat_id
              OR NEW.source_message_id IS DISTINCT FROM OLD.source_message_id
              OR NEW.created_at_ms IS DISTINCT FROM OLD.created_at_ms
            THEN
              RAISE EXCEPTION 'trading_manual_session_identity_mutation_forbidden';
            END IF;
          ELSIF TG_TABLE_NAME = 'trading_manual_intents' THEN
            IF NEW.intent_id IS DISTINCT FROM OLD.intent_id
              OR NEW.session_id IS DISTINCT FROM OLD.session_id
              OR NEW.account_ref IS DISTINCT FROM OLD.account_ref
              OR NEW.payload IS DISTINCT FROM OLD.payload
            THEN
              RAISE EXCEPTION 'trading_manual_intent_identity_mutation_forbidden';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_manual_sessions_identity "
        "BEFORE UPDATE ON trading_manual_sessions "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_manual_identity_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_trading_manual_intents_identity "
        "BEFORE UPDATE ON trading_manual_intents "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_manual_identity_mutation()"
    )
    for table in (
        "trading_account_bindings",
        "trading_manual_account_snapshots",
        "trading_manual_runtime",
        "trading_manual_telegram_updates",
        "trading_manual_sessions",
        "trading_manual_events",
        "trading_manual_notifications",
        "trading_manual_intents",
    ):
        op.execute(f"REVOKE ALL ON {table} FROM tracefold_workers, tracefold_serve, tracefold_nautilus")
        op.execute(f"GRANT SELECT ON {table} TO tracefold_serve")
    op.execute("GRANT SELECT ON trading_account_bindings TO tracefold_workers, tracefold_nautilus")
    op.execute("GRANT INSERT ON trading_account_bindings TO tracefold_nautilus")
    op.execute("GRANT SELECT ON trading_manual_account_snapshots TO tracefold_workers")
    op.execute("GRANT SELECT, INSERT ON trading_manual_account_snapshots TO tracefold_nautilus")
    op.execute(
        "GRANT UPDATE (venue, equity_usd, observed_at_ms, updated_at_ms) "
        "ON trading_manual_account_snapshots TO tracefold_nautilus"
    )
    op.execute("GRANT SELECT ON trading_manual_runtime TO tracefold_workers")
    op.execute("GRANT UPDATE (next_telegram_update_id, updated_at_ms) ON trading_manual_runtime TO tracefold_workers")
    op.execute("GRANT SELECT ON trading_manual_telegram_updates TO tracefold_workers")
    op.execute(
        "GRANT INSERT (update_id, callback_query_id, actor_user_id, chat_id, message_id, "
        "callback_data, authorized, received_at_ms) ON trading_manual_telegram_updates TO tracefold_workers"
    )
    op.execute(
        "GRANT UPDATE (state, result_code, settled_at_ms) ON trading_manual_telegram_updates TO tracefold_workers"
    )
    op.execute("GRANT SELECT ON trading_manual_sessions TO tracefold_workers, tracefold_nautilus")
    op.execute(
        "GRANT INSERT (session_id, source_sha256, source, actor_user_id, chat_id, source_message_id, "
        "state, created_at_ms, updated_at_ms) ON trading_manual_sessions TO tracefold_workers"
    )
    op.execute(
        "GRANT UPDATE (interaction_message_id, interaction_reply_attempted_at_ms, last_effect_update_id, "
        "last_effect_result_code, state, preset, account_snapshot, recommended, selected, preview, guard, "
        "intent_id, version, updated_at_ms) ON trading_manual_sessions TO tracefold_workers"
    )
    op.execute("GRANT UPDATE (state, version, updated_at_ms) ON trading_manual_sessions TO tracefold_nautilus")
    op.execute("GRANT SELECT, INSERT ON trading_manual_events TO tracefold_workers, tracefold_nautilus")
    op.execute("GRANT SELECT ON trading_manual_notifications TO tracefold_workers, tracefold_nautilus")
    op.execute(
        "GRANT INSERT (notification_id, event_id, session_id, source_message_id, notification_kind, payload) "
        "ON trading_manual_notifications TO tracefold_workers, tracefold_nautilus"
    )
    op.execute(
        "GRANT UPDATE (state, provider_message_id, attempted_at_ms, settled_at_ms, error_code, "
        "interaction_state, interaction_attempted_at_ms, interaction_settled_at_ms, interaction_error_code, "
        "reply_state, reply_attempted_at_ms, reply_settled_at_ms, reply_error_code) "
        "ON trading_manual_notifications TO tracefold_workers"
    )
    op.execute("GRANT SELECT ON trading_manual_intents TO tracefold_workers, tracefold_nautilus")
    op.execute(
        "GRANT INSERT (intent_id, session_id, account_ref, payload, updated_at_ms) "
        "ON trading_manual_intents TO tracefold_workers"
    )
    op.execute(
        """
        GRANT UPDATE (
          state, execution_plan,
          execution_setting_attempted_at_ms, execution_setting_applied_at_ms,
          entry_client_order_id, entry_fenced_at_ms, entry_attempted_at_ms,
          entry_submitted_at_ms, entry_receipt,
          take_profit_client_order_id, take_profit_fenced_at_ms, take_profit_attempted_at_ms,
          take_profit_submitted_at_ms, take_profit_receipt,
          stop_loss_client_order_id, stop_loss_fenced_at_ms, stop_loss_attempted_at_ms,
          stop_loss_submitted_at_ms, stop_loss_receipt,
          outcome, updated_at_ms
        ) ON trading_manual_intents TO tracefold_nautilus
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0330 owns durable manual Telegram trading and cannot be downgraded")
