"""Repair already-applied Macro reader constraints and require exact schemas."""

from __future__ import annotations

from alembic import op

revision = "20260729_0215"
down_revision = "20260729_0214"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM macro_module_current;

        ALTER TABLE macro_module_current
          DROP CONSTRAINT macro_module_current_typed_schema_check;
        ALTER TABLE macro_module_current
          ADD CONSTRAINT macro_module_current_typed_schema_check
          CHECK (
            payload_json ->> 'schema_version' = CASE module_id
              WHEN 'rates_fed' THEN 'macro_rates_fed_v5'
              WHEN 'economy_inflation' THEN 'macro_economy_inflation_v5'
              WHEN 'liquidity_funding' THEN 'macro_liquidity_funding_v5'
              WHEN 'credit' THEN 'macro_credit_v7'
              WHEN 'volatility' THEN 'macro_volatility_v6'
              WHEN 'cross_asset' THEN 'macro_cross_asset_v6'
              ELSE NULL
            END
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260729_0215 is an irreversible Macro exact-schema hard cut")
