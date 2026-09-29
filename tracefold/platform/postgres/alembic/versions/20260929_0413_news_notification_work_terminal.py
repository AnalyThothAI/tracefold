"""News notification work gets a visible failed end state and loses its always-empty plan column (#742).

`failed` is terminal until an operator retries that exact content revision, and it always carries the
error code of the failure that ended it. The `plan` column has been NULL since 0407 moved plans into
`news_notification_decisions`; it is dropped and the plan check no longer names it.

Work the previous code exhausted (pending, three attempts, already overdue) was never picked up again and
showed no reason. It becomes `failed` with `news_notification_exhausted_legacy`, so it is visible and
retryable. Exhausted work that was not yet due when the migration runs stays pending: the new code plans it
once more, and its next real failure fails it.

Forward only. Stop the News Deliverer for the schema/image switch; the previous image writes `plan` and
cannot run against this schema. Restore the verified backup with its matching image for rollback.
"""

from alembic import op

revision = "20260929_0413"
down_revision = "20260928_0412"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    op.execute("""
        ALTER TABLE public.news_notification_work DROP CONSTRAINT news_notification_work_plan_check;
        ALTER TABLE public.news_notification_work DROP CONSTRAINT news_notification_work_state_check;
        ALTER TABLE public.news_notification_work DROP COLUMN plan;
        ALTER TABLE public.news_notification_work ADD COLUMN last_error_code text;
        UPDATE public.news_notification_work
           SET state = 'failed', last_error_code = 'news_notification_exhausted_legacy',
               updated_at_ms = floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint
         WHERE state = 'pending' AND attempts >= 3
           AND next_attempt_at_ms <= floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint;
        ALTER TABLE public.news_notification_work ADD CONSTRAINT news_notification_work_state_check
          CHECK (state IN ('pending', 'done', 'failed'));
        ALTER TABLE public.news_notification_work ADD CONSTRAINT news_notification_work_plan_check CHECK (
          content_revision ~ '^[0-9a-f]{64}$'
          AND (state <> 'done' OR decision_ref IS NOT NULL)
          AND (state <> 'failed' OR last_error_code IS NOT NULL));
    """)


def downgrade() -> None:
    raise RuntimeError("news_notification_work_terminal_forward_only: restore verified backup and matching image")
