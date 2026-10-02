"""#764: bounded outstanding semantic jobs and recent failure reads.

Migration evidence:
- category: additive read-path indexes; no data rewrite.
- why_database_must_change: the consolidated jobs ledger lost the predecessor's outstanding
  and failure lookup paths; three semantic scheduler/status reads scan unrelated and completed jobs.
- current_source_revision: 20261001_0424
- minimum_supported_source_revision: 20261001_0424
- lock_level_and_order: SHARE on news_jobs, outstanding index followed by failed index.
- statement_timeout: 120s.
- lock_timeout: 5s.
- estimated_rows: 13,604 total jobs in the frozen production rehearsal; index only matching jobs.
- estimated_bytes: under 1 MiB of additional indexes at the measured production size.
- rewrite_or_index_build: two partial B-tree indexes; existing facts and job detail stay unchanged.
- preflight_and_maintenance_boundary: verified full backup; stop writers through the supported
  migrate-before-start workflow, including Analysis then drained Executor because image head changes.
- archive_current_compatibility: no tables, columns, facts, leases or retry budgets are removed.
- role_and_grant_impact: none; existing non-superuser owner creates the indexes.
- failure_state: transactional DDL rollback; no partial head advancement.
- roll_forward_or_verified_backup_restore: use the matching new image after success; on failure
  restart the predecessor only after confirming the database is still at 0424.
- production_postgres_image: postgres:18-bookworm, PostgreSQL 18.4,
  sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296.
"""

from alembic import op

revision = "20261002_0425"
down_revision = "20261001_0424"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("""
        CREATE INDEX news_jobs_semantic_outstanding ON public.news_jobs (next_attempt_at_ms, subject_id)
         WHERE job_kind='semantic'
           AND (detail->>'done_revision' IS NULL
                OR (detail->>'done_revision')::integer < (detail->>'wanted_revision')::integer)
    """)
    # Completed revisions can retain a failed outcome; counting only state='failed' would lose them.
    op.execute("""
        CREATE INDEX news_jobs_semantic_failed ON public.news_jobs (updated_at_ms)
         WHERE job_kind='semantic' AND detail->>'last_outcome'='failed'
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX public.news_jobs_semantic_failed")
    op.execute("DROP INDEX public.news_jobs_semantic_outstanding")
