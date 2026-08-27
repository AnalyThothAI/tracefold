"""Add the one-table TradeIntent handoff and Nautilus execution projection (#283).

Revision ID: 20260827_0315
Revises: 20260827_0314
"""

from __future__ import annotations

from alembic import op

revision = "20260827_0315"
down_revision = "20260827_0314"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE trading_cases
          ADD CONSTRAINT uq_trading_cases_manifest_identity UNIQUE (case_id, manifest_sha256)
        """
    )
    op.execute(
        """
        CREATE TABLE trading_intents (
          intent_id                    TEXT PRIMARY KEY,
          intent_version               TEXT NOT NULL,
          case_id                      TEXT NOT NULL UNIQUE,
          case_manifest_sha256         TEXT NOT NULL,
          intent_policy_sha256         TEXT NOT NULL,
          execution_environment        TEXT NOT NULL,
          instrument_id                TEXT NOT NULL,
          side                         TEXT NOT NULL,
          created_at_ms                BIGINT NOT NULL,
          valid_until_ms               BIGINT NOT NULL,
          reference_price              NUMERIC NOT NULL,
          target_notional_usd          NUMERIC NOT NULL,
          stop_loss_bps                INTEGER NOT NULL,
          max_holding_ms               BIGINT NOT NULL,
          max_entry_drift_bps          INTEGER NOT NULL,
          max_spread_bps               INTEGER NOT NULL,

          engine_identity              TEXT,
          execution_state              TEXT NOT NULL DEFAULT 'PENDING',
          execution_phase              TEXT,
          terminal_outcome             TEXT,
          reason_code                  TEXT,
          entry_client_order_id        TEXT,
          entry_fenced_at_ms            BIGINT,
          stop_client_order_id         TEXT,
          stop_generation              INTEGER,
          stop_submitted_at_ms         BIGINT,
          close_client_order_id        TEXT,
          close_submitted_at_ms        BIGINT,
          actual_quantity              NUMERIC,
          protected_quantity           NUMERIC,
          avg_entry_price              NUMERIC,
          avg_exit_price               NUMERIC,
          position_id                  TEXT,
          protection_order_id          TEXT,
          stop_price                   NUMERIC,
          opened_at_ms                 BIGINT,
          protected_at_ms              BIGINT,
          closed_at_ms                 BIGINT,
          flat_verified_at_ms          BIGINT,
          realized_pnl_amount          NUMERIC,
          realized_pnl_currency        TEXT,
          commissions_by_currency      JSONB,
          updated_at_ms                BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT,

          CONSTRAINT trading_intents_case_manifest_fk FOREIGN KEY (case_id, case_manifest_sha256)
            REFERENCES trading_cases (case_id, manifest_sha256) ON DELETE RESTRICT,
          CONSTRAINT trading_intents_sha256_check CHECK (
            intent_id ~ '^[0-9a-f]{64}$'
            AND case_manifest_sha256 ~ '^[0-9a-f]{64}$'
            AND intent_policy_sha256 ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT trading_intents_policy_identity_check
            CHECK (intent_policy_sha256 = '45702e47bf093ba7c5996eae2186e9e2d1dfee0d9c0a434ced7afa4377286243'),
          CONSTRAINT trading_intents_version_check CHECK (intent_version = 'trade_intent_v1'),
          CONSTRAINT trading_intents_environment_check
            CHECK (execution_environment = 'BINANCE_USDM_DEMO'),
          CONSTRAINT trading_intents_instrument_check
            CHECK (instrument_id = 'SOLUSDT-PERP.BINANCE'),
          CONSTRAINT trading_intents_side_check CHECK (side = 'long'),
          CONSTRAINT trading_intents_expiry_check CHECK (valid_until_ms = created_at_ms + 60000),
          CONSTRAINT trading_intents_money_positive
            CHECK (reference_price > 0 AND target_notional_usd > 0 AND target_notional_usd <= 10),
          CONSTRAINT trading_intents_policy_bounds CHECK (
            stop_loss_bps = 200
            AND max_holding_ms = 180000
            AND max_entry_drift_bps = 25
            AND max_spread_bps = 30
          ),
          CONSTRAINT trading_intents_state_check CHECK (
            execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW', 'TERMINAL')
          ),
          CONSTRAINT trading_intents_phase_check CHECK (
            execution_phase IS NULL OR execution_phase IN ('ENTRY', 'PROTECTION', 'EXIT')
          ),
          CONSTRAINT trading_intents_reason_check CHECK (
            reason_code IS NULL OR reason_code IN (
              'intent_expired', 'runtime_not_ready', 'external_exposure',
              'market_unacceptable', 'quantity_unexecutable', 'risk_denied',
              'entry_outcome_unknown', 'protection_unproven',
              'close_outcome_unknown', 'operator_intervention'
            )
          ),
          CONSTRAINT trading_intents_submission_identity_pairs CHECK (
            (entry_client_order_id IS NULL) = (entry_fenced_at_ms IS NULL)
            AND (stop_client_order_id IS NULL) = (stop_submitted_at_ms IS NULL)
            AND (stop_client_order_id IS NULL) = (stop_generation IS NULL)
            AND (close_client_order_id IS NULL) = (close_submitted_at_ms IS NULL)
          ),
          CONSTRAINT trading_intents_stop_generation_check
            CHECK (stop_generation IS NULL OR stop_generation >= 0),
          CONSTRAINT trading_intents_execution_values_positive CHECK (
            (actual_quantity IS NULL OR actual_quantity > 0)
            AND (protected_quantity IS NULL OR protected_quantity > 0)
            AND (avg_entry_price IS NULL OR avg_entry_price > 0)
            AND (avg_exit_price IS NULL OR avg_exit_price > 0)
            AND (stop_price IS NULL OR stop_price > 0)
          ),
          CONSTRAINT trading_intents_terminal_check CHECK (
            (execution_state = 'TERMINAL' AND terminal_outcome IN ('EXPIRED', 'REJECTED', 'CLOSED_FLAT'))
            OR (execution_state <> 'TERMINAL' AND terminal_outcome IS NULL)
          ),
          CONSTRAINT trading_intents_state_shape_check CHECK (
            (execution_state = 'PENDING'
              AND execution_phase IS NULL
              AND entry_fenced_at_ms IS NULL
              AND reason_code IS NULL)
            OR (execution_state = 'IN_FLIGHT'
              AND execution_phase IS NOT NULL
              AND entry_fenced_at_ms IS NOT NULL)
            OR (execution_state = 'OPEN_PROTECTED'
              AND execution_phase = 'PROTECTION'
              AND entry_fenced_at_ms IS NOT NULL
              AND actual_quantity IS NOT NULL
              AND protected_quantity = actual_quantity
              AND position_id IS NOT NULL
              AND avg_entry_price IS NOT NULL
              AND opened_at_ms IS NOT NULL
              AND stop_client_order_id IS NOT NULL
              AND stop_generation IS NOT NULL
              AND stop_submitted_at_ms IS NOT NULL
              AND protection_order_id IS NOT NULL
              AND stop_price IS NOT NULL
              AND protected_at_ms IS NOT NULL)
            OR (execution_state = 'MANUAL_REVIEW'
              AND entry_fenced_at_ms IS NOT NULL
              AND execution_phase IS NOT NULL
              AND reason_code IN (
                'entry_outcome_unknown', 'protection_unproven',
                'close_outcome_unknown', 'operator_intervention'
              ))
            OR execution_state = 'TERMINAL'
          ),
          CONSTRAINT trading_intents_flat_check CHECK (
            terminal_outcome <> 'CLOSED_FLAT' OR (
              execution_phase = 'EXIT'
              AND entry_fenced_at_ms IS NOT NULL
              AND actual_quantity IS NOT NULL
              AND position_id IS NOT NULL
              AND closed_at_ms IS NOT NULL
              AND flat_verified_at_ms IS NOT NULL
              AND reason_code IS NULL
            )
          ),
          CONSTRAINT trading_intents_expired_unfenced_check CHECK (
            terminal_outcome <> 'EXPIRED' OR (
              entry_fenced_at_ms IS NULL
              AND execution_phase IS NULL
              AND reason_code = 'intent_expired'
            )
          ),
          CONSTRAINT trading_intents_rejected_flat_check CHECK (
            terminal_outcome <> 'REJECTED' OR (
              actual_quantity IS NULL
              AND opened_at_ms IS NULL
              AND position_id IS NULL
              AND
              reason_code IN (
                'runtime_not_ready', 'external_exposure', 'market_unacceptable',
                'quantity_unexecutable', 'risk_denied'
              )
              AND (entry_fenced_at_ms IS NULL OR flat_verified_at_ms IS NOT NULL)
            )
          ),
          CONSTRAINT trading_intents_commissions_object_check
            CHECK (
              commissions_by_currency IS NULL
              OR (
                jsonb_typeof(commissions_by_currency) = 'object'
                AND octet_length(commissions_by_currency::text) <= 2048
                AND NOT jsonb_path_exists(
                  commissions_by_currency,
                  '$.* ? (@.type() != "string")'
                )
                AND NOT jsonb_path_exists(
                  commissions_by_currency,
                  '$.* ? (!(@ like_regex "^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$"))'
                )
              )
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_trading_intents_one_active
          ON trading_intents ((true))
         WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_trading_intents_one_entry_per_utc_day
          ON trading_intents ((entry_fenced_at_ms / 86400000))
         WHERE entry_fenced_at_ms IS NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE trading_runtime_state
          ADD COLUMN nautilus_heartbeat_at_ms BIGINT,
          ADD COLUMN nautilus_ready BOOLEAN NOT NULL DEFAULT false,
          ADD COLUMN nautilus_readiness_reason TEXT,
          ADD COLUMN nautilus_unexpected_exposure BOOLEAN NOT NULL DEFAULT false
        """
    )

    op.execute("REVOKE ALL ON trading_intents FROM tracefold_workers, tracefold_serve, tracefold_nautilus")
    op.execute("GRANT SELECT ON trading_intents TO tracefold_workers")
    op.execute(
        """
        GRANT INSERT (
          intent_id, intent_version, case_id, case_manifest_sha256, intent_policy_sha256,
          execution_environment, instrument_id, side, created_at_ms, valid_until_ms,
          reference_price, target_notional_usd, stop_loss_bps, max_holding_ms,
          max_entry_drift_bps, max_spread_bps
        ) ON trading_intents TO tracefold_workers
        """
    )
    op.execute("GRANT SELECT ON trading_intents TO tracefold_serve")
    op.execute("GRANT SELECT ON trading_intents TO tracefold_nautilus")
    op.execute(
        """
        GRANT UPDATE (
          engine_identity, execution_state, execution_phase, terminal_outcome, reason_code,
          entry_client_order_id, entry_fenced_at_ms,
          stop_client_order_id, stop_submitted_at_ms, close_client_order_id, close_submitted_at_ms,
          stop_generation, actual_quantity, protected_quantity, avg_entry_price, avg_exit_price,
          position_id, protection_order_id,
          stop_price, opened_at_ms, protected_at_ms, closed_at_ms, flat_verified_at_ms,
          realized_pnl_amount, realized_pnl_currency, commissions_by_currency, updated_at_ms
        ) ON trading_intents TO tracefold_nautilus
        """
    )
    op.execute(
        """
        GRANT SELECT (id, control, nautilus_heartbeat_at_ms, nautilus_ready,
                      nautilus_readiness_reason, nautilus_unexpected_exposure)
          ON trading_runtime_state TO tracefold_nautilus
        """
    )
    op.execute(
        """
        GRANT UPDATE (nautilus_heartbeat_at_ms, nautilus_ready,
                      nautilus_readiness_reason, nautilus_unexpected_exposure, updated_at_ms)
          ON trading_runtime_state TO tracefold_nautilus
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260827_0315 owns immutable capital intents and cannot be downgraded")
