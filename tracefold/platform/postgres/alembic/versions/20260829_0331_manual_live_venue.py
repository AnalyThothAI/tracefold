"""Allow the manual production venue without relabeling historical Demo facts (#327).

Revision ID: 20260829_0331
Revises: 20260829_0330
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0331"
down_revision = "20260829_0330"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE trading_account_bindings
          DROP CONSTRAINT trading_account_binding_venue_check,
          ADD CONSTRAINT trading_account_binding_venue_check
            CHECK (
              (account_lane = 'auto' AND venue = 'binance_usdm_demo')
              OR (account_lane = 'manual' AND venue IN ('binance_usdm_demo', 'binance_usdm_live'))
            ) NOT VALID
        """
    )
    op.execute("ALTER TABLE trading_account_bindings VALIDATE CONSTRAINT trading_account_binding_venue_check")
    op.execute(
        """
        ALTER TABLE trading_manual_account_snapshots
          DROP CONSTRAINT trading_manual_account_snapshot_venue_check,
          ADD CONSTRAINT trading_manual_account_snapshot_venue_check
            CHECK (venue IN ('binance_usdm_demo', 'binance_usdm_live')) NOT VALID
        """
    )
    op.execute(
        "ALTER TABLE trading_manual_account_snapshots VALIDATE CONSTRAINT trading_manual_account_snapshot_venue_check"
    )
    op.execute(
        """
        CREATE FUNCTION reject_retired_manual_demo_binding() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.account_lane = 'manual' AND NEW.venue = 'binance_usdm_demo'
          THEN
            RAISE EXCEPTION USING
              MESSAGE = 'trading_manual_demo_binding_retired',
              ERRCODE = '23514',
              CONSTRAINT = 'trading_manual_demo_binding_retired';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_retired_manual_demo_snapshot() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.venue = 'binance_usdm_demo'
          THEN
            RAISE EXCEPTION USING
              MESSAGE = 'trading_manual_demo_snapshot_retired',
              ERRCODE = '23514',
              CONSTRAINT = 'trading_manual_demo_snapshot_retired';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_retired_manual_demo_intent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.payload ->> 'venue' IS DISTINCT FROM 'binance_usdm_live'
          THEN
            RAISE EXCEPTION USING
              MESSAGE = 'trading_manual_demo_intent_retired',
              ERRCODE = '23514',
              CONSTRAINT = 'trading_manual_demo_intent_retired';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_manual_demo_binding_retired "
        "BEFORE INSERT OR UPDATE OF account_lane, venue ON trading_account_bindings "
        "FOR EACH ROW EXECUTE FUNCTION reject_retired_manual_demo_binding()"
    )
    op.execute(
        "CREATE TRIGGER trg_trading_manual_demo_snapshot_retired "
        "BEFORE INSERT OR UPDATE OF venue ON trading_manual_account_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION reject_retired_manual_demo_snapshot()"
    )
    op.execute(
        "CREATE TRIGGER trg_trading_manual_demo_intent_retired "
        "BEFORE INSERT ON trading_manual_intents "
        "FOR EACH ROW EXECUTE FUNCTION reject_retired_manual_demo_intent()"
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0331 enables live manual capital facts and cannot be downgraded")
