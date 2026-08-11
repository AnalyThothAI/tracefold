"""Hard-cut changed Macro modules to explicit semantic contracts.

Revision ID: 20260811_0253
Revises: 20260811_0252
"""

from __future__ import annotations

from alembic import op

revision = "20260811_0253"
down_revision = "20260811_0252"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        """
        DELETE FROM macro_module_current
         WHERE module_id IN ('rates_fed', 'economy_inflation', 'cross_asset');

        DELETE FROM macro_module_frontiers
         WHERE module_id IN ('rates_fed', 'economy_inflation', 'cross_asset');

        ALTER TABLE macro_module_current
          DROP CONSTRAINT macro_module_current_typed_schema_check;
        ALTER TABLE macro_module_current
          ADD CONSTRAINT macro_module_current_typed_schema_check
          CHECK (
            payload_json ->> 'schema_version' = CASE module_id
              WHEN 'rates_fed' THEN 'macro_rates_fed_v8'
              WHEN 'economy_inflation' THEN 'macro_economy_inflation_v6'
              WHEN 'liquidity_funding' THEN 'macro_liquidity_funding_v5'
              WHEN 'credit' THEN 'macro_credit_v7'
              WHEN 'volatility' THEN 'macro_volatility_v7'
              WHEN 'cross_asset' THEN 'macro_cross_asset_v8'
              ELSE NULL
            END
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260811_0253 is an irreversible Macro semantic-contract hard cut")
