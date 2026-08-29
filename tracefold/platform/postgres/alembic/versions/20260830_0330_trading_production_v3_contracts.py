"""Production V3 contract/storage hard cut (#376).

Revision ID: 20260830_0330
Revises: 20260829_0329
"""

from __future__ import annotations

from alembic import op

revision = "20260830_0330"
down_revision = "20260829_0329"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        LOCK TABLE trading_runtime_state, trading_intents IN SHARE ROW EXCLUSIVE MODE;
        DO $cutover$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM trading_runtime_state WHERE id = 1 AND control = 'PAUSED') THEN
            RAISE EXCEPTION 'trading_v3_contract_cutover_requires_paused';
          END IF;
          IF EXISTS (
            SELECT 1 FROM trading_intents
             WHERE intent_version IN ('trade_intent_v1', 'trade_intent_v2')
               AND execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
          ) THEN
            RAISE EXCEPTION 'trading_v3_contract_cutover_legacy_obligation';
          END IF;
        END
        $cutover$
        """
    )

    # V1 rows remain immutable archive facts. Every current insert is a complete per-binding V2
    # partition tied to the exact public catalogue it compiled.
    op.execute("ALTER TABLE trading_execution_capability_snapshots ALTER COLUMN execution_environment DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE trading_execution_capability_snapshots
          ADD COLUMN binding TEXT,
          ADD COLUMN venue TEXT,
          ADD COLUMN catalog_snapshot_sha256 TEXT,
          ADD COLUMN catalog_instrument_count INTEGER,
          ADD COLUMN partition_sha256 TEXT,
          ADD CONSTRAINT trading_capability_catalog_fk FOREIGN KEY (catalog_snapshot_sha256)
            REFERENCES trading_venue_catalog_snapshots(snapshot_sha256) ON DELETE RESTRICT
        """
    )
    op.execute(
        "ALTER TABLE trading_execution_capability_snapshots "
        "DROP CONSTRAINT trading_capability_snapshot_environment_check"
    )
    op.execute(
        "ALTER TABLE trading_execution_capability_snapshots DROP CONSTRAINT trading_capability_snapshot_counts_check"
    )
    op.execute(
        "ALTER TABLE trading_execution_capability_snapshots DROP CONSTRAINT trading_capability_snapshot_payload_check"
    )
    op.execute(
        """
        CREATE FUNCTION trading_jsonb_object_size(value JSONB) RETURNS INTEGER
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT count(*)::INTEGER FROM jsonb_object_keys(value)
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE trading_execution_capability_snapshots
          ADD CONSTRAINT trading_capability_snapshot_shape_check CHECK (
            (
              payload ->> 'snapshot_version' = 'execution_capability_snapshot_v1'
              AND execution_environment = 'BINANCE_USDM_DEMO'
              AND binding IS NULL AND venue IS NULL AND catalog_snapshot_sha256 IS NULL
              AND catalog_instrument_count IS NULL AND partition_sha256 IS NULL
              AND included_count > 0 AND excluded_count >= 0
            ) OR (
              payload ->> 'snapshot_version' = 'execution_capability_snapshot_v2'
              AND execution_environment IS NULL
              AND binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')
              AND venue = CASE binding
                    WHEN 'BINANCE_USDM' THEN 'binance.usdm'
                    ELSE 'hyperliquid.perp' END
              AND catalog_snapshot_sha256 ~ '^[0-9a-f]{64}$'
              AND catalog_instrument_count >= 0
              AND included_count >= 0 AND excluded_count >= 0
              AND catalog_instrument_count = included_count + excluded_count
              AND partition_sha256 ~ '^[0-9a-f]{64}$'
              AND payload ->> 'binding' = binding
              AND payload ->> 'venue' = venue
              AND payload ->> 'catalog_snapshot_sha256' = catalog_snapshot_sha256
              AND (payload ->> 'catalog_instrument_count')::INTEGER = catalog_instrument_count
              AND (payload ->> 'included_count')::INTEGER = included_count
              AND (payload ->> 'excluded_count')::INTEGER = excluded_count
              AND payload ->> 'partition_sha256' = partition_sha256
              AND jsonb_typeof(payload -> 'included') = 'object'
              AND jsonb_typeof(payload -> 'excluded') = 'object'
              AND trading_jsonb_object_size(payload -> 'included') = included_count
              AND trading_jsonb_object_size(payload -> 'excluded') = excluded_count
            )
          )
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_new_execution_capability_v1() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.payload ->> 'snapshot_version' <> 'execution_capability_snapshot_v2' THEN
            RAISE EXCEPTION 'new_execution_capability_v1_forbidden';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_capability_v2_only BEFORE INSERT "
        "ON trading_execution_capability_snapshots FOR EACH ROW "
        "EXECUTE FUNCTION reject_new_execution_capability_v1()"
    )

    op.execute(
        """
        ALTER TABLE trading_binding_runtime
          ADD COLUMN account_generation BIGINT NOT NULL DEFAULT 0,
          ADD COLUMN capability_state TEXT NOT NULL DEFAULT 'missing',
          ADD COLUMN capability_snapshot_sha256 TEXT,
          ADD COLUMN capability_compiled_at_ms BIGINT,
          ADD COLUMN capability_compile_error TEXT,
          ADD COLUMN execution_binding_sha256 TEXT,
          ADD CONSTRAINT trading_binding_account_generation_check CHECK (account_generation >= 0),
          ADD CONSTRAINT trading_binding_capability_state_check
            CHECK (capability_state IN ('missing', 'ready', 'stale', 'error')),
          ADD CONSTRAINT trading_binding_capability_pair_check CHECK (
            (capability_snapshot_sha256 IS NULL AND capability_compiled_at_ms IS NULL
              AND capability_state IN ('missing', 'error'))
            OR (capability_snapshot_sha256 IS NOT NULL AND capability_compiled_at_ms IS NOT NULL
              AND capability_state IN ('ready', 'stale', 'error'))
          ),
          ADD CONSTRAINT trading_binding_capability_error_check CHECK (
            capability_compile_error IS NULL OR length(capability_compile_error) BETWEEN 1 AND 128
          ),
          ADD CONSTRAINT trading_binding_capability_fk FOREIGN KEY (capability_snapshot_sha256)
            REFERENCES trading_execution_capability_snapshots(snapshot_sha256) ON DELETE RESTRICT
        """
    )

    op.execute(
        """
        CREATE TABLE trading_execution_bindings (
          binding_sha256    TEXT PRIMARY KEY,
          binding           TEXT NOT NULL,
          account_generation BIGINT NOT NULL,
          created_at_ms     BIGINT NOT NULL,
          payload           JSONB NOT NULL,
          CONSTRAINT trading_execution_binding_sha_check CHECK (binding_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_execution_binding_binding_check
            CHECK (binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')),
          CONSTRAINT trading_execution_binding_generation_check CHECK (account_generation >= 1),
          CONSTRAINT trading_execution_binding_payload_check CHECK (
            payload ->> 'binding_version' = 'execution_binding_v1'
            AND payload ->> 'binding' = binding
            AND (payload ->> 'account_generation')::BIGINT = account_generation
          )
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_execution_bindings_append_only BEFORE UPDATE OR DELETE "
        "ON trading_execution_bindings FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )
    op.execute(
        "ALTER TABLE trading_binding_runtime ADD CONSTRAINT trading_binding_execution_binding_fk "
        "FOREIGN KEY (execution_binding_sha256) REFERENCES trading_execution_bindings(binding_sha256) "
        "ON DELETE RESTRICT"
    )

    op.execute(
        """
        ALTER TABLE trading_intents
          ADD COLUMN source_venue TEXT,
          ADD COLUMN source_identity TEXT,
          ADD COLUMN canonical_asset TEXT,
          ADD COLUMN binding TEXT,
          ADD COLUMN account_generation BIGINT,
          ADD COLUMN execution_binding_sha256 TEXT,
          ADD COLUMN venue_catalog_snapshot_sha256 TEXT,
          ADD COLUMN capability_entry_id TEXT,
          ADD COLUMN provider_instrument_id TEXT,
          ADD COLUMN settlement_asset TEXT,
          ADD COLUMN execution_policy_sha256 TEXT,
          ADD COLUMN quote_contract_sha256 TEXT,
          ADD COLUMN protection_contract_sha256 TEXT,
          ADD COLUMN capital_authorization_receipt_sha256 TEXT,
          ADD COLUMN economic_lifecycle_id TEXT,
          ADD COLUMN entry_leg_id TEXT,
          ADD COLUMN protection_leg_id TEXT,
          ADD COLUMN close_leg_id TEXT,
          ADD COLUMN leverage INTEGER,
          ADD COLUMN target_notional NUMERIC,
          ADD COLUMN max_risk_amount NUMERIC,
          ADD COLUMN risk_currency TEXT,
          ADD CONSTRAINT trading_intents_execution_binding_fk FOREIGN KEY (execution_binding_sha256)
            REFERENCES trading_execution_bindings(binding_sha256) ON DELETE RESTRICT,
          ADD CONSTRAINT trading_intents_venue_catalog_fk FOREIGN KEY (venue_catalog_snapshot_sha256)
            REFERENCES trading_venue_catalog_snapshots(snapshot_sha256) ON DELETE RESTRICT
        """
    )
    op.execute("ALTER TABLE trading_intents ALTER COLUMN execution_environment DROP NOT NULL")
    op.execute("ALTER TABLE trading_intents ALTER COLUMN target_notional_usd DROP NOT NULL")
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_version_check")
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_v2_shape_check")
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_submission_fence_v1_check")
    op.execute(
        """
        ALTER TABLE trading_intents
          ADD CONSTRAINT trading_intents_version_check CHECK (
            intent_version IN ('trade_intent_v1', 'trade_intent_v2', 'trade_intent_v3')
          ),
          ADD CONSTRAINT trading_intents_current_shape_check CHECK (
            (
              intent_version IN ('trade_intent_v1', 'trade_intent_v2')
              AND source_venue IS NULL AND source_identity IS NULL AND canonical_asset IS NULL
              AND binding IS NULL AND account_generation IS NULL AND execution_binding_sha256 IS NULL
              AND venue_catalog_snapshot_sha256 IS NULL AND capability_entry_id IS NULL
              AND provider_instrument_id IS NULL AND settlement_asset IS NULL
              AND execution_policy_sha256 IS NULL AND quote_contract_sha256 IS NULL
              AND protection_contract_sha256 IS NULL AND capital_authorization_receipt_sha256 IS NULL
              AND economic_lifecycle_id IS NULL AND entry_leg_id IS NULL
              AND protection_leg_id IS NULL AND close_leg_id IS NULL AND leverage IS NULL
              AND target_notional IS NULL AND max_risk_amount IS NULL AND risk_currency IS NULL
              AND execution_environment = 'BINANCE_USDM_DEMO'
              AND target_notional_usd IS NOT NULL
              AND (
                (intent_version = 'trade_intent_v1'
                  AND execution_capability_snapshot_sha256 IS NULL
                  AND blacklist_revision_at_emission IS NULL
                  AND blacklist_snapshot_sha256_at_emission IS NULL
                  AND blacklist_snapshot_payload_at_emission IS NULL
                  AND underlying_key IS NULL)
                OR
                (intent_version = 'trade_intent_v2'
                  AND execution_capability_snapshot_sha256 IS NOT NULL
                  AND blacklist_revision_at_emission IS NOT NULL
                  AND blacklist_snapshot_sha256_at_emission ~ '^[0-9a-f]{64}$'
                  AND blacklist_snapshot_payload_at_emission ->> 'snapshot_version' = 'blacklist_snapshot_v1'
                  AND underlying_key ~ '^crypto:[A-Z0-9]{1,32}$')
              )
            ) OR (
              intent_version = 'trade_intent_v3'
              AND execution_environment IS NULL AND target_notional_usd IS NULL
              AND source_venue IN ('binance.usdm', 'hyperliquid.perp')
              AND length(source_identity) BETWEEN 1 AND 256
              AND canonical_asset ~ '^[A-Z0-9][A-Z0-9._-]{0,31}$'
              AND underlying_key = 'crypto:' || canonical_asset
              AND binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')
              AND source_venue = CASE binding
                    WHEN 'BINANCE_USDM' THEN 'binance.usdm' ELSE 'hyperliquid.perp' END
              AND account_generation >= 1
              AND execution_binding_sha256 ~ '^[0-9a-f]{64}$'
              AND venue_catalog_snapshot_sha256 ~ '^[0-9a-f]{64}$'
              AND execution_capability_snapshot_sha256 ~ '^[0-9a-f]{64}$'
              AND capability_entry_id ~ '^[0-9a-f]{64}$'
              AND length(provider_instrument_id) > 0 AND length(instrument_id) > 0
              AND settlement_asset IN ('USDT', 'USDC')
              AND intent_policy_sha256 ~ '^[0-9a-f]{64}$'
              AND execution_policy_sha256 ~ '^[0-9a-f]{64}$'
              AND quote_contract_sha256 ~ '^[0-9a-f]{64}$'
              AND protection_contract_sha256 ~ '^[0-9a-f]{64}$'
              AND capital_authorization_receipt_sha256 ~ '^[0-9a-f]{64}$'
              AND blacklist_revision_at_emission >= 0
              AND blacklist_snapshot_sha256_at_emission ~ '^[0-9a-f]{64}$'
              AND blacklist_snapshot_payload_at_emission ->> 'snapshot_version' = 'blacklist_snapshot_v1'
              AND economic_lifecycle_id ~ '^[0-9a-f]{64}$'
              AND entry_leg_id ~ '^[0-9a-f]{64}$'
              AND protection_leg_id ~ '^[0-9a-f]{64}$'
              AND close_leg_id ~ '^[0-9a-f]{64}$'
              AND side = 'long' AND leverage = 1
              AND reference_price > 0 AND target_notional > 0 AND target_notional <= 10
              AND max_risk_amount > 0 AND max_risk_amount <= target_notional
              AND risk_currency = settlement_asset
            )
          ),
          ADD CONSTRAINT trading_intents_submission_fence_v1_check CHECK (
            (submission_fence_version IS NULL AND submission_quantity IS NULL)
            OR (
              submission_fence_version = 'submission_fence_v1'
              AND submission_quantity > 0
              AND submission_quantity * (entry_quote_q1 ->> 'side_price')::NUMERIC
                    <= COALESCE(target_notional, target_notional_usd)
              AND entry_client_order_id IS NOT NULL
              AND entry_fenced_at_ms IS NOT NULL
              AND entry_quote_q1 ->> 'snapshot_version' = 'execution_quote_snapshot_v1'
              AND entry_quote_q1 ->> 'reason' = 'accepted'
            )
          )
        """
    )
    op.execute("DROP TRIGGER trg_trading_intents_v2_only ON trading_intents")
    op.execute("DROP FUNCTION reject_new_trade_intent_v1()")
    op.execute(
        """
        CREATE FUNCTION reject_new_legacy_trade_intent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.intent_version <> 'trade_intent_v3' THEN
            RAISE EXCEPTION 'new_legacy_trade_intent_forbidden';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_intents_v3_only BEFORE INSERT ON trading_intents "
        "FOR EACH ROW EXECUTE FUNCTION reject_new_legacy_trade_intent()"
    )

    op.execute("REVOKE ALL ON trading_execution_bindings FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON trading_execution_bindings TO tracefold_workers")
    op.execute("GRANT SELECT ON trading_execution_bindings TO tracefold_serve")
    op.execute("GRANT SELECT, INSERT ON trading_execution_bindings TO tracefold_nautilus")
    op.execute(
        "GRANT UPDATE (account_generation, capability_state, capability_snapshot_sha256, "
        "capability_compiled_at_ms, capability_compile_error, execution_binding_sha256) "
        "ON trading_binding_runtime TO tracefold_workers"
    )
    op.execute(
        "GRANT UPDATE (runtime_state, account_state, heartbeat_at_ms, reason, updated_at_ms, "
        "execution_binding_sha256) "
        "ON trading_binding_runtime TO tracefold_nautilus"
    )
    op.execute(
        """
        GRANT INSERT (
          intent_id, intent_version, case_id, case_manifest_sha256,
          source_venue, source_identity, canonical_asset, underlying_key, binding, account_generation,
          execution_binding_sha256, venue_catalog_snapshot_sha256,
          execution_capability_snapshot_sha256, capability_entry_id,
          provider_instrument_id, instrument_id, settlement_asset,
          intent_policy_sha256, execution_policy_sha256, quote_contract_sha256,
          protection_contract_sha256, capital_authorization_receipt_sha256,
          blacklist_revision_at_emission, blacklist_snapshot_sha256_at_emission,
          blacklist_snapshot_payload_at_emission,
          economic_lifecycle_id, entry_leg_id, protection_leg_id, close_leg_id,
          side, leverage, created_at_ms, valid_until_ms, reference_price,
          target_notional, max_risk_amount, risk_currency,
          stop_loss_bps, max_holding_ms, max_entry_drift_bps, max_spread_bps
        ) ON trading_intents TO tracefold_workers
        """
    )


def downgrade() -> None:
    raise RuntimeError("trading_production_v3_contracts_downgrade_unsupported")
