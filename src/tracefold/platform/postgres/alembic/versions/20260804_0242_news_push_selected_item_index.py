"""Bound selected-Article lookup in the durable News push ledger.

Revision ID: 20260804_0242
Revises: 20260802_0241
"""

from __future__ import annotations

from alembic import op

revision = "20260804_0242"
down_revision = "20260802_0241"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX idx_news_push_deliveries_selected_item
          ON news_push_deliveries(selected_item_id);
        ANALYZE news_push_deliveries;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260804_0242 is an irreversible News push lookup-index hard cut")
