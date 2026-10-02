"""#764: bounded outstanding semantic jobs and recent failure reads.

Migration evidence:
- category: additive read-path indexes and a transactional reader generation; no fact rewrite.
- why_database_must_change: the consolidated jobs ledger lost the predecessor's outstanding
  and failure lookup paths; three semantic scheduler/status reads scan unrelated and completed jobs.
- current_source_revision: 20261001_0424
- minimum_supported_source_revision: 20261001_0424
- lock_level_and_order: SHARE on news_jobs, then SHARE ROW EXCLUSIVE for reader triggers;
  stop News writers first. Permission writers lock Event, its job/intent, then reader clock;
  fact triggers lock the clock at commit.
- statement_timeout: 120s.
- lock_timeout: 5s.
- estimated_rows: 13,604 total jobs in the frozen production rehearsal; index only matching jobs.
- estimated_bytes: under 1 MiB of additional indexes at the measured production size.
- rewrite_or_index_build: two partial B-tree indexes, one singleton counter table
  and deferred AFTER constraint triggers;
  existing facts and job detail stay unchanged.
- preflight_and_maintenance_boundary: verified full backup; stop writers through the supported
  migrate-before-start workflow, including Analysis then drained Executor because image head changes.
- archive_current_compatibility: no facts, leases or retry budgets are removed; News owns 18 tables,
  27 total. The counter is a CAS witness and never substitutes for an analysis or receipt.
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
           AND state IN ('pending','failed')
    """)
    # Completed revisions can retain a failed outcome; counting only state='failed' would lose them.
    op.execute("""
        CREATE INDEX news_jobs_semantic_failed ON public.news_jobs (updated_at_ms)
         WHERE job_kind='semantic' AND detail->>'last_outcome'='failed'
    """)

    # This counter fences permission snapshots only; the analyses and receipts remain the facts.
    # It avoids unrelated database/Trading XIDs invalidating every News permission read.
    # Defer fact triggers until commit: admission may write Item/member before Event,
    # so an immediate counter lock would invert the permission writer's Event -> clock order.
    # The fact and its generation are still visible atomically to every other transaction.
    op.execute("""
        CREATE TABLE public.news_reader_clock (
          singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
          revision bigint NOT NULL DEFAULT 0 CHECK (revision>=0)
        ) WITH (fillfactor=85);
        INSERT INTO public.news_reader_clock(singleton) VALUES (true);
        CREATE FUNCTION public.news_reader_advance() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          UPDATE public.news_reader_clock SET revision=revision+1 WHERE singleton;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER news_reader_analysis_insert AFTER INSERT ON public.news_analyses
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (NEW.adopted_at_ms IS NOT NULL) EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_analysis_update AFTER UPDATE ON public.news_analyses
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (NEW.adopted_at_ms IS NOT NULL AND
            (OLD.adopted_at_ms IS DISTINCT FROM NEW.adopted_at_ms OR OLD.document IS DISTINCT FROM NEW.document))
          EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_analysis_delete AFTER DELETE ON public.news_analyses
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (OLD.adopted_at_ms IS NOT NULL) EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_notification_insert AFTER INSERT ON public.news_notifications
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (NEW.kind='update' AND NEW.state IN ('sending','sent','ambiguous'))
          EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_notification_update AFTER UPDATE ON public.news_notifications
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (NEW.kind='update' AND
            (NEW.state IN ('sending','sent','ambiguous') OR OLD.state IN ('sending','sent','ambiguous')) AND
            (NEW.state,NEW.card,NEW.receipt,NEW.claim_refs,NEW.sent_claims,NEW.history_context,NEW.settled_at_ms)
            IS DISTINCT FROM
            (OLD.state,OLD.card,OLD.receipt,OLD.claim_refs,OLD.sent_claims,OLD.history_context,OLD.settled_at_ms))
          EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_notification_delete AFTER DELETE ON public.news_notifications
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (OLD.kind='update' AND OLD.state IN ('sending','sent','ambiguous'))
          EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_membership AFTER INSERT OR UPDATE OR DELETE ON public.news_event_members
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_item_metadata AFTER UPDATE ON public.news_items
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (NEW.provider_metadata IS DISTINCT FROM OLD.provider_metadata)
          EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_event_kind AFTER UPDATE ON public.news_events
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (NEW.event_kind IS DISTINCT FROM OLD.event_kind)
          EXECUTE FUNCTION public.news_reader_advance();
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for trigger, table in (
        ("news_reader_analysis_insert", "news_analyses"),
        ("news_reader_analysis_update", "news_analyses"),
        ("news_reader_analysis_delete", "news_analyses"),
        ("news_reader_notification_insert", "news_notifications"),
        ("news_reader_notification_update", "news_notifications"),
        ("news_reader_notification_delete", "news_notifications"),
        ("news_reader_membership", "news_event_members"),
        ("news_reader_item_metadata", "news_items"),
        ("news_reader_event_kind", "news_events"),
    ):
        op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
    op.execute("DROP FUNCTION public.news_reader_advance()")
    op.execute("DROP TABLE public.news_reader_clock")
    op.execute("DROP INDEX public.news_jobs_semantic_failed")
    op.execute("DROP INDEX public.news_jobs_semantic_outstanding")
