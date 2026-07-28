"""Hard-cut Macro market identities, health summary, and publication status."""

from __future__ import annotations

from alembic import op

revision = "20260728_0211"
down_revision = "20260728_0210"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM macro_acquisition_targets
        WHERE dataset_id LIKE 'yfinance.%.market'
           OR dataset_id LIKE 'nasdaq.%.history'
        """
    )
    op.execute("DELETE FROM macro_module_current")
    op.execute("DELETE FROM macro_judgment_status WHERE state = 'blocked'")
    op.execute(
        """
        ALTER TABLE macro_judgment_status
        DROP CONSTRAINT macro_judgment_status_state_check
        """
    )
    op.execute(
        """
        ALTER TABLE macro_judgment_status
        ADD CONSTRAINT macro_judgment_status_state_check
        CHECK (state = 'current')
        """
    )
    op.execute(
        """
        ALTER TABLE macro_module_current
        DROP CONSTRAINT macro_module_current_data_health_state_check
        """
    )
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
            WHEN 'rates_fed' THEN 'macro_rates_fed_v3'
            WHEN 'economy_inflation' THEN 'macro_economy_inflation_v3'
            WHEN 'liquidity_funding' THEN 'macro_liquidity_funding_v3'
            WHEN 'credit' THEN 'macro_credit_v4'
            WHEN 'volatility' THEN 'macro_volatility_v3'
            WHEN 'cross_asset' THEN 'macro_cross_asset_v4'
            ELSE NULL
          END
        )
        """
    )
    op.execute(
        """
        ALTER TABLE macro_module_current
        ADD CONSTRAINT macro_module_current_data_health_state_check
        CHECK (
          data_health_state = ANY (
            ARRAY[
              'current'::text,
              'mixed'::text,
              'unavailable'::text
            ]
          )
        )
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260728_0211 is an irreversible Macro market contract hard cut")
