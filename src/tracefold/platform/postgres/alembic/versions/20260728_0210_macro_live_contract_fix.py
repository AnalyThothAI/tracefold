"""Allow the live-market v3 Credit and Cross-Asset module contracts."""

from __future__ import annotations

from alembic import op

revision = "20260728_0210"
down_revision = "20260728_0209"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5min'")
    op.execute("DELETE FROM macro_module_current")
    op.execute(
        """
        ALTER TABLE macro_module_current
          DROP CONSTRAINT macro_module_current_typed_schema_check
        """
    )
    op.execute(
        """
        ALTER TABLE macro_module_current
          ADD CONSTRAINT macro_module_current_typed_schema_check
          CHECK (
            payload_json ->> 'schema_version' = CASE module_id
              WHEN 'rates_fed' THEN 'macro_rates_fed_v2'
              WHEN 'economy_inflation' THEN 'macro_economy_inflation_v2'
              WHEN 'liquidity_funding' THEN 'macro_liquidity_funding_v2'
              WHEN 'credit' THEN 'macro_credit_v3'
              WHEN 'volatility' THEN 'macro_volatility_v2'
              WHEN 'cross_asset' THEN 'macro_cross_asset_v3'
            END
          )
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260728_0210 is an irreversible Macro live-contract hard cut")
