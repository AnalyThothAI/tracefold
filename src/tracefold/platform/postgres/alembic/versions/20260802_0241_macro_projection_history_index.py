"""Index exact UTC-day Macro projection history.

Revision ID: 20260802_0241
Revises: 20260801_0240
"""

from __future__ import annotations

from alembic import op

revision = "20260802_0241"
down_revision = "20260801_0240"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX idx_market_observations_projection_history
          ON market_observations(
            dataset_id,
            (observed_at_ms / 86400000) DESC,
            observed_at_ms DESC,
            received_at_ms DESC,
            observation_id DESC
          );
        ANALYZE market_observations;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260802_0241 is an irreversible Macro projection history index hard cut")
