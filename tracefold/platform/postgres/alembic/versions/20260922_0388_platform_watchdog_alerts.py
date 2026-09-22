"""The Workers watchdog's alert ledger: one row per watched condition (#680 PR-2).

Migration evidence:

- category: one new table with its primary key; nothing existing is read, rewritten or locked.
- why_database_must_change: the watchdog alerts on onset, re-alerts at most every few hours while a
  condition persists, and says so when it clears. "Did I already tell the operator about this" has to
  survive a Workers restart, and Workers restarted 10-21 times a day in the #680 audit window; state
  held in memory would re-page every active condition on every restart and could never send the
  recovery message for a condition that cleared while the process was down. No existing ledger fits:
  the delivery ledgers are News-owned and keyed by reader cards, and the execution tables belong to the
  Runtime, which this watchdog only reads. The table is platform-owned, like `workers_runtime`, and
  written only by the Workers singleton.
- current_source_revision: 20260922_0387
- minimum_supported_source_revision: 20260922_0387
- lock_level_and_order: CREATE TABLE takes no lock on any existing relation.
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: at most one row per watched condition -- six today -- updated a few times an hour
  while a condition is active and not at all while none is.
- estimated_bytes: under a kilobyte.
- rewrite_or_index_build: none beyond the new table's own primary key.
- preflight_and_maintenance_boundary: the canonical migration gate (Workers stopped). The table is
  additive: an image older than this revision never names it, and this image's watchdog is the only
  reader and writer.
- archive_current_compatibility: nothing existing changes, so nothing is archived.
- role_and_grant_impact: none; the single tracefold login is unchanged.
- failure_state: the transaction rolls back and no table exists.
- roll_forward_or_verified_backup_restore: downgrade drops the table, which loses only which
  conditions were last alerted; the next watchdog pass re-alerts any condition still active.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260922_0388
Revises: 20260922_0387
"""

from alembic import op

revision = "20260922_0388"
down_revision = "20260922_0387"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    # Single-writer bookkeeping, so the shape stays the application's: `active` with `opened_at_ms` is
    # the current episode, `notified_at_ms` is the last message the operator actually received about it
    # (NULL until one got through), and `clear_since_ms` is when an active condition first read clear.
    op.execute("""
        CREATE TABLE platform_watchdog_alerts (
            condition_key text PRIMARY KEY,
            active boolean NOT NULL,
            opened_at_ms bigint NOT NULL,
            notified_at_ms bigint,
            clear_since_ms bigint,
            detail text NOT NULL,
            updated_at_ms bigint NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("DROP TABLE platform_watchdog_alerts")
