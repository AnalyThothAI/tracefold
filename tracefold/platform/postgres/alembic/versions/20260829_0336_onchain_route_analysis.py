"""Independent Telegram onchain resolution and route-analysis sessions (#370).

Revision ID: 20260829_0336
Revises: 20260829_0335
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0336"
down_revision = "20260829_0335"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trading_onchain_analysis_sessions (
          session_id                       UUID PRIMARY KEY,
          sources                          JSONB NOT NULL,
          actor_user_id                    BIGINT NOT NULL,
          chat_id                          BIGINT NOT NULL,
          source_message_id                BIGINT NOT NULL,
          interaction_message_id           BIGINT,
          interaction_reply_attempted_at_ms BIGINT,
          interaction_reply_state           TEXT NOT NULL DEFAULT 'PENDING',
          interaction_reply_error_code      TEXT,
          state                            TEXT NOT NULL,
          selected_ticker                  TEXT,
          candidates                       JSONB NOT NULL DEFAULT '[]'::jsonb,
          selected_candidate               JSONB,
          analysis                         JSONB,
          provider_errors                  JSONB NOT NULL DEFAULT '[]'::jsonb,
          created_at_ms                    BIGINT NOT NULL,
          updated_at_ms                    BIGINT NOT NULL,
          CONSTRAINT trading_onchain_session_sources_check CHECK (
            jsonb_typeof(sources) = 'array' AND jsonb_array_length(sources) BETWEEN 1 AND 4
          ),
          CONSTRAINT trading_onchain_session_identity_check CHECK (
            actor_user_id > 0 AND source_message_id > 0
            AND (interaction_message_id IS NULL OR interaction_message_id > 0)
          ),
          CONSTRAINT trading_onchain_session_state_check CHECK (state IN (
            'AWAITING_TICKER', 'RESOLVING', 'AWAITING_CONTRACT', 'QUOTING',
            'ANALYZED', 'UNAVAILABLE', 'CANCELLED'
          )),
          CONSTRAINT trading_onchain_session_ticker_check CHECK (
            selected_ticker IS NULL OR selected_ticker ~
              '^([A-Z0-9][A-Z0-9._-]{0,19}|0x[0-9a-f]{40})$'
          ),
          CONSTRAINT trading_onchain_session_candidates_check CHECK (
            jsonb_typeof(candidates) = 'array' AND jsonb_array_length(candidates) <= 6
          ),
          CONSTRAINT trading_onchain_session_provider_errors_check CHECK (
            jsonb_typeof(provider_errors) = 'array' AND jsonb_array_length(provider_errors) <= 6
          ),
          CONSTRAINT trading_onchain_session_reply_state_check CHECK (
            (interaction_reply_state IN ('PENDING', 'SENDING', 'SENT', 'AMBIGUOUS')
            AND (
              (interaction_reply_state = 'PENDING'
                AND interaction_reply_attempted_at_ms IS NULL
                AND interaction_message_id IS NULL AND interaction_reply_error_code IS NULL)
              OR (interaction_reply_state = 'SENDING'
                AND interaction_reply_attempted_at_ms IS NOT NULL
                AND interaction_message_id IS NULL AND interaction_reply_error_code IS NULL)
              OR (interaction_reply_state = 'SENT'
                AND interaction_reply_attempted_at_ms IS NOT NULL
                AND interaction_message_id IS NOT NULL AND interaction_reply_error_code IS NULL)
              OR (interaction_reply_state = 'AMBIGUOUS'
                AND interaction_reply_attempted_at_ms IS NOT NULL
                AND interaction_message_id IS NULL
                AND length(interaction_reply_error_code) BETWEEN 1 AND 100)
            )
            ) IS TRUE
          ),
          CONSTRAINT trading_onchain_session_time_check CHECK (
            created_at_ms > 0 AND updated_at_ms >= created_at_ms
            AND (interaction_reply_attempted_at_ms IS NULL
              OR interaction_reply_attempted_at_ms >= created_at_ms)
          ),
          CONSTRAINT trading_onchain_session_shape_check CHECK (
            ((state = 'AWAITING_TICKER'
              AND selected_ticker IS NULL AND candidates = '[]'::jsonb
              AND selected_candidate IS NULL AND analysis IS NULL)
            OR (state = 'RESOLVING'
              AND selected_ticker IS NOT NULL AND candidates = '[]'::jsonb
              AND selected_candidate IS NULL AND analysis IS NULL)
            OR (state = 'AWAITING_CONTRACT'
              AND selected_ticker IS NOT NULL AND jsonb_array_length(candidates) > 0
              AND selected_candidate IS NULL AND analysis IS NULL)
            OR (state = 'QUOTING'
              AND selected_ticker IS NOT NULL AND jsonb_array_length(candidates) > 0
              AND jsonb_typeof(selected_candidate) = 'object' AND analysis IS NULL)
            OR (state = 'ANALYZED'
              AND selected_ticker IS NOT NULL AND jsonb_array_length(candidates) > 0
              AND jsonb_typeof(selected_candidate) = 'object'
              AND jsonb_typeof(analysis) = 'object')
            OR (state = 'UNAVAILABLE' AND selected_ticker IS NOT NULL)
            OR (state = 'CANCELLED')) IS TRUE
          )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_trading_onchain_active_source_actor
          ON trading_onchain_analysis_sessions (chat_id, actor_user_id, source_message_id)
         WHERE state <> 'CANCELLED'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_trading_onchain_interaction_message
          ON trading_onchain_analysis_sessions (chat_id, interaction_message_id)
         WHERE interaction_message_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_trading_onchain_edit_payload(value JSONB) RETURNS BOOLEAN
        LANGUAGE plpgsql IMMUTABLE STRICT AS $$
        DECLARE
          item JSONB;
        BEGIN
          IF jsonb_typeof(value) <> 'object'
            OR NOT (value ?& ARRAY['message_id', 'text', 'keyboard'])
            OR value - ARRAY['message_id', 'text', 'keyboard'] <> '{}'::jsonb
            OR jsonb_typeof(value -> 'message_id') <> 'number'
            OR (value ->> 'message_id')::numeric <= 0
            OR jsonb_typeof(value -> 'text') <> 'string'
            OR length(value ->> 'text') NOT BETWEEN 1 AND 4096
            OR jsonb_typeof(value -> 'keyboard') <> 'array'
            OR jsonb_array_length(value -> 'keyboard') > 8
          THEN
            RETURN FALSE;
          END IF;
          FOR item IN SELECT child FROM jsonb_array_elements(value -> 'keyboard') AS child LOOP
            IF jsonb_typeof(item) <> 'array'
              OR jsonb_array_length(item) <> 2
              OR jsonb_typeof(item -> 0) <> 'string'
              OR length(btrim(item ->> 0)) NOT BETWEEN 1 AND 80
              OR jsonb_typeof(item -> 1) <> 'string'
              OR octet_length(item ->> 1) NOT BETWEEN 1 AND 64
            THEN
              RETURN FALSE;
            END IF;
          END LOOP;
          RETURN TRUE;
        EXCEPTION WHEN OTHERS THEN
          RETURN FALSE;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TABLE trading_onchain_telegram_edit_effects (
          session_id       UUID NOT NULL REFERENCES trading_onchain_analysis_sessions(session_id),
          update_id        BIGINT NOT NULL,
          message_id       BIGINT NOT NULL,
          payload          JSONB NOT NULL,
          result_code      TEXT NOT NULL,
          state            TEXT NOT NULL DEFAULT 'SENDING',
          error_code       TEXT,
          attempted_at_ms  BIGINT NOT NULL,
          settled_at_ms    BIGINT,
          PRIMARY KEY (session_id, update_id),
          CONSTRAINT trading_onchain_edit_effect_identity_check CHECK (
            update_id >= 0 AND message_id > 0
            AND length(result_code) BETWEEN 1 AND 100
            AND attempted_at_ms > 0
          ),
          CONSTRAINT trading_onchain_edit_effect_payload_check CHECK (
            (validate_trading_onchain_edit_payload(payload) IS TRUE
            AND payload ->> 'message_id' = message_id::text
            ) IS TRUE
          ),
          CONSTRAINT trading_onchain_edit_effect_state_check CHECK (
            (state IN ('SENDING', 'SENT', 'AMBIGUOUS') AND (
              (state = 'SENDING' AND error_code IS NULL AND settled_at_ms IS NULL)
              OR (state = 'SENT' AND error_code IS NULL
                AND settled_at_ms IS NOT NULL AND settled_at_ms >= attempted_at_ms)
              OR (state = 'AMBIGUOUS' AND length(error_code) BETWEEN 1 AND 100
                AND settled_at_ms IS NOT NULL AND settled_at_ms >= attempted_at_ms)
            )
            ) IS TRUE
          )
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_trading_onchain_edit_binding() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          binding_ok BOOLEAN;
        BEGIN
          SELECT TRUE
            INTO binding_ok
            FROM trading_onchain_analysis_sessions AS session
            JOIN trading_manual_telegram_updates AS telegram_update
              ON telegram_update.update_id = NEW.update_id
           WHERE session.session_id = NEW.session_id
             AND session.interaction_reply_state = 'SENT'
             AND session.interaction_message_id = NEW.message_id
             AND telegram_update.state = 'RECEIVED'
             AND telegram_update.authorized IS TRUE
             AND telegram_update.actor_user_id = session.actor_user_id
             AND telegram_update.chat_id = session.chat_id
             AND telegram_update.message_id = session.interaction_message_id
             AND telegram_update.callback_data LIKE 'tf:o:%'
             AND right(telegram_update.callback_data, 37) = ':' || session.session_id::text;
          IF binding_ok IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'trading_onchain_edit_effect_binding_invalid';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_edit_effect_binding "
        "BEFORE INSERT ON trading_onchain_telegram_edit_effects "
        "FOR EACH ROW EXECUTE FUNCTION validate_trading_onchain_edit_binding()"
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_onchain_session_identity_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.session_id IS DISTINCT FROM OLD.session_id
            OR NEW.sources IS DISTINCT FROM OLD.sources
            OR NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id
            OR NEW.chat_id IS DISTINCT FROM OLD.chat_id
            OR NEW.source_message_id IS DISTINCT FROM OLD.source_message_id
            OR NEW.created_at_ms IS DISTINCT FROM OLD.created_at_ms
          THEN
            RAISE EXCEPTION 'trading_onchain_session_identity_mutation_forbidden';
          END IF;
          IF OLD.interaction_message_id IS NOT NULL
            AND NEW.interaction_message_id IS DISTINCT FROM OLD.interaction_message_id
          THEN
            RAISE EXCEPTION 'trading_onchain_interaction_message_rebind_forbidden';
          END IF;
          IF OLD.interaction_reply_attempted_at_ms IS NOT NULL
            AND NEW.interaction_reply_attempted_at_ms IS DISTINCT FROM OLD.interaction_reply_attempted_at_ms
          THEN
            RAISE EXCEPTION 'trading_onchain_interaction_attempt_mutation_forbidden';
          END IF;
          IF OLD.interaction_reply_error_code IS NOT NULL
            AND NEW.interaction_reply_error_code IS DISTINCT FROM OLD.interaction_reply_error_code
          THEN
            RAISE EXCEPTION 'trading_onchain_interaction_error_mutation_forbidden';
          END IF;
          IF NEW.interaction_reply_state IS DISTINCT FROM OLD.interaction_reply_state
            AND NOT (
              (OLD.interaction_reply_state = 'PENDING' AND NEW.interaction_reply_state = 'SENDING')
              OR (OLD.interaction_reply_state = 'SENDING'
                AND NEW.interaction_reply_state IN ('SENT', 'AMBIGUOUS'))
            )
          THEN
            RAISE EXCEPTION 'trading_onchain_interaction_reply_transition_forbidden';
          END IF;
          IF OLD.state = 'CANCELLED' AND NEW.state IS DISTINCT FROM OLD.state THEN
            RAISE EXCEPTION 'trading_onchain_cancelled_terminal';
          END IF;
          IF OLD.state = 'CANCELLED' AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'trading_onchain_cancelled_terminal';
          END IF;
          IF NEW.state = OLD.state
            AND ROW(NEW.selected_ticker, NEW.candidates, NEW.selected_candidate,
                    NEW.analysis, NEW.provider_errors)
                IS DISTINCT FROM
                ROW(OLD.selected_ticker, OLD.candidates, OLD.selected_candidate,
                    OLD.analysis, OLD.provider_errors)
          THEN
            RAISE EXCEPTION 'trading_onchain_same_state_business_mutation_forbidden';
          END IF;
          IF NEW.state IS DISTINCT FROM OLD.state
            AND NEW.state <> 'CANCELLED'
            AND NOT (
              (OLD.state = 'AWAITING_TICKER' AND NEW.state = 'RESOLVING')
              OR (OLD.state = 'RESOLVING' AND NEW.state IN ('AWAITING_CONTRACT', 'UNAVAILABLE'))
              OR (OLD.state = 'AWAITING_CONTRACT' AND NEW.state = 'QUOTING')
              OR (OLD.state = 'QUOTING' AND NEW.state IN ('ANALYZED', 'UNAVAILABLE'))
              OR (OLD.state = 'ANALYZED' AND NEW.state = 'QUOTING')
              OR (OLD.state = 'UNAVAILABLE' AND NEW.state = 'RESOLVING')
            )
          THEN
            RAISE EXCEPTION 'trading_onchain_state_transition_forbidden';
          END IF;
          IF NEW.state IS DISTINCT FROM OLD.state AND (
            ROW(NEW.interaction_message_id, NEW.interaction_reply_attempted_at_ms,
                NEW.interaction_reply_state, NEW.interaction_reply_error_code)
              IS DISTINCT FROM
            ROW(OLD.interaction_message_id, OLD.interaction_reply_attempted_at_ms,
                OLD.interaction_reply_state, OLD.interaction_reply_error_code)
            OR (NEW.state = 'CANCELLED' AND
              ROW(NEW.selected_ticker, NEW.candidates, NEW.selected_candidate,
                  NEW.analysis, NEW.provider_errors)
                IS DISTINCT FROM
              ROW(OLD.selected_ticker, OLD.candidates, OLD.selected_candidate,
                  OLD.analysis, OLD.provider_errors))
            OR (OLD.state = 'AWAITING_TICKER' AND NEW.state = 'RESOLVING' AND
              ROW(NEW.candidates, NEW.selected_candidate, NEW.analysis, NEW.provider_errors)
                IS DISTINCT FROM
              ROW(OLD.candidates, OLD.selected_candidate, OLD.analysis, OLD.provider_errors))
            OR (OLD.state = 'RESOLVING' AND NEW.state IN ('AWAITING_CONTRACT', 'UNAVAILABLE')
              AND ROW(NEW.selected_ticker, NEW.selected_candidate, NEW.analysis)
                IS DISTINCT FROM ROW(OLD.selected_ticker, OLD.selected_candidate, OLD.analysis))
            OR (OLD.state = 'AWAITING_CONTRACT' AND NEW.state = 'QUOTING'
              AND ROW(NEW.selected_ticker, NEW.candidates, NEW.analysis, NEW.provider_errors)
                IS DISTINCT FROM ROW(OLD.selected_ticker, OLD.candidates, OLD.analysis, OLD.provider_errors))
            OR (OLD.state = 'QUOTING' AND NEW.state IN ('ANALYZED', 'UNAVAILABLE')
              AND ROW(NEW.selected_ticker, NEW.candidates, NEW.selected_candidate)
                IS DISTINCT FROM ROW(OLD.selected_ticker, OLD.candidates, OLD.selected_candidate))
            OR (OLD.state = 'ANALYZED' AND NEW.state = 'QUOTING'
              AND ROW(NEW.selected_ticker, NEW.candidates, NEW.selected_candidate, NEW.provider_errors)
                IS DISTINCT FROM ROW(OLD.selected_ticker, OLD.candidates, OLD.selected_candidate,
                  OLD.provider_errors))
          ) THEN
            RAISE EXCEPTION 'trading_onchain_transition_field_mutation_forbidden';
          END IF;
          IF NEW.updated_at_ms < OLD.updated_at_ms THEN
            RAISE EXCEPTION 'trading_onchain_session_time_regression_forbidden';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_session_identity "
        "BEFORE UPDATE ON trading_onchain_analysis_sessions "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_onchain_session_identity_mutation()"
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_onchain_edit_effect_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.session_id, NEW.update_id, NEW.message_id, NEW.payload,
                 NEW.result_code, NEW.attempted_at_ms)
              IS DISTINCT FROM
             ROW(OLD.session_id, OLD.update_id, OLD.message_id, OLD.payload,
                 OLD.result_code, OLD.attempted_at_ms)
          THEN
            RAISE EXCEPTION 'trading_onchain_edit_effect_identity_mutation_forbidden';
          END IF;
          IF OLD.state IN ('SENT', 'AMBIGUOUS') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'trading_onchain_edit_effect_terminal';
          END IF;
          IF NEW.state IS DISTINCT FROM OLD.state
            AND NOT (OLD.state = 'SENDING' AND NEW.state IN ('SENT', 'AMBIGUOUS'))
          THEN
            RAISE EXCEPTION 'trading_onchain_edit_effect_transition_forbidden';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_edit_effect_identity "
        "BEFORE UPDATE ON trading_onchain_telegram_edit_effects "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_onchain_edit_effect_mutation()"
    )
    op.execute(
        "REVOKE ALL ON trading_onchain_analysis_sessions FROM tracefold_workers, tracefold_serve, tracefold_nautilus"
    )
    op.execute("GRANT SELECT ON trading_onchain_analysis_sessions TO tracefold_workers, tracefold_serve")
    op.execute(
        "GRANT INSERT (session_id, sources, actor_user_id, chat_id, source_message_id, state, selected_ticker, "
        "candidates, provider_errors, created_at_ms, updated_at_ms) "
        "ON trading_onchain_analysis_sessions TO tracefold_workers"
    )
    op.execute(
        "GRANT UPDATE (interaction_message_id, interaction_reply_attempted_at_ms, state, selected_ticker, "
        "interaction_reply_state, interaction_reply_error_code, candidates, selected_candidate, analysis, "
        "provider_errors, updated_at_ms) "
        "ON trading_onchain_analysis_sessions TO tracefold_workers"
    )
    op.execute(
        "REVOKE ALL ON trading_onchain_telegram_edit_effects "
        "FROM tracefold_workers, tracefold_serve, tracefold_nautilus"
    )
    op.execute("GRANT SELECT ON trading_onchain_telegram_edit_effects TO tracefold_workers, tracefold_serve")
    op.execute(
        "GRANT INSERT (session_id, update_id, message_id, payload, result_code, state, attempted_at_ms) "
        "ON trading_onchain_telegram_edit_effects TO tracefold_workers"
    )
    op.execute(
        "GRANT UPDATE (state, error_code, settled_at_ms) ON trading_onchain_telegram_edit_effects TO tracefold_workers"
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0336 owns independent onchain analysis history and cannot be downgraded")
