"""Index source-root research tapes independently of model dispositions.

Revision ID: 20260924_0396
Revises: 20260924_0395
"""

from __future__ import annotations

from alembic import op

revision = "20260924_0396"
down_revision = "20260924_0395"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute(
        """
        CREATE TABLE public.trading_root_market_tapes (
            case_id text PRIMARY KEY REFERENCES public.trading_cases(case_id) ON DELETE RESTRICT,
            tape_ref text,
            next_sample_at_ms bigint NOT NULL,
            expires_at_ms bigint NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_trading_root_market_tapes_due ON public.trading_root_market_tapes(next_sample_at_ms,case_id)"
    )


def downgrade() -> None:
    raise RuntimeError("rule_research_tape_forward_only: restore a verified pre-0396 archive")
