"""Remove the retired Workers Trading watchdog alert ledger.

Migration evidence:

- category: drop one platform-owned alert bookkeeping table; no Trading or News fact is changed.
- why_database_must_change: the sole reader and writer of platform_watchdog_alerts is removed, so
  retaining its episode state would leave an unowned table and stale operator data.
- current_source_revision: 20260924_0396
- minimum_supported_source_revision: 20260924_0396
- lock_level_and_order: acquire ACCESS EXCLUSIVE on platform_watchdog_alerts, check for rows,
  then DROP TABLE while holding that lock.
- statement_timeout: 30s set locally by the revision.
- lock_timeout: 5s set locally by the revision.
- estimated_rows: at most one row per watchdog condition (six in the retired implementation).
- estimated_bytes: under a kilobyte of alert bookkeeping.
- rewrite_or_index_build: none.
- preflight_and_maintenance_boundary: the standard stopped-Workers migration gate; the retired
  Workers image must stop before the table is dropped. If alert rows remain, archive them and clear
  the table under that gate before upgrading. Nautilus never reads this table.
- archive_current_compatibility: no business facts are deleted. The guard refuses to discard even
  retired alert episode timestamps until the operator has archived and cleared them.
- role_and_grant_impact: none; the single tracefold login is unchanged.
- failure_state: a populated table raises watchdog_alerts_present before DDL; transactional DDL
  rolls back, leaving the old table intact on every other failure.
- roll_forward_or_verified_backup_restore: downgrade recreates the empty historical table if the old
  image is restored; prior alert episodes are not recoverable without a pre-cut backup.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260925_0397
Revises: 20260924_0396
"""

from alembic import op

revision = "20260925_0397"
down_revision = "20260924_0396"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("LOCK TABLE platform_watchdog_alerts IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM platform_watchdog_alerts) THEN
                RAISE EXCEPTION 'watchdog_alerts_present: archive and clear platform_watchdog_alerts before upgrading';
            END IF;
        END
        $$;
    """)
    op.execute("DROP TABLE platform_watchdog_alerts")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
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
