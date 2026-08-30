"""Private Telegram manual positions, close requests, and test-notional ceiling.

Revision ID: 20260829_0340
Revises: 20260829_0339
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0340"
down_revision = "20260829_0339"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trading_manual_intents ADD CONSTRAINT "
        "trading_manual_test_notional_cap_check CHECK ("
        "payload -> 'source' ->> 'news_event_id' NOT LIKE 'development-test:%' "
        "OR (payload -> 'selected' ->> 'notional_usd')::numeric <= 200)"
    )
    op.execute(
        """
        CREATE TABLE trading_manual_positions (
          intent_id             TEXT PRIMARY KEY
            REFERENCES trading_manual_intents(intent_id) ON DELETE RESTRICT,
          session_id            UUID NOT NULL UNIQUE
            REFERENCES trading_manual_sessions(session_id) ON DELETE RESTRICT,
          account_ref           TEXT NOT NULL
            REFERENCES trading_account_bindings(account_ref) ON DELETE RESTRICT,
          symbol                TEXT NOT NULL,
          side                  TEXT NOT NULL,
          state                 TEXT NOT NULL,
          quantity              NUMERIC NOT NULL,
          entry_price           NUMERIC NOT NULL,
          mark_price            NUMERIC NOT NULL,
          unrealized_pnl_usd    NUMERIC NOT NULL,
          leverage              INTEGER NOT NULL,
          liquidation_price     NUMERIC,
          take_profit_price     NUMERIC NOT NULL,
          stop_loss_price       NUMERIC NOT NULL,
          opened_at_ms          BIGINT NOT NULL,
          observed_at_ms        BIGINT NOT NULL,
          closed_at_ms          BIGINT,
          exit_reason           TEXT,
          exit_price            NUMERIC,
          realized_pnl_usd      NUMERIC,
          take_profit_cancel_attempted_at_ms BIGINT,
          take_profit_cancelled_at_ms BIGINT,
          stop_loss_cancel_attempted_at_ms BIGINT,
          stop_loss_cancelled_at_ms BIGINT,
          last_error_code       TEXT,
          version               INTEGER NOT NULL DEFAULT 1,
          CONSTRAINT trading_manual_position_symbol_check CHECK (symbol ~ '^[A-Z0-9]{2,40}$'),
          CONSTRAINT trading_manual_position_side_check CHECK (side IN ('long', 'short')),
          CONSTRAINT trading_manual_position_state_check CHECK (
            state IN ('OPEN', 'EXPOSED', 'CLOSING', 'CLOSED', 'MANUAL_REVIEW')
          ),
          CONSTRAINT trading_manual_position_values_check CHECK (
            quantity >= 0 AND entry_price > 0 AND mark_price > 0
            AND leverage BETWEEN 1 AND 125
            AND (liquidation_price IS NULL OR liquidation_price >= 0)
            AND take_profit_price > 0 AND stop_loss_price > 0
          ),
          CONSTRAINT trading_manual_position_time_check CHECK (
            opened_at_ms > 0 AND observed_at_ms >= opened_at_ms
            AND (closed_at_ms IS NULL OR closed_at_ms >= opened_at_ms)
          ),
          CONSTRAINT trading_manual_position_closed_shape_check CHECK (
            (state = 'CLOSED' AND quantity = 0 AND closed_at_ms IS NOT NULL AND exit_reason IS NOT NULL)
            OR (state <> 'CLOSED' AND closed_at_ms IS NULL)
          ),
          CONSTRAINT trading_manual_position_cancel_time_check CHECK (
            (take_profit_cancelled_at_ms IS NULL OR (
              take_profit_cancel_attempted_at_ms IS NOT NULL
              AND take_profit_cancelled_at_ms >= take_profit_cancel_attempted_at_ms
            )) AND (stop_loss_cancelled_at_ms IS NULL OR (
              stop_loss_cancel_attempted_at_ms IS NOT NULL
              AND stop_loss_cancelled_at_ms >= stop_loss_cancel_attempted_at_ms
            ))
          ),
          CONSTRAINT trading_manual_position_error_check CHECK (
            last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 160
          ),
          CONSTRAINT trading_manual_position_version_check CHECK (version > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_manual_close_orders (
          close_id          TEXT PRIMARY KEY,
          intent_id         TEXT NOT NULL
            REFERENCES trading_manual_positions(intent_id) ON DELETE RESTRICT,
          session_id        UUID NOT NULL
            REFERENCES trading_manual_sessions(session_id) ON DELETE RESTRICT,
          requested_bps     INTEGER NOT NULL,
          client_order_id   TEXT NOT NULL UNIQUE,
          state             TEXT NOT NULL DEFAULT 'PENDING',
          target_quantity   NUMERIC,
          attempted_at_ms   BIGINT,
          receipt           JSONB,
          reconciled_at_ms  BIGINT,
          error_code        TEXT,
          requested_at_ms   BIGINT NOT NULL,
          updated_at_ms     BIGINT NOT NULL,
          CONSTRAINT trading_manual_close_id_check CHECK (close_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_manual_close_bps_check CHECK (requested_bps IN (3000, 5000, 10000)),
          CONSTRAINT trading_manual_close_client_check CHECK (
            length(client_order_id) BETWEEN 1 AND 36
          ),
          CONSTRAINT trading_manual_close_state_check CHECK (
            state IN ('PENDING', 'SUBMITTING', 'FILLED', 'AMBIGUOUS', 'REJECTED')
          ),
          CONSTRAINT trading_manual_close_shape_check CHECK (
            (state = 'PENDING' AND target_quantity IS NULL AND attempted_at_ms IS NULL
              AND receipt IS NULL AND error_code IS NULL)
            OR (state = 'SUBMITTING' AND target_quantity > 0 AND attempted_at_ms IS NOT NULL
              AND receipt IS NULL AND error_code IS NULL)
            OR (state = 'FILLED' AND target_quantity > 0 AND attempted_at_ms IS NOT NULL
              AND jsonb_typeof(receipt) = 'object' AND error_code IS NULL)
            OR (state IN ('AMBIGUOUS', 'REJECTED') AND error_code IS NOT NULL)
          ),
          CONSTRAINT trading_manual_close_time_check CHECK (
            requested_at_ms > 0 AND updated_at_ms >= requested_at_ms
            AND (attempted_at_ms IS NULL OR attempted_at_ms >= requested_at_ms)
            AND (reconciled_at_ms IS NULL OR (
              state = 'FILLED' AND reconciled_at_ms >= updated_at_ms
            ))
          )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_trading_manual_close_active "
        "ON trading_manual_close_orders(intent_id) "
        "WHERE state IN ('PENDING', 'SUBMITTING', 'AMBIGUOUS')"
    )
    op.execute(
        "REVOKE ALL ON trading_manual_positions, trading_manual_close_orders "
        "FROM tracefold_workers, tracefold_serve, tracefold_nautilus, tracefold_onchain"
    )
    op.execute(
        "GRANT SELECT ON trading_manual_positions, trading_manual_close_orders "
        "TO tracefold_workers, tracefold_serve, tracefold_nautilus"
    )
    op.execute(
        "GRANT INSERT (close_id, intent_id, session_id, requested_bps, client_order_id, "
        "requested_at_ms, updated_at_ms) ON trading_manual_close_orders TO tracefold_workers"
    )
    op.execute("GRANT INSERT, UPDATE ON trading_manual_positions, trading_manual_close_orders TO tracefold_nautilus")


def downgrade() -> None:
    raise RuntimeError("20260829_0340 owns manual portfolio management and cannot be downgraded")
