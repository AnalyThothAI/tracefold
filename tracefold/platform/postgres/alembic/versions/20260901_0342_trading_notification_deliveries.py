"""Add the append-only best-effort Trading notification delivery ledger.

Migration evidence:

- category: additive append-only delivery ledger
- why_database_must_change: resume asynchronous Telegram observation delivery after Workers restart
- current_source_revision: 20260901_0341
- minimum_supported_source_revision: 20260901_0341
- lock_level_and_order: CREATE TABLE, then one partial btree on the dormant observation stream
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: one ledger row per delivered operator-relevant observation and target; observation stream is dormant
- estimated_bytes: bounded receipt metadata plus one target SHA-256 identity per row
- rewrite_or_index_build: new ledger primary key plus a partial candidate-sequence btree; no heap rewrite
- preflight_and_maintenance_boundary: execution stays disabled during D, so the indexed observation
  stream has no active writer
- archive_current_compatibility: 0341 code ignores the additive table
- role_and_grant_impact: none; the existing application owner creates and owns it
- failure_state: transactional DDL rolls back completely
- roll_forward_or_verified_backup_restore: correct with a forward revision or verified backup restore

Revision ID: 20260901_0342
Revises: 20260901_0341
Create Date: 2026-09-01 12:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0342"
down_revision = "20260901_0341"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(
        """
        CREATE TABLE trading_execution_notification_deliveries (
          target_sha256 text NOT NULL
            CHECK (target_sha256 ~ '^[0-9a-f]{64}$'),
          observation_seq bigint NOT NULL
            REFERENCES trading_execution_observations(seq) ON DELETE RESTRICT,
          message_id bigint NOT NULL
            CHECK (message_id > 0),
          delivered_at_ns bigint NOT NULL
            CHECK (delivered_at_ns > 0),
          PRIMARY KEY (target_sha256, observation_seq)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX trading_execution_notification_candidates_idx
          ON trading_execution_observations (seq)
         WHERE normalized_kind IN (
           'signal_disposition', 'control_disposition', 'fill', 'audit_gap',
           'readiness', 'order', 'reconciliation'
         )
        """
    )


def downgrade() -> None:
    raise RuntimeError("irreversible Trading notification delivery ledger; roll forward or restore a verified backup")
