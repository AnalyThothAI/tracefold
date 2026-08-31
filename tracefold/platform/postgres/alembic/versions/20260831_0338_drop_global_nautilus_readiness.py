"""Drop the unowned global Nautilus readiness projection (#426).

Migration evidence:

- category: destructive hard-cut
- why_database_must_change: per-binding runtime rows and the live readiness seam replaced
  the global Nautilus heartbeat/readiness fields; retaining the unowned fields leaves stale
  durable readiness after the execution adapter stops
- current_source_revision: 20260830_0337
- minimum_supported_source_revision: 20260830_0337
- lock_level_and_order: one ACCESS EXCLUSIVE lock on the singleton
  trading_runtime_state table
- statement_timeout: 5s
- lock_timeout: 1s
- estimated_rows: 1
- estimated_bytes: four fixed-width/short scalar columns on one row
- rewrite_or_index_build: metadata-only column drop; no rewrite or index build
- preflight_and_maintenance_boundary: normal stopped-writer migration gate
- archive_current_compatibility: no compatibility path; per-binding runtime is the sole
  current readiness projection
- role_and_grant_impact: obsolete column grants disappear with the columns; no new grant
- failure_state: transactional DDL rolls back and business processes remain stopped
- roll_forward_or_verified_backup_restore: roll forward; use the verified pre-migration
  backup for rollback
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260831_0338
Revises: 20260830_0337
"""

from __future__ import annotations

from alembic import op

revision = "20260831_0338"
down_revision = "20260830_0337"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '1s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE trading_runtime_state
          DROP COLUMN nautilus_heartbeat_at_ms,
          DROP COLUMN nautilus_ready,
          DROP COLUMN nautilus_readiness_reason,
          DROP COLUMN nautilus_unexpected_exposure
        """
    )


def downgrade() -> None:
    raise RuntimeError("global_nautilus_readiness_hard_cut_downgrade_unsupported")
