"""Cover the bounded News realtime latency reads.

Revision ID: 20260813_0267
Revises: 20260813_0266
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0267"
down_revision = "20260813_0266"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        CREATE INDEX ix_news_items_opennews_live_latency
          ON news_items(first_observed_at_ms DESC, item_id DESC)
          INCLUDE (published_at_ms)
          WHERE source_id = 'news-opennews'
            AND first_ingest_mode = 'live';

        CREATE INDEX ix_news_stories_created
          ON news_stories(created_at_ms DESC, story_id DESC);

        ANALYZE news_items;
        ANALYZE news_stories;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        DROP INDEX IF EXISTS ix_news_stories_created;
        DROP INDEX IF EXISTS ix_news_items_opennews_live_latency;
        """
    )
