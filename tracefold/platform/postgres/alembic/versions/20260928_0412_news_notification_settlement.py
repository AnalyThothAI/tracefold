"""Retain exact send outcome identities across a lost settlement response.

Revision ID: 20260928_0412
Revises: 20260928_0411
"""

from alembic import op

revision = "20260928_0412"
down_revision = "20260928_0411"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE public.news_delivery_queue DROP CONSTRAINT news_delivery_queue_attempted_check")
    op.execute("ALTER TABLE public.news_delivery_queue ADD COLUMN last_settlement jsonb")
    op.execute(
        "ALTER TABLE public.news_delivery_queue ADD CONSTRAINT news_delivery_queue_last_settlement_shape "
        "CHECK (last_settlement IS NULL OR jsonb_typeof(last_settlement) = 'object')"
    )
    op.execute("ALTER TABLE public.news_deliveries ADD COLUMN settlement jsonb")
    op.execute(
        "ALTER TABLE public.news_deliveries ADD CONSTRAINT news_deliveries_settlement_shape "
        "CHECK (settlement IS NULL OR jsonb_typeof(settlement) = 'object')"
    )


def downgrade() -> None:
    raise RuntimeError("news_notification_settlement_forward_only_restore_verified_backup")
