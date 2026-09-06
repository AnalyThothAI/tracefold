"""The wallet tape's four-hourly digest becomes a third kind of wallet observation (#572 PR-3).

Migration evidence:

- category: three CHECK constraints on `news_market_wallet_events` replaced -- the kind vocabulary, the
  identity predicate and the window predicate -- plus two indexes on that same table. No existing
  column, row or index is rewritten, and every row that was valid before is still valid.
- why_database_must_change: a digest is an observation about a *window*, not about a movement, so it
  names no wallet and no token. `news_market_wallet_events_kind_check` admits only `exit` and
  `crowding`, and `news_market_wallet_events_identity_check` requires both `wallet` and `token` to be
  addresses -- so the row cannot be written at all until both say what a digest is. The empty string is
  the honest value for the two identity columns: the zero address would be a claim about an address,
  and NULL would make three columns nullable to express one kind's absence.
  `news_market_wallet_events_window_check` is replaced only to allow the equal-instant case it already
  allowed while stating the digest's own rule beside it; its expression is otherwise unchanged.
  The two indexes serve the two reads PR-3 adds. `ix_news_market_wallet_events_event_at` is the console
  page's window read (`GET /api/news/wallets/cards`) and the digest's own "which cards did this window
  send" scan: the table's two existing indexes lead with `wallet` and with `token`, so a pure time
  window on either is a scan of the whole retention. `ix_news_market_wallet_events_digest` is the
  partial index behind "when did the last digest end and how many model calls has the rolling day
  spent" -- six rows a day, and the query runs six times a day, but on the full-table index that
  question would read every card ever opened.
- current_source_revision: 20260906_0372
- minimum_supported_source_revision: 20260906_0372
- lock_level_and_order: one `ALTER TABLE` on `news_market_wallet_events` carrying three
  `DROP CONSTRAINT` + `ADD CONSTRAINT` pairs, taking `ACCESS EXCLUSIVE` on that table for one
  validating scan, then two `CREATE INDEX`es on the same table, each taking `SHARE` for its build. No
  other relation is touched.
- statement_timeout: 60s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: `news_market_wallet_events` holds one row per card. #572's 24-hour on-chain
  calibration projects 30-50 exit cards and 1-2 crowding cards a day, so the table holds low thousands
  of rows at the market Item retention tier; the digest adds six rows a day on top of that.
- estimated_bytes: the two indexes are tens of kilobytes at that row count; the digest rows themselves
  carry their fact pack in `evidence` at roughly 5 KB each, so about 30 KB a day.
- rewrite_or_index_build: no rewrite. Each CHECK replacement is one validating scan of a table with
  low thousands of rows, and both index builds are over the same table. The tape's own writer is
  blocked for the length of the `ALTER TABLE` and each build; it retries the same turn's range on the
  next pass, so nothing is lost.
- preflight_and_maintenance_boundary: none required. A writer running the previous code writes no
  `digest` row and is unaffected by constraints that admit one more kind, and the new indexes block
  that writer only for their builds.
- archive_current_compatibility: every existing `exit` and `crowding` row keeps every value it had and
  satisfies all three replacements unchanged.
- role_and_grant_impact: none; the single `tracefold` login is unchanged.
- failure_state: the transaction rolls back completely and the table keeps its current constraints and
  indexes.
- roll_forward_or_verified_backup_restore: `downgrade` is refused. Narrowing the kind vocabulary again
  would leave digest rows the CHECK rejects, and dropping them would delete summaries readers were
  sent. Roll forward with a new revision.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260906_0373
Revises: 20260906_0372
Create Date: 2026-09-06 09:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260906_0373"
down_revision = "20260906_0372"
branch_labels = None
depends_on = None

_ADDRESS_PATTERN = "^0x[0-9a-f]{40}$"
_TX_HASH_PATTERN = "^0x[0-9a-f]{64}$"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    _events_admit_a_digest()
    _digest_indexes()


def _events_admit_a_digest() -> None:
    """A third kind, and the two predicates that assumed every observation had a subject address.

    The three statements are one `ALTER TABLE` so the table is scanned once and locked once. A CHECK is
    a single expression, so each of the three is replaced rather than extended.

    The identity predicate is where the digest differs in substance. `exit` and `crowding` are about a
    wallet and a token and must carry both; a digest is about four hours of a roster and carries
    neither, and it says so with two empty strings rather than with a placeholder address or with two
    more nullable columns. The predicate now states the difference instead of admitting anything: a
    digest must have *both* empty, and every other kind must have both as real addresses.
    """

    op.execute(
        f"""
        ALTER TABLE public.news_market_wallet_events
          DROP CONSTRAINT IF EXISTS news_market_wallet_events_kind_check,
          DROP CONSTRAINT IF EXISTS news_market_wallet_events_identity_check,
          DROP CONSTRAINT IF EXISTS news_market_wallet_events_window_check,
          ADD CONSTRAINT news_market_wallet_events_kind_check
            CHECK (kind = ANY (ARRAY['exit'::text, 'crowding'::text, 'digest'::text])),
          ADD CONSTRAINT news_market_wallet_events_identity_check
            CHECK ((tx_hash IS NULL OR tx_hash ~ '{_TX_HASH_PATTERN}')
                   AND CASE WHEN kind = 'digest'::text
                            THEN wallet = ''::text AND token = ''::text
                            ELSE wallet ~ '{_ADDRESS_PATTERN}' AND token ~ '{_ADDRESS_PATTERN}'
                       END),
          ADD CONSTRAINT news_market_wallet_events_window_check
            CHECK (window_from_ms > 0 AND window_to_ms >= window_from_ms
                   AND (kind <> 'digest'::text OR window_to_ms > window_from_ms))
        """
    )


def _digest_indexes() -> None:
    """The console's window read, and the digest's own "when did the last one end".

    The table's two existing indexes lead with `wallet` and with `token`, which is what the card rules
    ask about. Both reads PR-3 adds ask a different question -- one about a span of time across every
    subject, one about the digest rows alone -- and neither of those is a prefix of either index.

    The partial index is small by construction: six rows a day against thousands of cards, so it is the
    difference between reading the digests and reading everything to find them.
    """

    op.execute(
        """
        CREATE INDEX ix_news_market_wallet_events_event_at
            ON public.news_market_wallet_events (event_at_ms DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_market_wallet_events_digest
            ON public.news_market_wallet_events (window_to_ms DESC)
         WHERE kind = 'digest'
        """
    )


def downgrade() -> None:
    """Refused. A narrower kind vocabulary would reject rows readers were already sent."""

    raise RuntimeError("news_market_wallet_digest_downgrade_unsupported")
