"""Remove the evidence-append row-lock privilege dependency.

The repository's latest-snapshot read used ``FOR SHARE``. PostgreSQL requires
UPDATE privilege for locking SELECTs, even though the immutable table rejects
every UPDATE through a trigger. The raw queue already has one runtime writer,
and a shared row lock did not serialize two appenders anyway. Keep the narrow
SELECT/INSERT ACL and reassert it after the code drops the ineffective lock.

Revision ID: 20260821_0290
Revises: 20260821_0289
"""

from __future__ import annotations

from alembic import op

revision = "20260821_0290"
down_revision = "20260821_0289"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT, INSERT ON news_event_evidence_snapshots TO tracefold_workers")
    op.execute("REVOKE UPDATE, DELETE ON news_event_evidence_snapshots FROM tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("20260821_0290 is an irreversible evidence-append contract repair")
