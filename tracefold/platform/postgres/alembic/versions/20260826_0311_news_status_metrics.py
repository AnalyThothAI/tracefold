"""Promote News pipeline status metrics out of the verdict trace (#221).

Revision ID: 20260826_0311
Revises: 20260826_0310
"""

from __future__ import annotations

from alembic import op

revision = "20260826_0311"
down_revision = "20260826_0310"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE news_verdicts "
        "ADD COLUMN latency_ms DOUBLE PRECISION "
        "GENERATED ALWAYS AS ((trace ->> 'latency_ms')::double precision) STORED, "
        "ADD COLUMN queue_lag_ms DOUBLE PRECISION "
        "GENERATED ALWAYS AS ((trace ->> 'queue_lag_ms')::double precision) STORED, "
        "ADD COLUMN reasked_after_told_change BOOLEAN "
        "GENERATED ALWAYS AS (COALESCE((trace ->> 'reasked_after_told_change')::boolean, false)) STORED, "
        "ADD COLUMN novelty_defaulted BOOLEAN "
        "GENERATED ALWAYS AS (COALESCE((trace ->> 'novelty_defaulted')::boolean, false)) STORED, "
        "ADD COLUMN seen_scope TEXT "
        "GENERATED ALWAYS AS (COALESCE(trace ->> 'seen_scope', '')) STORED"
    )


def downgrade() -> None:
    raise RuntimeError("20260826_0311 is an irreversible metric promotion; restore a backup to downgrade")
