"""Replace source-only completion with versioned Event task reads.

Migration evidence:

- category: News semantic work identity and insert-only observation extension.
- why_database_must_change: one source can be read for several independent Event scopes.
- current_source_revision: 20260928_0408
- minimum_supported_source_revision: 20260928_0408
- lock_level_and_order: drain News semantic writers; alter work then observations.
- statement_timeout: 300s locally; lock_timeout: 5s locally.
- estimated_rows: no fact rewrite or backfill; existing source-only markers cannot
  prove a task scope and therefore are not promoted to completed task reads.
- preflight_and_maintenance_boundary: stop the old News worker before migration.
- archive_current_compatibility: old observations retain their source refs as audit
  data; they have no asserted task reads. EventUpdate and delivery facts are unchanged.
- role_and_grant_impact: none.
- failure_state: transactional DDL rollback.
- roll_forward_or_verified_backup_restore: old worker cannot write this schema;
  restore a verified pre-cut archive or roll forward with the new worker.

Revision ID: 20260928_0409
Revises: 20260928_0408
"""

from alembic import op

revision = "20260928_0409"
down_revision = "20260928_0408"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    op.execute("ALTER TABLE public.news_semantic_work ADD COLUMN processed_read_refs text[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE public.news_semantic_work ADD COLUMN reanalysis_read_ref text")
    op.execute("ALTER TABLE public.news_semantic_work ADD COLUMN reanalysis_reason text")
    op.execute("ALTER TABLE public.news_semantic_work ADD COLUMN reanalysis_head_ref text")
    op.execute("ALTER TABLE public.news_semantic_observations ADD COLUMN read_refs text[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE public.news_semantic_observations ADD COLUMN reanalysis_reason text")
    op.execute("ALTER TABLE public.news_semantic_observations ADD COLUMN reanalysis_head_ref text")
    op.execute("ALTER TABLE public.news_semantic_work DROP COLUMN processed_evidence_refs")
    op.execute("DROP INDEX public.news_notification_decisions_input")
    op.execute(
        "CREATE INDEX news_notification_decisions_editorial_input ON "
        "public.news_notification_decisions(event_id,channel,input_digest,created_at_ms DESC) "
        "WHERE input_digest IS NOT NULL"
    )
    op.execute("ALTER TABLE public.news_delivery_queue ADD COLUMN card_copy_input_digest text")
    op.execute("ALTER TABLE public.news_delivery_queue ADD COLUMN card_copy_document jsonb")
    op.execute("ALTER TABLE public.news_deliveries ADD COLUMN card_copy_input_digest text")
    op.execute("ALTER TABLE public.news_deliveries ADD COLUMN card_copy_document jsonb")
    op.execute(
        "CREATE INDEX news_delivery_queue_copy_input ON public.news_delivery_queue(card_copy_input_digest) "
        "WHERE kind='update' AND card_copy_document IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX news_deliveries_copy_input ON public.news_deliveries(card_copy_input_digest) "
        "WHERE kind='update' AND card_copy_document IS NOT NULL"
    )


def downgrade() -> None:
    raise RuntimeError("news_task_reads_forward_only_restore_verified_backup")
