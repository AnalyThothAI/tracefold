"""One manual EVM wallet execution lane shared by all onchain routes (#370).

Revision ID: 20260829_0337
Revises: 20260829_0336
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0337"
down_revision = "20260829_0336"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trading_onchain_execution_intents (
          execution_id            UUID PRIMARY KEY,
          session_id              UUID NOT NULL UNIQUE
            REFERENCES trading_onchain_analysis_sessions(session_id),
          actor_user_id           BIGINT NOT NULL,
          chat_id                 BIGINT NOT NULL,
          interaction_message_id  BIGINT NOT NULL,
          provider                TEXT NOT NULL,
          wallet_address          TEXT NOT NULL,
          wallet_fingerprint      TEXT NOT NULL,
          request                 JSONB NOT NULL,
          quote                   JSONB NOT NULL,
          state                   TEXT NOT NULL DEFAULT 'AWAITING_CONFIRMATION',
          confirmation_update_id  BIGINT UNIQUE,
          plan                    JSONB,
          error_code              TEXT,
          created_at_ms           BIGINT NOT NULL,
          confirmed_at_ms         BIGINT,
          updated_at_ms           BIGINT NOT NULL,
          CONSTRAINT trading_onchain_execution_provider_check
            CHECK (provider IN ('okx', 'oneinch')),
          CONSTRAINT trading_onchain_execution_wallet_check CHECK (
            wallet_address ~ '^0x[0-9a-f]{40}$'
            AND wallet_fingerprint ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT trading_onchain_execution_json_check CHECK (
            jsonb_typeof(request) = 'object'
            AND jsonb_typeof(quote) = 'object'
            AND (plan IS NULL OR jsonb_typeof(plan) = 'object')
          ),
          CONSTRAINT trading_onchain_execution_state_check CHECK (
            state IN ('AWAITING_CONFIRMATION', 'PENDING', 'CLAIMED',
              'APPROVAL_SUBMITTED', 'SWAP_SUBMITTED', 'CONFIRMED',
              'FAILED', 'AMBIGUOUS', 'CANCELLED')
          ),
          CONSTRAINT trading_onchain_execution_confirmation_check CHECK ((
            (state = 'AWAITING_CONFIRMATION'
              AND confirmation_update_id IS NULL AND confirmed_at_ms IS NULL)
            OR (state <> 'AWAITING_CONFIRMATION'
              AND confirmation_update_id IS NOT NULL AND confirmed_at_ms IS NOT NULL)
          ) IS TRUE),
          CONSTRAINT trading_onchain_execution_error_check CHECK ((
            (state IN ('FAILED', 'AMBIGUOUS') AND length(error_code) BETWEEN 1 AND 100)
            OR (state NOT IN ('FAILED', 'AMBIGUOUS') AND error_code IS NULL)
          ) IS TRUE),
          CONSTRAINT trading_onchain_execution_time_check CHECK (
            created_at_ms > 0 AND updated_at_ms >= created_at_ms
            AND (confirmed_at_ms IS NULL OR confirmed_at_ms >= created_at_ms)
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_onchain_signed_transactions (
          execution_id       UUID NOT NULL
            REFERENCES trading_onchain_execution_intents(execution_id),
          leg                TEXT NOT NULL,
          signed_transaction JSONB NOT NULL,
          transaction_hash   TEXT NOT NULL UNIQUE,
          state              TEXT NOT NULL DEFAULT 'SIGNED',
          receipt            JSONB,
          error_code         TEXT,
          signed_at_ms       BIGINT NOT NULL,
          submitted_at_ms    BIGINT,
          settled_at_ms      BIGINT,
          PRIMARY KEY (execution_id, leg),
          CONSTRAINT trading_onchain_signed_leg_check CHECK (leg IN ('approval', 'swap')),
          CONSTRAINT trading_onchain_signed_payload_check CHECK (
            jsonb_typeof(signed_transaction) = 'object'
            AND transaction_hash ~ '^0x[0-9a-f]{64}$'
            AND signed_transaction ->> 'transaction_hash' = transaction_hash
            AND signed_transaction ->> 'leg' = leg
          ),
          CONSTRAINT trading_onchain_signed_state_check CHECK (state IN (
            'SIGNED', 'SUBMITTED', 'CONFIRMED', 'FAILED', 'AMBIGUOUS'
          )),
          CONSTRAINT trading_onchain_signed_shape_check CHECK ((
            (state = 'SIGNED' AND submitted_at_ms IS NULL AND settled_at_ms IS NULL
              AND receipt IS NULL AND error_code IS NULL)
            OR (state = 'SUBMITTED' AND submitted_at_ms IS NOT NULL
              AND settled_at_ms IS NULL AND receipt IS NULL AND error_code IS NULL)
            OR (state = 'CONFIRMED' AND submitted_at_ms IS NOT NULL
              AND settled_at_ms IS NOT NULL AND jsonb_typeof(receipt) = 'object'
              AND error_code IS NULL)
            OR (state IN ('FAILED', 'AMBIGUOUS') AND settled_at_ms IS NOT NULL
              AND length(error_code) BETWEEN 1 AND 100)
          ) IS TRUE),
          CONSTRAINT trading_onchain_signed_time_check CHECK (
            signed_at_ms > 0
            AND (submitted_at_ms IS NULL OR submitted_at_ms >= signed_at_ms)
            AND (settled_at_ms IS NULL OR settled_at_ms >= signed_at_ms)
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_onchain_executor_runtime (
          id                    SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
          wallet_fingerprint    TEXT NOT NULL CHECK (wallet_fingerprint ~ '^[0-9a-f]{64}$'),
          started_at_ms         BIGINT NOT NULL CHECK (started_at_ms > 0),
          heartbeat_at_ms       BIGINT NOT NULL CHECK (heartbeat_at_ms >= started_at_ms)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_trading_onchain_execution_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          binding_ok BOOLEAN;
        BEGIN
          SELECT TRUE INTO binding_ok
            FROM trading_onchain_analysis_sessions AS session
           WHERE session.session_id = NEW.session_id
             AND session.actor_user_id = NEW.actor_user_id
             AND session.chat_id = NEW.chat_id
             AND session.interaction_message_id = NEW.interaction_message_id
             AND session.interaction_reply_state = 'SENT'
             AND session.state = 'ANALYZED'
             AND session.analysis ->> 'winner_provider' = NEW.provider
             AND NEW.quote ->> 'provider' = NEW.provider
             AND NEW.request ->> 'chain_id' = session.selected_candidate ->> 'chain_id'
             AND NEW.request ->> 'output_contract' = session.selected_candidate ->> 'contract_address'
             AND NEW.quote ->> 'chain_id' = NEW.request ->> 'chain_id'
             AND NEW.quote ->> 'input_contract' = NEW.request ->> 'input_contract'
             AND NEW.quote ->> 'output_contract' = NEW.request ->> 'output_contract'
             AND NEW.quote ->> 'input_amount_raw' = NEW.request ->> 'input_amount_raw';
          IF binding_ok IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'trading_onchain_execution_binding_invalid';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_execution_insert "
        "BEFORE INSERT ON trading_onchain_execution_intents "
        "FOR EACH ROW EXECUTE FUNCTION validate_trading_onchain_execution_insert()"
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_onchain_execution_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          confirmation_ok BOOLEAN;
        BEGIN
          IF ROW(NEW.execution_id, NEW.session_id, NEW.actor_user_id, NEW.chat_id,
                 NEW.interaction_message_id, NEW.provider, NEW.wallet_address,
                 NEW.wallet_fingerprint, NEW.request, NEW.quote, NEW.created_at_ms)
              IS DISTINCT FROM
             ROW(OLD.execution_id, OLD.session_id, OLD.actor_user_id, OLD.chat_id,
                 OLD.interaction_message_id, OLD.provider, OLD.wallet_address,
                 OLD.wallet_fingerprint, OLD.request, OLD.quote, OLD.created_at_ms)
          THEN
            RAISE EXCEPTION 'trading_onchain_execution_identity_mutation_forbidden';
          END IF;
          IF OLD.state IN ('CONFIRMED', 'FAILED', 'AMBIGUOUS', 'CANCELLED')
            AND NEW IS DISTINCT FROM OLD
          THEN
            RAISE EXCEPTION 'trading_onchain_execution_terminal';
          END IF;
          IF current_user = 'tracefold_workers' AND NOT (
            OLD.state = 'AWAITING_CONFIRMATION'
            AND NEW.state IN ('PENDING', 'CANCELLED')
            AND NEW.state IS DISTINCT FROM OLD.state
          ) THEN
            RAISE EXCEPTION 'trading_onchain_execution_workers_transition_forbidden';
          END IF;
          IF OLD.state <> 'AWAITING_CONFIRMATION' AND ROW(
            NEW.confirmation_update_id, NEW.confirmed_at_ms
          ) IS DISTINCT FROM ROW(
            OLD.confirmation_update_id, OLD.confirmed_at_ms
          ) THEN
            RAISE EXCEPTION 'trading_onchain_execution_confirmation_mutation_forbidden';
          END IF;
          IF NEW.plan IS DISTINCT FROM OLD.plan AND NOT (
            OLD.state = 'CLAIMED' AND NEW.state = 'CLAIMED'
            AND OLD.plan IS NULL AND NEW.plan IS NOT NULL
          ) THEN
            RAISE EXCEPTION 'trading_onchain_execution_plan_mutation_forbidden';
          END IF;
          IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
            (OLD.state = 'AWAITING_CONFIRMATION' AND NEW.state IN ('PENDING', 'CANCELLED'))
            OR (OLD.state = 'PENDING' AND NEW.state = 'CLAIMED')
            OR (OLD.state = 'CLAIMED'
              AND NEW.state IN ('APPROVAL_SUBMITTED', 'SWAP_SUBMITTED', 'FAILED', 'AMBIGUOUS'))
            OR (OLD.state = 'APPROVAL_SUBMITTED'
              AND NEW.state IN ('SWAP_SUBMITTED', 'FAILED', 'AMBIGUOUS'))
            OR (OLD.state = 'SWAP_SUBMITTED'
              AND NEW.state IN ('CONFIRMED', 'FAILED', 'AMBIGUOUS'))
          ) THEN
            RAISE EXCEPTION 'trading_onchain_execution_transition_forbidden';
          END IF;
          IF OLD.state = 'AWAITING_CONFIRMATION' AND NEW.state = 'PENDING' THEN
            SELECT TRUE INTO confirmation_ok
              FROM trading_manual_telegram_updates AS telegram_update
             WHERE telegram_update.update_id = NEW.confirmation_update_id
               AND telegram_update.state = 'RECEIVED'
               AND telegram_update.authorized IS TRUE
               AND telegram_update.actor_user_id = NEW.actor_user_id
               AND telegram_update.chat_id = NEW.chat_id
               AND telegram_update.message_id = NEW.interaction_message_id
               AND telegram_update.callback_data = 'tf:o:y:' || NEW.session_id::text;
            IF confirmation_ok IS DISTINCT FROM TRUE THEN
              RAISE EXCEPTION 'trading_onchain_execution_confirmation_invalid';
            END IF;
          END IF;
          IF OLD.state = 'AWAITING_CONFIRMATION' AND NEW.state = 'CANCELLED' THEN
            SELECT TRUE INTO confirmation_ok
              FROM trading_manual_telegram_updates AS telegram_update
             WHERE telegram_update.update_id = NEW.confirmation_update_id
               AND telegram_update.state = 'RECEIVED'
               AND telegram_update.authorized IS TRUE
               AND telegram_update.actor_user_id = NEW.actor_user_id
               AND telegram_update.chat_id = NEW.chat_id
               AND telegram_update.message_id = NEW.interaction_message_id
               AND telegram_update.callback_data = 'tf:o:x:' || NEW.session_id::text;
            IF confirmation_ok IS DISTINCT FROM TRUE THEN
              RAISE EXCEPTION 'trading_onchain_execution_cancellation_invalid';
            END IF;
          END IF;
          IF NEW.state IN ('APPROVAL_SUBMITTED', 'SWAP_SUBMITTED') AND NEW.plan IS NULL THEN
            RAISE EXCEPTION 'trading_onchain_execution_plan_required';
          END IF;
          IF NEW.state = 'APPROVAL_SUBMITTED' AND NOT EXISTS (
            SELECT 1 FROM trading_onchain_signed_transactions
             WHERE execution_id = NEW.execution_id AND leg = 'approval' AND state = 'SUBMITTED'
          ) THEN
            RAISE EXCEPTION 'trading_onchain_execution_approval_submission_missing';
          END IF;
          IF NEW.state = 'SWAP_SUBMITTED' AND NOT EXISTS (
            SELECT 1 FROM trading_onchain_signed_transactions
             WHERE execution_id = NEW.execution_id AND leg = 'swap' AND state = 'SUBMITTED'
          ) THEN
            RAISE EXCEPTION 'trading_onchain_execution_swap_submission_missing';
          END IF;
          IF OLD.state = 'APPROVAL_SUBMITTED' AND NEW.state = 'SWAP_SUBMITTED' AND NOT EXISTS (
            SELECT 1 FROM trading_onchain_signed_transactions
             WHERE execution_id = NEW.execution_id AND leg = 'approval' AND state = 'CONFIRMED'
          ) THEN
            RAISE EXCEPTION 'trading_onchain_execution_approval_confirmation_missing';
          END IF;
          IF OLD.state = 'SWAP_SUBMITTED' AND NEW.state IN ('CONFIRMED', 'FAILED') AND NOT EXISTS (
            SELECT 1 FROM trading_onchain_signed_transactions
             WHERE execution_id = NEW.execution_id AND leg = 'swap'
               AND state = CASE WHEN NEW.state = 'CONFIRMED' THEN 'CONFIRMED' ELSE 'FAILED' END
          ) THEN
            RAISE EXCEPTION 'trading_onchain_execution_swap_settlement_missing';
          END IF;
          IF NEW.updated_at_ms < OLD.updated_at_ms THEN
            RAISE EXCEPTION 'trading_onchain_execution_time_regression_forbidden';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_execution_mutation "
        "BEFORE UPDATE ON trading_onchain_execution_intents "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_onchain_execution_mutation()"
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_onchain_signed_transaction_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.execution_id, NEW.leg, NEW.signed_transaction,
                 NEW.transaction_hash, NEW.signed_at_ms)
              IS DISTINCT FROM
             ROW(OLD.execution_id, OLD.leg, OLD.signed_transaction,
                 OLD.transaction_hash, OLD.signed_at_ms)
          THEN
            RAISE EXCEPTION 'trading_onchain_signed_transaction_identity_mutation_forbidden';
          END IF;
          IF OLD.state IN ('CONFIRMED', 'FAILED', 'AMBIGUOUS') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'trading_onchain_signed_transaction_terminal';
          END IF;
          IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
            (OLD.state = 'SIGNED' AND NEW.state IN ('SUBMITTED', 'AMBIGUOUS'))
            OR (OLD.state = 'SUBMITTED' AND NEW.state IN ('CONFIRMED', 'FAILED', 'AMBIGUOUS'))
          ) THEN
            RAISE EXCEPTION 'trading_onchain_signed_transaction_transition_forbidden';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_signed_transaction_mutation "
        "BEFORE UPDATE ON trading_onchain_signed_transactions "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_onchain_signed_transaction_mutation()"
    )
    op.execute(
        """
        CREATE FUNCTION validate_trading_onchain_signed_transaction_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM trading_onchain_execution_intents AS intent
             WHERE intent.execution_id = NEW.execution_id
               AND intent.state IN ('CLAIMED', 'APPROVAL_SUBMITTED')
               AND intent.plan IS NOT NULL
               AND NEW.signed_transaction ->> 'provider' = intent.provider
               AND (NEW.signed_transaction ->> 'chain_id')::bigint
                   = (intent.request ->> 'chain_id')::bigint
               AND NEW.signed_transaction ->> 'wallet_address' = intent.wallet_address
               AND NEW.signed_transaction ->> 'raw_transaction' IS NOT NULL
          ) THEN
            RAISE EXCEPTION 'trading_onchain_signed_transaction_binding_invalid';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_signed_transaction_insert "
        "BEFORE INSERT ON trading_onchain_signed_transactions "
        "FOR EACH ROW EXECUTE FUNCTION validate_trading_onchain_signed_transaction_insert()"
    )
    op.execute(
        "REVOKE ALL ON trading_onchain_execution_intents "
        "FROM tracefold_workers, tracefold_serve, tracefold_nautilus, tracefold_onchain"
    )
    op.execute(
        "GRANT SELECT ON trading_onchain_execution_intents TO tracefold_workers, tracefold_serve, tracefold_onchain"
    )
    op.execute(
        "GRANT INSERT (execution_id, session_id, actor_user_id, chat_id, interaction_message_id, "
        "provider, wallet_address, wallet_fingerprint, request, quote, state, created_at_ms, updated_at_ms) "
        "ON trading_onchain_execution_intents TO tracefold_workers"
    )
    op.execute(
        "GRANT UPDATE (state, confirmation_update_id, confirmed_at_ms, updated_at_ms) "
        "ON trading_onchain_execution_intents TO tracefold_workers"
    )
    op.execute(
        "GRANT UPDATE (state, plan, error_code, updated_at_ms) "
        "ON trading_onchain_execution_intents TO tracefold_onchain"
    )
    op.execute(
        "REVOKE ALL ON trading_onchain_signed_transactions "
        "FROM tracefold_workers, tracefold_serve, tracefold_nautilus, tracefold_onchain"
    )
    op.execute(
        "GRANT SELECT (execution_id, leg, transaction_hash, state) "
        "ON trading_onchain_signed_transactions TO tracefold_workers, tracefold_serve"
    )
    op.execute("GRANT SELECT ON trading_onchain_signed_transactions TO tracefold_onchain")
    op.execute(
        "GRANT INSERT (execution_id, leg, signed_transaction, transaction_hash, state, signed_at_ms) "
        "ON trading_onchain_signed_transactions TO tracefold_onchain"
    )
    op.execute(
        "GRANT UPDATE (state, receipt, error_code, submitted_at_ms, settled_at_ms) "
        "ON trading_onchain_signed_transactions TO tracefold_onchain"
    )
    op.execute(
        "REVOKE ALL ON trading_onchain_executor_runtime "
        "FROM tracefold_workers, tracefold_serve, tracefold_nautilus, tracefold_onchain"
    )
    op.execute(
        "GRANT SELECT ON trading_onchain_executor_runtime TO tracefold_workers, tracefold_serve, tracefold_onchain"
    )
    op.execute(
        "GRANT INSERT (id, wallet_fingerprint, started_at_ms, heartbeat_at_ms), "
        "UPDATE (wallet_fingerprint, started_at_ms, heartbeat_at_ms) "
        "ON trading_onchain_executor_runtime TO tracefold_onchain"
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0337 owns manual onchain wallet execution and cannot be downgraded")
