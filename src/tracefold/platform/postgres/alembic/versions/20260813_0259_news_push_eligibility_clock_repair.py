"""Repair and require the News Push eligibility clock.

Revision ID: 20260813_0259
Revises: 20260813_0258
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0259"
down_revision = "20260813_0258"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("SET LOCAL transaction_timeout = '60s'")
    op.execute(
        r"""
        UPDATE news_items
           SET push_eligibility_updated_at_ms = coalesce(
                 provider_score_updated_at_ms,
                 first_observed_at_ms
               )
         WHERE jsonb_typeof(provider_metadata -> 'score') = 'number'
           AND push_eligibility_updated_at_ms IS NULL;

        ALTER TABLE news_items
          ADD CONSTRAINT news_items_numeric_score_push_eligibility_clock_check
          CHECK (
            jsonb_typeof(provider_metadata -> 'score') IS DISTINCT FROM 'number'
            OR push_eligibility_updated_at_ms IS NOT NULL
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260813_0259 is an irreversible eligibility-clock invariant cut")
