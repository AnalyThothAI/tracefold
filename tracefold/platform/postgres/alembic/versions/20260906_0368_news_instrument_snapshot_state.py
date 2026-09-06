"""The instrument catalogue records refresh success per venue, not per row (#570 A11).

Migration evidence:

- category: one new table, one column rename with its dependent NOT NULL constraint, and one
  single-statement backfill from the column being renamed into the new table
- why_database_must_change: `news_market_instruments.last_seen_ms` answered two questions with one
  number. "When did a refresh last see this contract" is what the status page's `last_snapshot_ms`
  reads, and it is a fact about a *venue answering a complete catalogue*, not about each of its 16 493
  rows -- so recording it per row made every six-hourly refresh rewrite the whole table. The audit at
  7b9628ca measured the cost on the live database: 3 790 237 cumulative `n_tup_upd`, one UPSERT
  statement with 3 806 706 calls, and 1 821 215 526 bytes of WAL, for a value no reader consults per
  row. `news_market_instrument_snapshot_state` holds that fact once per venue, which is where it can
  be written on a refresh that changes nothing.

  The other question -- "when was this contract's identity observed" -- is what stays on the row, and
  `observed_at_ms` is its honest name for what the writer records from here on: every row this
  revision or a later refresh writes carries the `observed_at_ms` of the listing event written beside
  it, the same fact `news_market_instrument_listing_events` already carries and the same name it
  carries it under. It is deliberately not backfilled. The roughly 16 493 rows that exist when this
  revision runs keep the stamp their last full refresh left, which is a real observation of that
  contract and is never later than its identity's, and each one is replaced by a true observation
  time the next time that contract actually changes. Rewriting all of them to make the two exactly
  equal today would cost one more whole-table rewrite -- the precise thing this revision exists to
  stop -- to correct a stamp no reader compares across the two tables. Keeping the old name would
  have left a column called `last_seen_ms` that no longer moves when a venue sees a contract -- the
  one reading a future operator would certainly take from it.
- current_source_revision: 20260905_0367
- minimum_supported_source_revision: 20260905_0367
- lock_level_and_order: `ACCESS EXCLUSIVE` on `news_market_instruments` for the catalog-only column and
  constraint renames, then one sequential scan of it for the backfill; the `CREATE TABLE` takes no lock
  on any existing object. No other table is read or written
- statement_timeout: 120s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: production holds about 16 493 `news_market_instruments` rows across six venues at the
  audit SHA. The backfill reads them once and inserts one row per distinct venue -- six rows
- estimated_bytes: one table of two columns holding one row per venue, and no index beyond its primary
  key. Kilobytes. The rename adds nothing
- rewrite_or_index_build: none. `RENAME COLUMN` and `RENAME CONSTRAINT` are catalog-only and do not
  rewrite the heap; the new table is created empty and its primary-key index is built on six rows
- preflight_and_maintenance_boundary: both writers and readers must be stopped. The snapshot writer on
  the previous revision names `last_seen_ms` in its UPSERT and would fail against this schema, and a
  writer on this revision names `observed_at_ms` and `news_market_instrument_snapshot_state`, which the
  previous schema does not have. The previous-revision *reader* breaks the same way and is the more
  visible half: Serve's `universe_summary()` selects `max(last_seen_ms)`, so a Serve process left
  running across this revision would fail the whole `/api/news/status` route rather than one figure on
  it. `make up` stops Serve as well as Workers before migrating, which is why that boundary holds
- archive_current_compatibility: every existing row keeps every value it had; the rename changes the
  column's name and its meaning going forward, never a stored number. The backfill seeds each venue's
  snapshot state with `max(last_seen_ms)` for that venue, which under the previous writer is exactly
  the last time that venue answered a complete catalogue -- so the status page's `last_snapshot_ms`
  reports the same instant across the cutover instead of reading empty until the next six-hourly
  snapshot. Delisted rows are included in that maximum for the same reason they were included before:
  a delisting is written by a refresh that answered
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and every table keeps its current shape
- roll_forward_or_verified_backup_restore: `downgrade` is refused. Renaming the column back would
  restore a name whose meaning this revision deliberately narrows: after it, the row stamp moves only
  when a contract's identity moves, so a previous-revision reader computing `max(last_seen_ms)` would
  report the last catalogue *change* as the last catalogue *refresh* and quietly understate freshness
  by up to a full retention of unchanged snapshots. Dropping the state table would also lose the only
  record of which venue last answered. Roll forward with a new revision
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260906_0368
Revises: 20260905_0367
Create Date: 2026-09-05 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260906_0368"
down_revision = "20260905_0367"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    # One row per venue, and the venue is the key: a venue that fails to answer keeps the last time it
    # did, which is exactly the "a failed venue is omitted from reconciliation" rule the snapshot has
    # always applied to its rows, now applied to its freshness too.
    op.execute(
        """
        CREATE TABLE public.news_market_instrument_snapshot_state (
            venue text NOT NULL,
            last_snapshot_ms bigint NOT NULL,
            CONSTRAINT news_market_instrument_snapshot_state_pkey PRIMARY KEY (venue),
            CONSTRAINT news_market_instrument_snapshot_state_venue_nonempty CHECK (venue <> ''::text)
        )
        """
    )
    # Seeded before the rename, from the column that carried the fact until now.
    op.execute(
        """
        INSERT INTO public.news_market_instrument_snapshot_state (venue, last_snapshot_ms)
        SELECT venue, max(last_seen_ms) FROM public.news_market_instruments GROUP BY venue
        """
    )
    op.execute("ALTER TABLE public.news_market_instruments RENAME COLUMN last_seen_ms TO observed_at_ms")
    # `RENAME COLUMN` does not rename the constraints that depend on the column, and PostgreSQL 18
    # catalogues NOT NULL as a named constraint -- so without this the schema keeps a constraint called
    # `news_market_instruments_last_seen_ms_not_null` on a column that no longer exists under that name,
    # and the next operator reading `\d news_market_instruments` meets the old name anyway. Guarded
    # because `RENAME CONSTRAINT` has no `IF EXISTS`: a database created after PostgreSQL stops naming
    # NOT NULLs, or one whose constraint was already renamed, must not fail here.
    op.execute(
        """
        DO $block$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conrelid = 'public.news_market_instruments'::regclass
               AND conname = 'news_market_instruments_last_seen_ms_not_null'
          ) THEN
            ALTER TABLE public.news_market_instruments
              RENAME CONSTRAINT news_market_instruments_last_seen_ms_not_null
                              TO news_market_instruments_observed_at_ms_not_null;
          END IF;
        END
        $block$
        """
    )


def downgrade() -> None:
    """Refused. The old name would come back meaning something narrower than it used to."""

    raise RuntimeError("news_instrument_snapshot_state_downgrade_unsupported")
