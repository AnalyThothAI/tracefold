"""#799: bound historical claim backfill by source-document cursors.

Migration evidence:
- category: replace timestamp indexes with reversible composite cursor indexes.
- why_database_must_change: resume pages need an ordered timestamp and stable document ID.
- current_source_revision: 20261002_0427
- minimum_supported_source_revision: 20261002_0427
- lock_level_and_order: ACCESS EXCLUSIVE on news_analyses, then news_notifications; writers stopped.
- statement_timeout: 600s; lock_timeout: 5s.
- estimated_rows: approximately 75k historical claims across adopted documents and frozen receipts.
- estimated_bytes: two compact B-tree indexes over timestamps and document IDs; no fact rewrite.
- rewrite_or_index_build: two partial B-tree replacements; counters use the same timestamp prefix.
- preflight_and_maintenance_boundary: verified backup and migrate-before-start, with News writers stopped.
- archive_current_compatibility: all adopted documents, frozen receipts and vectors remain unchanged.
- role_and_grant_impact: none; no extensions or new privileges.
- failure_state: transactional DDL rollback, leaving revision 0427 and existing indexes intact.
- roll_forward_or_verified_backup_restore: restore the two single-column indexes to 0427 before its matching image.
- production_postgres_image: postgres:18-bookworm; isolated PostgreSQL 18.6 verification.
"""

from alembic import op

revision = "20261002_0428"
down_revision = "20261002_0427"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.execute("""
        DROP INDEX public.news_analyses_adopted;
        CREATE INDEX news_analyses_adopted ON public.news_analyses(adopted_at_ms,analysis_id)
          WHERE adopted_at_ms IS NOT NULL;
        DROP INDEX public.news_notifications_sent;
        CREATE INDEX news_notifications_sent ON public.news_notifications(settled_at_ms,intent_id)
          WHERE kind='update' AND state='sent';
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.execute("""
        DROP INDEX public.news_analyses_adopted;
        CREATE INDEX news_analyses_adopted ON public.news_analyses(adopted_at_ms)
          WHERE adopted_at_ms IS NOT NULL;
        DROP INDEX public.news_notifications_sent;
        CREATE INDEX news_notifications_sent ON public.news_notifications(settled_at_ms)
          WHERE kind='update' AND state='sent';
    """)
