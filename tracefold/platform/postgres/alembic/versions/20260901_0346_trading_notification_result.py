"""Let a notification receipt outlive its provider's message id and carry a 4 h result.

Migration evidence:

- category: additive column plus one relaxed constraint
- why_database_must_change: the delivery receipt was written for Telegram, whose `sendMessage`
  returns a message id. #458 PR-B adds Feishu, whose custom-bot webhook
  (`POST /open-apis/bot/v2/hook/<id>`) returns no id at all and cannot edit a sent message. The
  receipt's job is "this observation was notified on this target at this time"; the provider message
  id is a Telegram affordance, so it becomes nullable rather than being faked. `result_delivered_at_ns`
  records the second message that carries the Signal's 1 h/4 h outcome -- a second message rather than
  an edit, because the deployed channel cannot edit.
- current_source_revision: 20260901_0345
- minimum_supported_source_revision: 20260901_0345
- lock_level_and_order: brief ACCESS EXCLUSIVE on one table for the column add and the constraint
  swap; no data is read or rewritten
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: `trading_execution_notification_deliveries` is empty in production -- the notifier
  has never been assembled, because it was reachable only through `trading.control.enabled`
- estimated_bytes: one nullable bigint column; PostgreSQL 18 adds it without a rewrite
- rewrite_or_index_build: no heap rewrite, no index build
- preflight_and_maintenance_boundary: none beyond the ordinary migration stop. Relaxing a NOT NULL
  and widening a CHECK cannot invalidate an existing row.
- archive_current_compatibility: fully compatible. Every existing receipt keeps its message id and
  reads back unchanged; only new rows may omit one.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the receipt keeps its Telegram-shaped
  contract
- roll_forward_or_verified_backup_restore: correct with a new forward revision; nothing is destroyed
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260901_0346
Revises: 20260901_0345
Create Date: 2026-09-01 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0346"
down_revision = "20260901_0345"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    # A webhook channel has no message id. `NULL` is the honest record of that; a sentinel like 0 would
    # be a number a reader could try to open.
    op.execute("ALTER TABLE trading_execution_notification_deliveries ALTER COLUMN message_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE trading_execution_notification_deliveries "
        "DROP CONSTRAINT trading_execution_notification_deliveries_message_id_check"
    )
    op.execute(
        "ALTER TABLE trading_execution_notification_deliveries "
        "ADD CONSTRAINT trading_execution_notification_deliveries_message_id_check "
        "CHECK (message_id IS NULL OR message_id > 0)"
    )

    # The 4 h outcome message. Nullable because it is written hours after the receipt it belongs to,
    # and its absence on a young receipt is the ordinary state rather than a fault.
    op.execute("ALTER TABLE trading_execution_notification_deliveries ADD COLUMN result_delivered_at_ns bigint")
    op.execute(
        "ALTER TABLE trading_execution_notification_deliveries "
        "ADD CONSTRAINT trading_execution_notification_deliveries_result_clock_check "
        "CHECK (result_delivered_at_ns IS NULL OR result_delivered_at_ns > delivered_at_ns)"
    )


def downgrade() -> None:
    raise RuntimeError("trading_notification_result_forward_only")
