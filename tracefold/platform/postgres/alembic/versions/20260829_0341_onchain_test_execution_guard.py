"""Bind onchain test executions to their source and a 200U durable ceiling.

Revision ID: 20260829_0341
Revises: 20260829_0340
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0341"
down_revision = "20260829_0340"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM trading_onchain_execution_intents) THEN
            RAISE EXCEPTION '0341_onchain_execution_reset_required';
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TABLE trading_onchain_settlement_assets (
          chain_id         BIGINT NOT NULL CHECK (chain_id > 0),
          contract_address TEXT NOT NULL CHECK (contract_address ~ '^0x[0-9a-f]{40}$'),
          symbol           TEXT NOT NULL CHECK (symbol ~ '^[A-Z0-9]{2,16}$'),
          decimals         INTEGER NOT NULL CHECK (decimals BETWEEN 0 AND 255),
          PRIMARY KEY (chain_id, contract_address),
          UNIQUE (chain_id, contract_address, decimals)
        )
        """
    )
    op.execute(
        """
        INSERT INTO trading_onchain_settlement_assets
          (chain_id, contract_address, symbol, decimals)
        VALUES
          (1, '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48', 'USDC', 6),
          (56, '0x55d398326f99059ff775485246999027b3197955', 'USDT', 18),
          (8453, '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913', 'USDC', 6),
          (42161, '0xaf88d065e77c8cc2239327c5edb3a432268e5831', 'USDC', 6),
          (4663, '0x5fc5360d0400a0fd4f2af552add042d716f1d168', 'USDG', 6)
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_onchain_settlement_asset_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'trading_onchain_settlement_asset_mutation_forbidden';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_settlement_asset_mutation "
        "BEFORE INSERT OR UPDATE OR DELETE ON trading_onchain_settlement_assets "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_onchain_settlement_asset_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_settlement_asset_truncate "
        "BEFORE TRUNCATE ON trading_onchain_settlement_assets "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_trading_onchain_settlement_asset_mutation()"
    )
    op.execute(
        "ALTER TABLE trading_onchain_execution_intents "
        "ADD COLUMN development_test BOOLEAN NOT NULL, "
        "ADD COLUMN notional_usd NUMERIC(38, 18) NOT NULL, "
        "ADD COLUMN settlement_decimals INTEGER NOT NULL, "
        "ADD CONSTRAINT trading_onchain_execution_notional_check CHECK ("
        "notional_usd > 0 AND settlement_decimals BETWEEN 0 AND 255 "
        "AND (request ->> 'input_amount_raw')::numeric = "
        "trunc(notional_usd * power(10::numeric, settlement_decimals)) "
        "AND (NOT development_test OR notional_usd <= 200))"
    )
    op.execute(
        """
        CREATE FUNCTION validate_trading_onchain_execution_test_binding() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          expected_development_test BOOLEAN;
          settlement_binding_ok BOOLEAN;
        BEGIN
          SELECT EXISTS (
            SELECT 1
              FROM jsonb_array_elements(session.sources) AS source
             WHERE source ->> 'news_event_id' LIKE 'development-test:%'
          )
            INTO expected_development_test
            FROM trading_onchain_analysis_sessions AS session
           WHERE session.session_id = NEW.session_id;
          IF expected_development_test IS NULL
             OR NEW.development_test IS DISTINCT FROM expected_development_test THEN
            RAISE EXCEPTION 'trading_onchain_execution_test_binding_invalid';
          END IF;
          SELECT TRUE
            INTO settlement_binding_ok
            FROM trading_onchain_settlement_assets AS asset
           WHERE asset.chain_id = (NEW.request ->> 'chain_id')::bigint
             AND asset.contract_address = NEW.request ->> 'input_contract'
             AND asset.decimals = NEW.settlement_decimals;
          IF settlement_binding_ok IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'trading_onchain_execution_settlement_binding_invalid';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_execution_test_binding "
        "BEFORE INSERT ON trading_onchain_execution_intents "
        "FOR EACH ROW EXECUTE FUNCTION validate_trading_onchain_execution_test_binding()"
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_onchain_execution_notional_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.development_test, NEW.notional_usd, NEW.settlement_decimals)
              IS DISTINCT FROM
             ROW(OLD.development_test, OLD.notional_usd, OLD.settlement_decimals)
          THEN
            RAISE EXCEPTION 'trading_onchain_execution_notional_mutation_forbidden';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_onchain_execution_notional_mutation "
        "BEFORE UPDATE OF development_test, notional_usd, settlement_decimals "
        "ON trading_onchain_execution_intents "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_onchain_execution_notional_mutation()"
    )
    op.execute(
        "REVOKE ALL ON trading_onchain_settlement_assets "
        "FROM tracefold_workers, tracefold_serve, tracefold_nautilus, tracefold_onchain"
    )
    op.execute("GRANT SELECT ON trading_onchain_settlement_assets TO tracefold_workers, tracefold_serve")
    op.execute(
        "GRANT INSERT (development_test, notional_usd, settlement_decimals) "
        "ON trading_onchain_execution_intents TO tracefold_workers"
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0341 owns onchain development-test execution guards and cannot be downgraded")
