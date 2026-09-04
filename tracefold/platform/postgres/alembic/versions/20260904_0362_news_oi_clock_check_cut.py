"""Delete the OI frame ledger's clock-ordering CHECK: three clocks are three recorded facts (#544).

Migration evidence:

- category: constraint deletion; no column, row or index changes
- why_database_must_change: `news_oi_signals_available_clock_check` asserts
  `available_at_ms >= observed_at_ms AND available_at_ms >= created_at_ms`, which is not an invariant
  of the data — it is an assumption that the provider's exchange clock never runs ahead of this
  host's. It does. `observed_at_ms` is the provider's own publication time, carried unmodified from
  `news_items.published_at_ms` through `news_events.opened_at_ms`, and it arrives at second
  granularity; `available_at_ms` and `created_at_ms` are both this process's `now_ms()` at settle
  time. A frame whose vendor stamp is a few hundred milliseconds ahead is a perfectly good
  measurement, and no reader compares the two columns: every point-in-time query bounds each column
  separately (`available_at_ms <= cutoff`, `created_at_ms <= cutoff`, `observed_at_ms` in a window),
  which is what "could this lane have read it" actually means. The constraint only ever answered a
  question nobody asked, and PostgreSQL answered it by refusing the row — which `_store_frame` does
  not classify, so the whole News Workers process died on it. Production restarted seven times in six
  hours on 2026-09-04 for exactly this. Deleting the rule is the fix; clamping the column would
  overwrite one of the three facts the columns exist to record.
- current_source_revision: 20260904_0361
- minimum_supported_source_revision: 20260904_0361
- lock_level_and_order: one `ACCESS EXCLUSIVE` catalog update on `news_oi_signals`, held for the
  duration of a catalog row delete; no other table is touched
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: 0 rows read or written. `DROP CONSTRAINT` on a CHECK is a `pg_constraint` delete;
  the 364 rows the production ledger holds are not revalidated, rewritten or scanned
- estimated_bytes: one catalog tuple
- rewrite_or_index_build: neither. No heap rewrite, no index build, no `NOT VALID` phase
- preflight_and_maintenance_boundary: none required. Dropping a CHECK only widens what the table
  accepts, so a writer running against the old schema stays correct against the new one; this is the
  one direction that needs no stopped writer. `make up` stops Workers anyway
- archive_current_compatibility: fully compatible. Every stored row still satisfies the deleted
  predicate — it had to, to be stored — and no row is changed. Rows written after this revision may
  carry `observed_at_ms > available_at_ms`, which reads as what it is: the provider stamped the frame
  a moment ahead of this host's clock
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and the ledger keeps the constraint
- roll_forward_or_verified_backup_restore: correct with a new forward revision. `downgrade` restores
  the CHECK, which is honest here — the rule was wrong, not destructive, and no data was lost — but a
  database that has since accepted an ahead-of-host frame will refuse to re-add it, which is the
  correct refusal and the reason to roll forward instead
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


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    # Reversible on purpose: this revision deletes a rule, not data. The re-added CHECK is validated
    # against every stored row, so a database that has accepted a frame whose provider clock ran ahead
    # refuses the downgrade rather than deleting the frame — which is the answer this revision exists
    # to make possible.
    op.execute(
        """
        ALTER TABLE public.news_oi_signals
          ADD CONSTRAINT news_oi_signals_available_clock_check
            CHECK (available_at_ms >= observed_at_ms AND available_at_ms >= created_at_ms)
        """
    )
