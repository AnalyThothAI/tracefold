"""Drop the notification delivery ledger and the index that fed it; neither ever ran (#528 PR-1).

Migration evidence:

- category: destructive hard-cut of one empty table and one partial index
- why_database_must_change: `trading_execution_notification_deliveries` is the watermark of a Trading
  notification channel that has never been assembled in production. It was reachable only through
  `trading.notifications.enabled`, and before #458 only through `trading.control.enabled`; neither
  block exists in the operator's `config.yaml`, and the table holds 0 rows. #528 deletes the worker,
  both senders, the policy that chose what to send, and the Telegram control ingress beside it, so
  the ledger has no writer, no reader and no channel left to resume. The partial
  `trading_execution_notification_candidates_idx` on `trading_execution_observations` existed only to
  make that ledger's anti-join cheap; keeping an index on the hot append-only observation stream to
  serve a query no code can issue is write amplification an operator pays for nothing.
- current_source_revision: 20260903_0358
- minimum_supported_source_revision: 20260903_0358
- lock_level_and_order: `DROP TABLE` first (it holds the only foreign key into
  `trading_execution_observations(seq)`, so the index drop cannot be blocked by it), then
  `DROP INDEX`. ACCESS EXCLUSIVE on the dropped table and, briefly, on the observation stream for the
  index unlink. No column of any surviving table changes.
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: 0 in `trading_execution_notification_deliveries`; the index covers the seven
  notifiable kinds of `trading_execution_observations`
- estimated_bytes: one empty heap plus its primary key, and one partial btree over the observation
  stream
- rewrite_or_index_build: none; both statements unlink files rather than rewriting them
- preflight_and_maintenance_boundary: **the absence of `CASCADE` is the preflight.** `DROP TABLE`
  without it refuses if anything outside the drop set still depends on the ledger, so a dependency
  this revision failed to notice aborts the transaction instead of taking a live object with it.
  Ordinary canonical migration stop otherwise; the Runtime keeps appending observations throughout.
- archive_current_compatibility: nothing is archived because nothing was ever written. The 0-row
  count is the whole record, and `trading_execution_observations` -- the durable facts the ledger
  pointed at -- is untouched.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the empty ledger and its index stay
- roll_forward_or_verified_backup_restore: roll forward. A revision that recreated the table would
  recreate an empty table; there is no row to restore.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260903_0359
Revises: 20260903_0358
Create Date: 2026-09-03 12:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260903_0359"
down_revision = "20260903_0358"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("DROP TABLE trading_execution_notification_deliveries")
    op.execute("DROP INDEX trading_execution_notification_candidates_idx")


def downgrade() -> None:
    raise RuntimeError(
        "20260903_0359 drops the never-written Trading notification ledger; roll forward rather than "
        "recreating an empty table"
    )
