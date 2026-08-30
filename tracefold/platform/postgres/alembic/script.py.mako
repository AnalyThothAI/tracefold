"""${message}

Migration evidence:

- category: TODO(additive | index | privilege | backfill | destructive hard-cut)
- why_database_must_change: TODO
- current_source_revision: ${down_revision | comma,n}
- minimum_supported_source_revision: TODO
- lock_level_and_order: TODO
- statement_timeout: TODO
- lock_timeout: TODO
- estimated_rows: TODO
- estimated_bytes: TODO
- rewrite_or_index_build: TODO
- preflight_and_maintenance_boundary: TODO
- archive_current_compatibility: TODO
- role_and_grant_impact: TODO
- failure_state: TODO
- roll_forward_or_verified_backup_restore: TODO
- production_postgres_image: TODO(exact major, family, and digest)

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from alembic import op
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else 'raise NotImplementedError("complete the migration evidence and implementation before running this revision")'}


def downgrade() -> None:
    ${downgrades if downgrades else 'raise RuntimeError("irreversible migration; use the verified backup-restore path recorded above")'}
