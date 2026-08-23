"""Source-artifact identity for provider Items (#154).

The Deduper's identity is derived from the *text*, and that derivation is deliberately weak: a comparison
title of fewer than three tokens is not shareable at all, and an Event's family window is 12 h. Both guards
are correct — a fingerprint over `what a coincidence` would merge unrelated Items — but they leave the
provider's own exact identity unused. Two records carrying the same tweet are the same source artifact
whatever their text scores, and `canonical_url` cannot stand in for it: 17 of 29 repeat ingests in a 30-day
window differed only in URL spelling (`twitter.com` vs `x.com`, `coindesk` vs `CoinDesk` — `_article_url`
lowercases the host but not the path).

This is an identity column, not a fact and not a decision: every value is reproducible from the row's own
`canonical_url`, which is why the backfill can compute it in SQL. `tracefold.news.opennews` owns the rule
going forward and the two must agree; `test_source_artifact_backfill_matches_the_parser` pins that.

The index is partial because only x/twitter frames have one — 3174 of 9312 Items in a 30-day window.

Revision ID: 20260823_0299
Revises: 20260822_0298
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260823_0299"
down_revision = "20260822_0298"
branch_labels = None
depends_on = None

# Kept identical to `_STATUS_URL_RE` in `tracefold.news.opennews`, POSIX-flavoured.
_STATUS_RE = r"^https?://(www\.)?(x|twitter)\.com/[^/]+/status(es)?/[0-9]{5,25}([/?#]|$)"
_STATUS_CAPTURE = r"/status(?:es)?/([0-9]{5,25})"


def upgrade() -> None:
    op.execute("ALTER TABLE news_items ADD COLUMN source_artifact_id TEXT NOT NULL DEFAULT ''")
    # One-time backfill of the 30-day retained window. The rule is the same one the parser applies, so a row
    # ingested before this migration and one ingested after resolve to the same artifact.
    # Both patterns are bound rather than interpolated: `(?:` reads as a `:...` bind parameter otherwise.
    op.execute(
        sa.text(
            """
            UPDATE news_items
               SET source_artifact_id = 'x:' || substring(canonical_url from :capture)
             WHERE canonical_url IS NOT NULL
               AND canonical_url ~* :match
            """
        ).bindparams(capture=_STATUS_CAPTURE, match=_STATUS_RE)
    )
    # The lookup is "this artifact, this fingerprint, inside the artifact window" — always for a non-empty id.
    op.execute(
        "CREATE INDEX ix_news_items_source_artifact ON news_items (source_artifact_id) WHERE source_artifact_id <> ''"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_news_items_source_artifact")
    op.execute("ALTER TABLE news_items DROP COLUMN IF EXISTS source_artifact_id")
