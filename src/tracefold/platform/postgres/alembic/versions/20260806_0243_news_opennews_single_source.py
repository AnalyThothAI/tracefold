"""Retire RSS inventory and hard-cut News acquisition to OpenNews.

Revision ID: 20260806_0243
Revises: 20260804_0242
"""

from __future__ import annotations

from alembic import op

revision = "20260806_0243"
down_revision = "20260804_0242"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP TABLE news_source_memberships;

        DROP INDEX ix_news_sources_due;
        DROP INDEX ix_news_sources_due_claim;

        ALTER TABLE news_sources
          DROP COLUMN feed_url,
          DROP COLUMN refresh_interval_seconds,
          DROP COLUMN etag,
          DROP COLUMN last_modified,
          DROP COLUMN next_fetch_at_ms,
          DROP COLUMN claim_token,
          DROP COLUMN claim_lease_expires_at_ms;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260806_0243 is an irreversible OpenNews single-source hard cut")
