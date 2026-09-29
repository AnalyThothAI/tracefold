"""Quarantine the task reads of a failed semantic revision.

Migration evidence:

- category: News semantic work extension (one additive column).
- why_database_must_change: a revision that ends failed must mark its material "read but failed" so a later
  revision of the same Event does not send it again; completion (`processed_read_refs`) must stay a success.
- current_source_revision: 20260929_0413
- minimum_supported_source_revision: 20260929_0413
- lock_level_and_order: ACCESS EXCLUSIVE on news_semantic_work for a metadata-only ADD COLUMN with a constant
  default; no rewrite.
- statement_timeout: 60s locally; lock_timeout: 5s locally.
- estimated_rows: no fact rewrite or backfill; existing rows start with an empty quarantine.
- preflight_and_maintenance_boundary: none; the old worker ignores the column.
- archive_current_compatibility: EventUpdate, observation and delivery facts are unchanged.
- role_and_grant_impact: none.
- failure_state: transactional DDL rollback.
- roll_forward_or_verified_backup_restore: forward only; an older worker keeps running and simply does not
  quarantine.

Revision ID: 20260929_0415
Revises: 20260929_0413
"""

from alembic import op

revision = "20260929_0415"
down_revision = "20260929_0413"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("ALTER TABLE public.news_semantic_work ADD COLUMN failed_read_refs text[] NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    raise RuntimeError("news_semantic_failed_reads_forward_only_restore_verified_backup")
