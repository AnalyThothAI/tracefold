"""Index Event-owned asset reads for the bounded reader-history projection (#175).

``news_event_assets`` is keyed by ``(symbol, event_id)`` for symbol-led feed and
retrieval reads. Reader history also projects canonical assets for at most 128
already-selected Events; without the reverse key PostgreSQL scans the full asset
table once per Event. This additive index gives that independent read direction
an exact, bounded lookup without changing material facts or application behavior.

Revision ID: 20260824_0302
Revises: 20260823_0301
"""

from __future__ import annotations

from alembic import op

revision = "20260824_0302"
down_revision = "20260823_0301"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX ix_news_event_assets_event ON news_event_assets (event_id, symbol)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_news_event_assets_event")
