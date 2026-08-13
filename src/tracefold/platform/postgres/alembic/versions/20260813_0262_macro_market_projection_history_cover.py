"""Cover the exact UTC-day Macro market projection read.

Revision ID: 20260813_0262
Revises: 20260813_0261
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0262"
down_revision = "20260813_0261"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        """
        DROP INDEX idx_market_observations_projection_history;

        CREATE INDEX idx_market_observations_projection_history
          ON market_observations(
            dataset_id,
            (observed_at_ms / 86400000) DESC,
            observed_at_ms DESC,
            received_at_ms DESC,
            observation_id DESC
          )
          INCLUDE (
            instrument_id,
            source_id,
            field_name,
            value_numeric,
            unit,
            published_at_ms,
            trust_tier,
            source_url,
            fact_hash
          );

        ANALYZE market_observations;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260813_0262 is an irreversible Macro projection covering-read cut")
