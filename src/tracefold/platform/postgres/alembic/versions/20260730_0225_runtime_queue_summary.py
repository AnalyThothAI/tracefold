"""Persist projection quarantine count in worker runtime queue summaries."""

from __future__ import annotations

from alembic import op

revision = "20260730_0225"
down_revision = "20260730_0224"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE worker_runtime_status
          ADD COLUMN quarantine_count bigint NOT NULL DEFAULT 0,
          ADD CONSTRAINT worker_runtime_status_quarantine_count_check
            CHECK (quarantine_count >= 0);
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260730_0225 is an irreversible runtime-status hard cut")
