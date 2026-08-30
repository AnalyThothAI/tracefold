"""Production V3 capital authority, UTC risk reservation, and arm epoch (#376).

Revision ID: 20260830_0332
Revises: 20260830_0331
"""

from __future__ import annotations

from alembic import op

revision = "20260830_0332"
down_revision = "20260830_0331"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PR 1 has no allowed Intent writer.  Refuse to attach authority semantics to a V3 row that was
    # inserted outside the new reservation transaction, and never invent a receipt for it.
    op.execute(
        """
        LOCK TABLE trading_runtime_state, trading_intents IN SHARE ROW EXCLUSIVE MODE;
        DO $cutover$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM trading_runtime_state WHERE id = 1 AND control = 'PAUSED') THEN
            RAISE EXCEPTION 'trading_capital_authority_cutover_requires_paused';
          END IF;
          IF EXISTS (SELECT 1 FROM trading_intents WHERE intent_version = 'trade_intent_v3') THEN
            RAISE EXCEPTION 'trading_capital_authority_cutover_unowned_v3_intent';
          END IF;
        END
        $cutover$
        """
    )

    op.execute("ALTER TABLE trading_runtime_state ADD COLUMN arm_epoch BIGINT NOT NULL DEFAULT 1")
    op.execute(
        "ALTER TABLE trading_runtime_state ADD CONSTRAINT trading_runtime_arm_epoch_check CHECK (arm_epoch >= 1)"
    )
    op.execute("ALTER TABLE trading_intents ADD COLUMN funding_by_currency JSONB")
    op.execute(
        """
        ALTER TABLE trading_intents ADD CONSTRAINT trading_intents_funding_check CHECK (
          funding_by_currency IS NULL OR (
            jsonb_typeof(funding_by_currency) = 'object'
            AND octet_length(funding_by_currency::text) <= 2048
            AND NOT jsonb_path_exists(
              funding_by_currency,
              '$.keyvalue() ? (!(@.key like_regex "^[A-Z0-9]{1,12}$") '
              '|| @.value.type() != "string" '
              '|| !(@.value like_regex "^-?(0|[1-9][0-9]*)([.][0-9]+)?$"))'
            )
          )
        )
        """
    )
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_reason_check")
    op.execute(
        """
        ALTER TABLE trading_intents ADD CONSTRAINT trading_intents_reason_check CHECK (
          reason_code IS NULL OR reason_code IN (
            'intent_expired', 'runtime_not_ready', 'external_exposure',
            'blacklisted', 'capability_mismatch', 'market_unacceptable',
            'quantity_unexecutable', 'risk_denied',
            'entry_outcome_unknown', 'protection_unproven',
            'close_outcome_unknown', 'settlement_unproven', 'operator_intervention',
            'quote_missing', 'quote_type_invalid', 'quote_instrument_mismatch',
            'quote_book_invalid', 'quote_side_unsupported', 'quote_intent_not_active',
            'quote_intent_expired', 'quote_clock_invalid', 'quote_receive_stale',
            'quote_event_stale', 'quote_source_latency_exceeded', 'quote_future_skew',
            'quote_event_out_of_order', 'quote_spread_exceeded', 'quote_reference_drift_exceeded'
          )
        )
        """
    )
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_state_shape_check")
    op.execute(
        """
        ALTER TABLE trading_intents ADD CONSTRAINT trading_intents_state_shape_check CHECK (
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
              'close_outcome_unknown', 'settlement_unproven', 'operator_intervention'
            ))
          OR execution_state = 'TERMINAL'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE trading_daily_risk_policies (
          risk_policy_sha256 TEXT PRIMARY KEY,
          approved_release TEXT NOT NULL,
          effective_from_ms BIGINT NOT NULL,
          expires_at_ms BIGINT NOT NULL,
          created_at_ms BIGINT NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_risk_policy_sha_check CHECK (risk_policy_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_risk_policy_clock_check CHECK (
            effective_from_ms > 0 AND expires_at_ms > effective_from_ms AND created_at_ms > 0
          ),
          CONSTRAINT trading_risk_policy_payload_check CHECK (
            payload ->> 'risk_policy_version' = 'daily_risk_policy_v1'
            AND payload ->> 'approved_release' = approved_release
            AND (payload ->> 'effective_from_ms')::BIGINT = effective_from_ms
            AND (payload ->> 'expires_at_ms')::BIGINT = expires_at_ms
          )
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_daily_risk_policies_append_only BEFORE UPDATE OR DELETE "
        "ON trading_daily_risk_policies FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )

    op.execute(
        """
        CREATE TABLE trading_production_promotion_grants (
          grant_sha256 TEXT PRIMARY KEY,
          binding TEXT NOT NULL,
          risk_policy_sha256 TEXT NOT NULL REFERENCES trading_daily_risk_policies(risk_policy_sha256)
            ON DELETE RESTRICT,
          issued_at_ms BIGINT NOT NULL,
          review_at_ms BIGINT NOT NULL,
          expires_at_ms BIGINT NOT NULL,
          created_at_ms BIGINT NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_promotion_grant_sha_check CHECK (grant_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_promotion_grant_binding_check
            CHECK (binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')),
          CONSTRAINT trading_promotion_grant_clock_check CHECK (
            issued_at_ms > 0 AND review_at_ms >= issued_at_ms AND expires_at_ms > review_at_ms
          ),
          CONSTRAINT trading_promotion_grant_payload_check CHECK (
            payload ->> 'grant_version' = 'production_promotion_grant_v1'
            AND payload ->> 'scope' = 'canary'
            AND payload ->> 'binding' = binding
            AND payload ->> 'locked_future_result' = 'PROMOTE'
            AND payload ->> 'risk_policy_sha256' = risk_policy_sha256
          )
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_promotion_grants_append_only BEFORE UPDATE OR DELETE "
        "ON trading_production_promotion_grants FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )
    op.execute(
        """
        CREATE TABLE trading_promotion_grant_revocations (
          revocation_sha256 TEXT PRIMARY KEY,
          grant_sha256 TEXT NOT NULL UNIQUE REFERENCES trading_production_promotion_grants(grant_sha256)
            ON DELETE RESTRICT,
          revoked_at_ms BIGINT NOT NULL,
          created_at_ms BIGINT NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_grant_revocation_sha_check CHECK (revocation_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_grant_revocation_clock_check CHECK (revoked_at_ms > 0),
          CONSTRAINT trading_grant_revocation_payload_check CHECK (
            payload ->> 'revocation_version' = 'production_promotion_grant_revocation_v1'
            AND payload ->> 'grant_sha256' = grant_sha256
            AND (payload ->> 'revoked_at_ms')::BIGINT = revoked_at_ms
          )
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_grant_revocations_append_only BEFORE UPDATE OR DELETE "
        "ON trading_promotion_grant_revocations FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )

    op.execute(
        """
        CREATE TABLE trading_operator_arm_receipts (
          arm_receipt_sha256 TEXT PRIMARY KEY,
          arm_epoch BIGINT NOT NULL,
          binding TEXT NOT NULL,
          grant_sha256 TEXT NOT NULL REFERENCES trading_production_promotion_grants(grant_sha256)
            ON DELETE RESTRICT,
          risk_policy_sha256 TEXT NOT NULL REFERENCES trading_daily_risk_policies(risk_policy_sha256)
            ON DELETE RESTRICT,
          armed_at_ms BIGINT NOT NULL,
          expires_at_ms BIGINT NOT NULL,
          created_at_ms BIGINT NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_arm_receipt_sha_check CHECK (arm_receipt_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_arm_epoch_check CHECK (arm_epoch >= 1),
          CONSTRAINT trading_arm_binding_check CHECK (binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')),
          CONSTRAINT trading_arm_clock_check CHECK (armed_at_ms > 0 AND expires_at_ms > armed_at_ms),
          CONSTRAINT trading_arm_payload_check CHECK (
            payload ->> 'arm_version' = 'operator_arm_receipt_v1'
            AND (payload ->> 'arm_epoch')::BIGINT = arm_epoch
            AND payload ->> 'binding' = binding
            AND payload ->> 'grant_sha256' = grant_sha256
            AND payload ->> 'risk_policy_sha256' = risk_policy_sha256
            AND payload ->> 'reconciliation_state' = 'reconciled_flat'
          )
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_operator_arm_receipts_append_only BEFORE UPDATE OR DELETE "
        "ON trading_operator_arm_receipts FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )
    op.execute("ALTER TABLE trading_binding_runtime ADD COLUMN active_arm_receipt_sha256 TEXT")
    op.execute(
        "ALTER TABLE trading_binding_runtime ADD CONSTRAINT trading_binding_active_arm_fk "
        "FOREIGN KEY (active_arm_receipt_sha256) REFERENCES trading_operator_arm_receipts(arm_receipt_sha256) "
        "ON DELETE RESTRICT"
    )

    op.execute(
        """
        CREATE TABLE trading_capital_risk_reservations (
          reservation_sha256 TEXT PRIMARY KEY,
          case_id TEXT NOT NULL UNIQUE REFERENCES trading_cases(case_id) ON DELETE RESTRICT,
          economic_lifecycle_id TEXT NOT NULL UNIQUE,
          binding TEXT NOT NULL,
          settlement_asset TEXT NOT NULL,
          risk_policy_sha256 TEXT NOT NULL REFERENCES trading_daily_risk_policies(risk_policy_sha256)
            ON DELETE RESTRICT,
          grant_sha256 TEXT NOT NULL REFERENCES trading_production_promotion_grants(grant_sha256)
            ON DELETE RESTRICT,
          arm_receipt_sha256 TEXT NOT NULL REFERENCES trading_operator_arm_receipts(arm_receipt_sha256)
            ON DELETE RESTRICT,
          risk_day_start_ms BIGINT NOT NULL,
          risk_day_end_ms BIGINT NOT NULL,
          target_notional NUMERIC NOT NULL,
          planned_risk_amount NUMERIC NOT NULL,
          created_at_ms BIGINT NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_risk_reservation_sha_check CHECK (reservation_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_risk_reservation_binding_check
            CHECK (binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')),
          CONSTRAINT trading_risk_reservation_asset_check CHECK (settlement_asset IN ('USDT', 'USDC')),
          CONSTRAINT trading_risk_reservation_day_check CHECK (
            risk_day_start_ms >= 0 AND risk_day_end_ms = risk_day_start_ms + 86400000
          ),
          CONSTRAINT trading_risk_reservation_amount_check CHECK (
            target_notional > 0 AND target_notional <= 10
            AND planned_risk_amount > 0 AND planned_risk_amount <= target_notional
          ),
          CONSTRAINT trading_risk_reservation_payload_check CHECK (
            payload ->> 'reservation_version' = 'capital_risk_reservation_v1'
            AND payload ->> 'case_id' = case_id
            AND payload ->> 'economic_lifecycle_id' = economic_lifecycle_id
            AND payload ->> 'binding' = binding
            AND payload ->> 'settlement_asset' = settlement_asset
            AND payload ->> 'risk_policy_sha256' = risk_policy_sha256
            AND payload ->> 'grant_sha256' = grant_sha256
            AND payload ->> 'arm_receipt_sha256' = arm_receipt_sha256
          )
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_risk_reservations_append_only BEFORE UPDATE OR DELETE "
        "ON trading_capital_risk_reservations FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )

    op.execute(
        """
        CREATE TABLE trading_capital_authorization_receipts (
          authorization_receipt_sha256 TEXT PRIMARY KEY,
          reservation_sha256 TEXT NOT NULL UNIQUE REFERENCES trading_capital_risk_reservations(reservation_sha256)
            ON DELETE RESTRICT,
          case_id TEXT NOT NULL UNIQUE REFERENCES trading_cases(case_id) ON DELETE RESTRICT,
          binding TEXT NOT NULL,
          created_at_ms BIGINT NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_authorization_receipt_sha_check
            CHECK (authorization_receipt_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_authorization_binding_check
            CHECK (binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')),
          CONSTRAINT trading_authorization_payload_check CHECK (
            payload ->> 'authorization_version' = 'capital_authorization_receipt_v1'
            AND payload ->> 'reservation_sha256' = reservation_sha256
            AND payload ->> 'case_id' = case_id
            AND payload ->> 'binding' = binding
          )
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_authorization_receipts_append_only BEFORE UPDATE OR DELETE "
        "ON trading_capital_authorization_receipts FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )
    op.execute(
        "ALTER TABLE trading_intents ADD CONSTRAINT trading_intents_capital_authorization_fk "
        "FOREIGN KEY (capital_authorization_receipt_sha256) "
        "REFERENCES trading_capital_authorization_receipts(authorization_receipt_sha256) ON DELETE RESTRICT"
    )

    op.execute(
        """
        CREATE TABLE trading_capital_risk_reservation_state (
          reservation_sha256 TEXT PRIMARY KEY REFERENCES trading_capital_risk_reservations(reservation_sha256)
            ON DELETE RESTRICT,
          intent_id TEXT NOT NULL UNIQUE REFERENCES trading_intents(intent_id) ON DELETE RESTRICT,
          status TEXT NOT NULL,
          current_planned_risk_amount NUMERIC NOT NULL,
          attempt_consumed BOOLEAN NOT NULL,
          attempt_day_start_ms BIGINT,
          attempt_day_end_ms BIGINT,
          settlement_known BOOLEAN NOT NULL,
          updated_at_ms BIGINT NOT NULL,
          CONSTRAINT trading_risk_state_status_check CHECK (
            status IN ('RESERVED', 'FENCED', 'OPEN', 'MANUAL_REVIEW', 'RELEASED', 'SETTLED')
          ),
          CONSTRAINT trading_risk_state_amount_check CHECK (current_planned_risk_amount >= 0),
          CONSTRAINT trading_risk_state_attempt_day_check CHECK (
            (NOT attempt_consumed AND attempt_day_start_ms IS NULL AND attempt_day_end_ms IS NULL)
            OR (attempt_consumed AND attempt_day_start_ms >= 0
                AND attempt_day_end_ms = attempt_day_start_ms + 86400000)
          ),
          CONSTRAINT trading_risk_state_terminal_amount_check CHECK (
            status NOT IN ('RELEASED', 'SETTLED') OR current_planned_risk_amount = 0
          ),
          CONSTRAINT trading_risk_state_settlement_check CHECK (
            (status = 'SETTLED' AND settlement_known) OR status <> 'SETTLED'
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_capital_risk_events (
          event_sha256 TEXT PRIMARY KEY,
          reservation_sha256 TEXT NOT NULL REFERENCES trading_capital_risk_reservations(reservation_sha256)
            ON DELETE RESTRICT,
          intent_id TEXT NOT NULL REFERENCES trading_intents(intent_id) ON DELETE RESTRICT,
          event_kind TEXT NOT NULL,
          current_planned_risk_amount NUMERIC NOT NULL,
          attempt_consumed BOOLEAN NOT NULL,
          settlement_asset TEXT,
          realized_loss_amount NUMERIC,
          occurred_at_ms BIGINT NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_risk_event_sha_check CHECK (event_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_risk_event_kind_check CHECK (
            event_kind IN ('RESERVED', 'FENCE_COMMITTED', 'PLANNED_RISK_RELEASED',
                           'EXPOSURE_OPENED', 'MANUAL_REVIEW', 'SETTLED')
          ),
          CONSTRAINT trading_risk_event_amount_check CHECK (current_planned_risk_amount >= 0),
          CONSTRAINT trading_risk_event_settlement_check CHECK (
            (event_kind = 'SETTLED'
              AND settlement_asset IN ('USDT', 'USDC') AND realized_loss_amount >= 0)
            OR (event_kind <> 'SETTLED' AND settlement_asset IS NULL AND realized_loss_amount IS NULL)
          ),
          CONSTRAINT trading_risk_event_payload_check CHECK (
            payload ->> 'event_version' = 'capital_risk_event_v1'
            AND payload ->> 'reservation_sha256' = reservation_sha256
            AND payload ->> 'intent_id' = intent_id
            AND payload ->> 'event_kind' = event_kind
          )
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_capital_risk_events_append_only BEFORE UPDATE OR DELETE "
        "ON trading_capital_risk_events FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )

    for table in (
        "trading_daily_risk_policies",
        "trading_production_promotion_grants",
        "trading_promotion_grant_revocations",
        "trading_operator_arm_receipts",
        "trading_capital_risk_reservations",
        "trading_capital_authorization_receipts",
        "trading_capital_risk_reservation_state",
        "trading_capital_risk_events",
    ):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"GRANT SELECT ON {table} TO tracefold_serve")
        op.execute(f"GRANT SELECT ON {table} TO tracefold_workers")
        op.execute(f"GRANT SELECT ON {table} TO tracefold_nautilus")

    for table in (
        "trading_daily_risk_policies",
        "trading_production_promotion_grants",
        "trading_promotion_grant_revocations",
        "trading_operator_arm_receipts",
        "trading_capital_risk_reservations",
        "trading_capital_authorization_receipts",
        "trading_capital_risk_reservation_state",
        "trading_capital_risk_events",
    ):
        op.execute(f"GRANT INSERT ON {table} TO tracefold_workers")
    op.execute("GRANT INSERT ON trading_capital_risk_events TO tracefold_nautilus")
    op.execute("GRANT UPDATE (funding_by_currency) ON trading_intents TO tracefold_nautilus")
    op.execute("GRANT SELECT (arm_epoch) ON trading_runtime_state TO tracefold_nautilus")
    op.execute(
        "GRANT UPDATE (status, current_planned_risk_amount, attempt_consumed, attempt_day_start_ms, "
        "attempt_day_end_ms, settlement_known, updated_at_ms) "
        "ON trading_capital_risk_reservation_state TO tracefold_nautilus"
    )
    op.execute("REVOKE UPDATE ON trading_runtime_state FROM tracefold_workers")
    op.execute("GRANT UPDATE (control, arm_epoch, updated_at_ms) ON trading_runtime_state TO tracefold_workers")
    op.execute(
        "GRANT UPDATE (active_arm_receipt_sha256, runtime_state, account_state, heartbeat_at_ms, reason, "
        "updated_at_ms) ON trading_binding_runtime TO tracefold_workers"
    )


def downgrade() -> None:
    raise RuntimeError("trading_capital_authority_v1_downgrade_unsupported")
