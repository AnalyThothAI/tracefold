"""Record deterministic scope retractions separately from model observations.

Forward only. Stop News writers for the image/schema switch and restore a verified
matching backup on failure. Existing updates, source evidence and send receipts stay
unchanged; only new revisions may name an operator scope repair.

Revision ID: 20260928_0410
Revises: 20260928_0409
"""

from alembic import op

revision = "20260928_0410"
down_revision = "20260928_0409"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    op.execute(
        """
        CREATE TABLE public.news_head_scope_repairs (
          repair_id text PRIMARY KEY,
          event_id text NOT NULL REFERENCES public.news_events(event_id) ON DELETE CASCADE,
          previous_content_revision text NOT NULL,
          content_revision text NOT NULL,
          claim_refs text[] NOT NULL,
          proof jsonb NOT NULL,
          projection_version text NOT NULL,
          recorded_at_ms bigint NOT NULL CHECK (recorded_at_ms >= 0),
          CONSTRAINT news_head_scope_repairs_previous_fkey
            FOREIGN KEY (event_id,previous_content_revision)
            REFERENCES public.news_event_updates(event_id,content_revision),
          CONSTRAINT news_head_scope_repairs_shape_check CHECK (
            cardinality(claim_refs) > 0 AND jsonb_typeof(proof) = 'object'
            AND content_revision ~ '^[0-9a-f]{64}$'
            AND previous_content_revision ~ '^[0-9a-f]{64}$'
            AND content_revision <> previous_content_revision),
          CONSTRAINT news_head_scope_repairs_target_unique UNIQUE (event_id,content_revision)
        )
        """
    )
    op.execute("ALTER TABLE public.news_event_updates ALTER COLUMN observation_result_id DROP NOT NULL")
    op.execute("ALTER TABLE public.news_event_updates ADD COLUMN scope_repair_id text")
    op.execute(
        "ALTER TABLE public.news_event_updates ADD CONSTRAINT news_event_updates_scope_repair_fkey "
        "FOREIGN KEY (scope_repair_id) REFERENCES public.news_head_scope_repairs(repair_id)"
    )
    op.execute(
        "ALTER TABLE public.news_event_updates ADD CONSTRAINT news_event_updates_origin_check "
        "CHECK ((observation_result_id IS NULL) <> (scope_repair_id IS NULL))"
    )
    op.execute("CREATE UNIQUE INDEX ux_news_event_updates_scope_repair ON public.news_event_updates(scope_repair_id)")


def downgrade() -> None:
    raise RuntimeError("news_head_scope_repairs_forward_only_restore_verified_backup")
