"""A notification group records where its current alert round started (#562 PR-F).

Migration evidence:

- category: one additive `NOT NULL DEFAULT 0` bigint column on `news_market_tracks`, and one
  single-statement backfill over the same table
- why_database_must_change: `market_adopt_unclaimed` hands a group's un-claimed observations to its
  un-started card with no lower bound, so an observation the rules deliberately suppressed hours ago
  is swept into whatever card comes next. Production showed it the day the loop was deployed: the
  first OI card for MARSCOIN covered a 01:20 observation that had been held below the follow-up
  threshold together with the 07:34 observation that opened a new round after the 4 h quiet reset,
  and reported the span as `01:20-07:34` -- a card claiming a round it did not belong to (#553 §4.2).

  The bound is the group's current alert round start, and it belongs in PostgreSQL for the same
  reason the rest of the track does: a round outlives the process that opened it, and the reset that
  ended the previous round is a decision the rules made once, at a moment only the loop saw. The
  alternative -- recomputing the bound from the observations at adoption time -- would be a second
  copy of the quiet-reset rule, in SQL, able to disagree with the branch that actually decided it.
  It is one column rather than a table because it is one number per group and it is only ever read
  with the rest of the track.
- current_source_revision: 20260905_0366
- minimum_supported_source_revision: 20260905_0366
- lock_level_and_order: `ACCESS EXCLUSIVE` on `news_market_tracks` for the catalog change, then one
  sequential update of that same table inside the same transaction. Nothing else is touched
- statement_timeout: 120s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: one row per notification group. The table was created yesterday by
  `20260905_0366` and holds a few hundred rows at the measured 208 market observations a day; the
  backfill is one pass over it
- estimated_bytes: eight bytes per row, plus nothing -- no index is built and no other table changes
- rewrite_or_index_build: `ADD COLUMN ... NOT NULL DEFAULT 0` stores the default in the catalog and
  does not rewrite the heap. The backfill then writes the real value per row, which is an ordinary
  update at these row counts. No index is created: every read of this column already has the track
  row in hand, by primary key
- preflight_and_maintenance_boundary: none beyond the ordinary deploy. A writer on the old code
  upserts the track without naming the column and takes the default, which is the behaviour that
  exists today; new code against the old schema is what fails, and `make up` stops Workers before
  migrating, which is the boundary the deploy already has
- archive_current_compatibility: every existing row keeps every value it had. The backfill sets each
  group's round start to its last send attempt, which is the newest moment the group is known to
  have interrupted a reader: observations older than that were either covered by that card or held
  by a rule in a round that has ended, and observations newer than it belong to the round now open.
  A group that has never been sent keeps 0, so its first card still speaks for everything it holds
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and `news_market_tracks` keeps its current
  shape and contents
- roll_forward_or_verified_backup_restore: `downgrade` is refused. The round starts exist nowhere
  else -- they are the loop's own record of which alert round each group is in -- and dropping them
  would return every group to an unbounded adoption that sweeps suppressed observations from ended
  rounds into the next card, which is the exact defect this revision closes. Roll forward with a new
  revision
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260905_0367
Revises: 20260905_0366
Create Date: 2026-09-05 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260905_0367"
down_revision = "20260905_0366"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    op.execute(
        """
        ALTER TABLE public.news_market_tracks
          ADD COLUMN round_started_at_ms bigint NOT NULL DEFAULT 0
        """
    )
    # The last send attempt is where the round now open began for every group that has ever sent a
    # card: what came before it was either on that card or held in a round that has ended.
    op.execute(
        """
        UPDATE public.news_market_tracks
           SET round_started_at_ms = COALESCE(anchor_attempt_at_ms, 0)
        """
    )


def downgrade() -> None:
    """Refused. These are the only record of which alert round each group is currently in."""

    raise RuntimeError("news_market_alert_round_downgrade_unsupported")
