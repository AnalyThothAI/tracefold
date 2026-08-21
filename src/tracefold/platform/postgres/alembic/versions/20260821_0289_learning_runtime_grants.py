"""Repair the production learning-plane runtime grants.

The 0288 rollout proved that container readiness cannot substitute for a
role-authentic write probe: Workers reached the new evidence table only when
the first post-migration Event arrived.  Reassert the exact append contract in
its own revision so an upgrade from the already-deployed 0288 state is safe
and repeatable.

Revision ID: 20260821_0289
Revises: 20260821_0288
"""

from __future__ import annotations

from alembic import op

revision = "20260821_0289"
down_revision = "20260821_0288"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT, INSERT ON news_event_evidence_snapshots TO tracefold_workers")
    op.execute("REVOKE UPDATE, DELETE ON news_event_evidence_snapshots FROM tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("20260821_0289 is an irreversible learning-runtime grant repair")
