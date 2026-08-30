"""Grant Nautilus the canonical JSON function used by its risk-event writer (#375).

Migration evidence:

- category: privilege
- why_database_must_change: Nautilus appends capital risk events whose database-owned
  hash constraint invokes this function; its runtime role must be able to execute it
- current_source_revision: 20260830_0336
- minimum_supported_source_revision: 20260830_0336
- lock_level_and_order: function ACL only; no table lock
- statement_timeout: 5s
- lock_timeout: 1s
- estimated_rows: 0
- estimated_bytes: 0
- rewrite_or_index_build: none
- preflight_and_maintenance_boundary: normal stopped-writer migration gate
- archive_current_compatibility: none; News genesis remains unchanged
- role_and_grant_impact: tracefold_nautilus receives EXECUTE only on
  trading_canonical_jsonb(JSONB)
- failure_state: transactional DDL rolls back and business processes remain stopped
- roll_forward_or_verified_backup_restore: roll forward; use the verified pre-migration
  backup for rollback
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260830_0337
Revises: 20260830_0336
"""

from __future__ import annotations

from alembic import op

revision = "20260830_0337"
down_revision = "20260830_0336"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '1s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute("GRANT EXECUTE ON FUNCTION trading_canonical_jsonb(JSONB) TO tracefold_nautilus")


def downgrade() -> None:
    raise RuntimeError("20260830_0337 is an irreversible runtime-role privilege correction")
