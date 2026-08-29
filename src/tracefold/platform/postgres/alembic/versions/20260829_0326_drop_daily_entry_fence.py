"""Drop the one-entry-per-UTC-day fence (#348).

The fence capped how many theses the lane could act on in a day. It never bounded exposure: that is
`ux_trading_intents_one_active`, a unique index admitting a single nonterminal Intent, which this
migration leaves untouched. What the daily cap actually produced was a blind spot — after the day's
first entry every later frame was refused *before* the policy ran, so on exactly the days the lane
was working it could not say which of the day's remaining frames it should have taken. Measured over
seven days of production frames it would have capped the busiest day at one of six qualifying frames.

One-way. Recreating the index would fail against any day that has since taken more than one entry,
and re-imposing a throughput cap is a product decision, not a rollback.

Revision ID: 20260829_0326
Revises: 20260829_0325
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0326"
down_revision = "20260829_0325"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_trading_intents_one_entry_per_utc_day")


def downgrade() -> None:
    raise RuntimeError("daily_entry_fence_downgrade_unsupported")
