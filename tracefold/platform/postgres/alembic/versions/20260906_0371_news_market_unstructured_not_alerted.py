"""Unstructured records get no notification state, and smart money keeps no live action (#582).

Migration evidence:

- category: one delete of notification state (not facts), one CHECK tightened, two columns dropped;
  all on `news_market_tracks`
- why_database_must_change: two shapes stop existing in the same change, and both of them are
  alerting state rather than record of what a provider said.

  A record whose template could not be proved was the loop's fourth rule branch: it became its own
  group `raw|<kind>|<item_id>`, its own track and its own card, outside every suppression rule.
  Production sent four such cards in the feature's whole life -- two `Deposit` lines and two BTC
  opens that the smart-money parser has since learned to read (#560) -- and each of them interrupted
  a reader with a sentence nobody could act on. #582 §3.2 deletes the branch: the record is still
  stored, still parsed as far as it can be, and still on the page; it simply earns no card, so it
  needs no track. The four `family = 'raw'` rows here are that deleted branch's leftover state, and
  the CHECK is tightened so the shape cannot come back by accident. Their `news_items` rows and
  their `news_market_deliveries` receipts are untouched: the observations are facts, and the
  receipts are evidence that a reader was told.

  `current_action` and `current_position_side` were the newest observation, rewritten every turn.
  The rule that read them -- smart money's "the action or the side changed, send a card now" -- is
  deleted with them (#582 §3.1): the side is not a trigger at all, and what a round's second card
  depends on is `anchor_action`, what the last *delivered* card ended on, which is a different
  column that stays. Two columns whose only reader is gone are two columns that can disagree with
  the anchor beside them.
- current_source_revision: 20260906_0370
- minimum_supported_source_revision: 20260906_0370
- lock_level_and_order: one transaction on `news_market_tracks` only -- a delete of at most a handful
  of rows, then `ACCESS EXCLUSIVE` for the constraint swap and the two column drops. No other table
  is touched
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: one row per notification group -- a few hundred at the measured intake -- of which
  the delete removes the `raw` ones (four in production on 2026-09-06). The CHECK is added `NOT
  VALID` first and validated after, so the table is read once without holding the exclusive lock
- estimated_bytes: two dropped `text` columns are catalog-only (`DROP COLUMN` marks them dropped and
  does not rewrite the heap), plus the deleted rows
- rewrite_or_index_build: neither. `DROP COLUMN` on a nullable text column is a catalog update;
  `ADD CONSTRAINT ... NOT VALID` followed by `VALIDATE CONSTRAINT` takes `SHARE UPDATE EXCLUSIVE`
  for the scan rather than blocking readers
- preflight_and_maintenance_boundary: the columns must be dropped with the writer stopped, because
  code that still names them fails against the new schema. `make up` stops Workers before migrating,
  which is that boundary; nothing further is required
- archive_current_compatibility: every fact and every receipt is kept. What is deleted is alerting
  state for groups that will never be alerted again, and two columns no rule reads. An unstructured
  record admitted after this revision is marked `processed` with its group key and no track, which
  the read model reports as `not_alerted` / `unstructured_record_not_alerted` -- a fourth answer
  beside `merging`, `uncovered` and `historical`, not a silence
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and `news_market_tracks` keeps its rows, its
  CHECK and both columns
- roll_forward_or_verified_backup_restore: `downgrade` is refused. Re-adding the columns would
  restore two columns with no writer, and the deleted rows are the state of a rule that no longer
  exists -- recreating them would put groups back on a page as though a card were still coming for
  them. Roll forward with a new revision
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260906_0371
Revises: 20260906_0370
Create Date: 2026-09-06 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260906_0371"
down_revision = "20260906_0370"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    # Notification state for a branch that no longer exists. The observations themselves are
    # `news_items` rows and are not touched; nor are the delivery receipts that name these groups.
    op.execute("DELETE FROM public.news_market_tracks WHERE family = 'raw'")
    op.execute("ALTER TABLE public.news_market_tracks DROP CONSTRAINT news_market_tracks_family_check")
    op.execute(
        """
        ALTER TABLE public.news_market_tracks
          ADD CONSTRAINT news_market_tracks_family_check
            CHECK (family = ANY (ARRAY['oi'::text, 'liquidation'::text, 'smart_money'::text]))
            NOT VALID
        """
    )
    op.execute("ALTER TABLE public.news_market_tracks VALIDATE CONSTRAINT news_market_tracks_family_check")
    # The newest observation, written every turn by a rule that is gone. `anchor_action` -- what the
    # last delivered card ended on -- is the one the remaining rules read, and it stays.
    op.execute(
        """
        ALTER TABLE public.news_market_tracks
          DROP COLUMN current_action,
          DROP COLUMN current_position_side
        """
    )


def downgrade() -> None:
    """Refused. The deleted rows are the state of a rule that no longer exists."""

    raise RuntimeError("news_market_unstructured_not_alerted_downgrade_unsupported")
