"""Install the supported PostgreSQL query-statistics extension."""

from __future__ import annotations

from alembic import op

revision = "20260609_0160"
down_revision = "20260609_0159"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")

    op.execute("ANALYZE events")
    op.execute("ANALYZE enriched_events")
    op.execute("ANALYZE news_items")
    op.execute("ANALYZE news_page_rows")
    op.execute("ANALYZE news_item_agent_runs")
    op.execute("ANALYZE worker_queue_terminal_events")
    op.execute("ANALYZE event_anchor_backfill_jobs")
    op.execute("ANALYZE token_radar_rank_source_events")
    op.execute("ANALYZE token_radar_target_features")
    op.execute("ANALYZE token_radar_current_rows")
    op.execute("ANALYZE token_radar_publication_state")


def downgrade() -> None:
    pass
