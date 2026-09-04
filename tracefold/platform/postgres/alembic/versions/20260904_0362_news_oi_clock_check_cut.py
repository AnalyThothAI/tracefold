"""Delete the two cross-clock CHECKs: a venue stamp and a host stamp are two recorded facts (#544).

Migration evidence:

- category: constraint deletion; no column, row or index changes
- why_database_must_change: two CHECKs order a *provider's* clock against *this host's*, which is not
  an invariant of the data — it is an assumption that a venue clock never runs ahead of ours. It
  does, by a few hundred milliseconds.

  `news_oi_signals_available_clock_check` asserts
  `available_at_ms >= observed_at_ms AND available_at_ms >= created_at_ms`. `observed_at_ms` is the
  provider's own publication time, carried unmodified from `news_items.published_at_ms` through
  `news_events.opened_at_ms`, and it arrives at second granularity; `available_at_ms` and
  `created_at_ms` are both this process's `now_ms()` at settle time. No reader compares the columns:
  every point-in-time query bounds each one separately (`available_at_ms <= cutoff`,
  `created_at_ms <= cutoff`, `observed_at_ms` in a window), which is what "could this lane have read
  it" actually means. PostgreSQL answered the question nobody asked by refusing the row — which
  `_store_frame` does not classify, so the whole News Workers process died on it. Production
  restarted seven times in six hours on 2026-09-04 for exactly this.

  `news_market_liquidations_time_order` asserts `received_at_ms >= event_at_ms` over the same pair of
  clocks: the venue's own stamp for a forced trade, and when this host read it. It never fired,
  because `parse_liquidation` refused such a frame first and returned `None` — a guard that existed
  only to keep this CHECK quiet, and whose price was discarding a liquidation that had really
  happened. That guard goes with the constraint in this change; leaving the CHECK would put the
  refusal straight back, one layer down, as the same fatal `CheckViolation`.

  Deleting the rules is the fix. Clamping a column would overwrite one of the facts the columns exist
  to record.
- current_source_revision: 20260904_0361
- minimum_supported_source_revision: 20260904_0361
- lock_level_and_order: one `ACCESS EXCLUSIVE` catalog update on `news_oi_signals` and one on
  `news_market_liquidations`, each held for the duration of a catalog row delete, in that order; no
  other table is touched
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: 0 rows read or written. `DROP CONSTRAINT` on a CHECK is a `pg_constraint` delete;
  the 364 rows the production OI ledger holds are not revalidated, rewritten or scanned, and
  `news_market_liquidations` holds none at all
- estimated_bytes: two catalog tuples
- rewrite_or_index_build: neither. No heap rewrite, no index build, no `NOT VALID` phase
- preflight_and_maintenance_boundary: none required. Dropping a CHECK only widens what the table
  accepts, so a writer running against the old schema stays correct against the new one; this is the
  one direction that needs no stopped writer. `make up` stops Workers anyway
- archive_current_compatibility: fully compatible. Every stored row still satisfies both deleted
  predicates — it had to, to be stored — and no row is changed. Rows written after this revision may
  carry `observed_at_ms > available_at_ms`, or `event_at_ms > received_at_ms`, which reads as what it
  is: the venue stamped the fact a moment ahead of this host's clock
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and both ledgers keep their constraint
- roll_forward_or_verified_backup_restore: correct with a new forward revision. `downgrade` restores
  both CHECKs, which is honest here — the rules were wrong, not destructive, and no data was lost —
  but a database that has since accepted an ahead-of-host frame will refuse to re-add them, which is
  the correct refusal and the reason to roll forward instead
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260904_0362
Revises: 20260904_0361
Create Date: 2026-09-04 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260904_0362"
down_revision = "20260904_0361"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    op.execute("ALTER TABLE public.news_oi_signals DROP CONSTRAINT news_oi_signals_available_clock_check")
    op.execute("ALTER TABLE public.news_market_liquidations DROP CONSTRAINT news_market_liquidations_time_order")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    # Reversible on purpose: this revision deletes rules, not data. Each re-added CHECK is validated
    # against every stored row, so a database that has accepted a fact whose venue clock ran ahead
    # refuses the downgrade rather than deleting that fact — which is the answer this revision exists
    # to make possible.
    op.execute(
        """
        ALTER TABLE public.news_oi_signals
          ADD CONSTRAINT news_oi_signals_available_clock_check
            CHECK (available_at_ms >= observed_at_ms AND available_at_ms >= created_at_ms)
        """
    )
    op.execute(
        """
        ALTER TABLE public.news_market_liquidations
          ADD CONSTRAINT news_market_liquidations_time_order CHECK (received_at_ms >= event_at_ms)
        """
    )
