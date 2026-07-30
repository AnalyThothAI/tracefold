"""Keep the high-churn Events search planner statistics current."""

from __future__ import annotations

from alembic import op

revision = "20260731_0228"
down_revision = "20260730_0227"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE events SET (
          autovacuum_analyze_scale_factor = 0.01,
          autovacuum_analyze_threshold = 10000
        );
        ANALYZE events;
        """
    )


def downgrade() -> None:
    raise NotImplementedError("hard cut migration")
