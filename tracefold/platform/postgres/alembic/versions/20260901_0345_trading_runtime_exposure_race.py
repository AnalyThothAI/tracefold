"""Remove the stale Runtime exposure projection constraint.

Migration evidence:

- category: runtime projection hard-cut
- why_database_must_change: account flatness and unexpected exposure can briefly
  describe different observation cutoffs; readiness already fails closed on
  unexpected exposure, while rejecting the transient pair crashes the Runtime
- current_source_revision: 20260901_0344
- minimum_supported_source_revision: 20260901_0344
- lock_level_and_order: short ALTER TABLE after the canonical migration stop
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: one current row per account slot
- estimated_bytes: catalog-only constraint removal; no heap rewrite
- rewrite_or_index_build: none
- preflight_and_maintenance_boundary: ordinary canonical migration stop
- role_and_grant_impact: none
- failure_state: the transaction rolls back completely
- roll_forward_or_verified_backup_restore: correct with a forward revision or
  restore the verified pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260901_0345
Revises: 20260901_0344
Create Date: 2026-09-01 15:50:00
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0345"
down_revision = "20260901_0344"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("ALTER TABLE trading_execution_runtime_state DROP CONSTRAINT trading_execution_runtime_exposure_check")


def downgrade() -> None:
    raise RuntimeError("trading_runtime_exposure_projection_forward_only")
