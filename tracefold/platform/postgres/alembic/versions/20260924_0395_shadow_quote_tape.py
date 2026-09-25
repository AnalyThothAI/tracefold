"""Index bounded, archived executable quote samples for pending shadow paths.

Revision ID: 20260924_0395
Revises: 20260924_0394
"""

from __future__ import annotations

from alembic import op

revision = "20260924_0395"
down_revision = "20260924_0394"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute(
        "ALTER TABLE public.trading_case_evaluations ADD COLUMN quote_tape_ref text, ADD COLUMN next_quote_at_ms bigint"
    )
    op.execute(
        "UPDATE public.trading_case_evaluations SET next_quote_at_ms=scheduled_at_ms "
        "WHERE source='shadow_simulation' AND status='pending'"
    )
    op.execute(
        "CREATE INDEX ix_trading_case_evaluations_quote_due "
        "ON public.trading_case_evaluations(next_quote_at_ms,case_id) "
        "WHERE source='shadow_simulation' AND status='pending'"
    )


def downgrade() -> None:
    raise RuntimeError("shadow_quote_tape_forward_only: restore a verified pre-0395 archive")
