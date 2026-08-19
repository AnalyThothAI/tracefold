"""Listing admission publishes (#72): widen the unpublished-candidate index to every admitted admission.

`listing_deterministic` is an admitted state — the funnel, the outcome vocabulary and the re-gate set have always
counted it alongside `candidate` — but the Deduper published only `candidate` and the Janitor's rescue index was
partial on `admission = 'candidate'`, so exchange listing/delisting Events died between the Gate and the queue.
The consumer fix alone would leave the Janitor blind to a crash between event-create and publish; this widens the
index predicate so the rescue path covers listing Events too.

Revision ID: 20260820_0279
Revises: 20260819_0278
"""

from __future__ import annotations

from alembic import op

revision = "20260820_0279"
down_revision = "20260819_0278"
branch_labels = None
depends_on = None

_INDEX = "ix_news_events_unpublished"
_ADMITTED = "(admission = 'candidate'::text OR admission = 'listing_deterministic'::text)"
_CANDIDATE_ONLY = "admission = 'candidate'::text"


def upgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute(
        f"CREATE INDEX {_INDEX} ON public.news_events USING btree (opened_at_ms) "
        f"WHERE (published_at_ms IS NULL AND {_ADMITTED})"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute(
        f"CREATE INDEX {_INDEX} ON public.news_events USING btree (opened_at_ms) "
        f"WHERE (published_at_ms IS NULL AND {_CANDIDATE_ONLY})"
    )
