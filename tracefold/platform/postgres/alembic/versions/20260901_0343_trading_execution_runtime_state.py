"""Add the single current projection for the Binance execution Runtime.

Migration evidence:

- category: additive runtime projection
- why_database_must_change: canonical deploy/status and cold+flat transitions need
  one durable current owner rather than container or probe inference
- current_source_revision: 20260901_0342
- minimum_supported_source_revision: 20260901_0342
- lock_level_and_order: short CREATE TABLE/INDEX/FOREIGN KEY DDL after migration
  has stopped every steady application process
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: activation rows are currently single digits; the new table is empty
- estimated_bytes: one small current row per account slot plus one activation index
- rewrite_or_index_build: no heap rewrite; the activation index is over a bounded ledger
- preflight_and_maintenance_boundary: ordinary canonical migration stop
- role_and_grant_impact: none; the single tracefold login owns this projection
- failure_state: the transaction rolls back completely
- roll_forward_or_verified_backup_restore: correct with a forward revision or restore
  the verified pre-cut backup
- archive_current_compatibility: additive indexes cover only current append-only
  observations; archived facts and readers keep their existing shape
- evidence_postgresql_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260901_0343
Revises: 20260901_0342
Create Date: 2026-09-01 09:45:00
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0343"
down_revision = "20260901_0342"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(
        """
        CREATE INDEX ix_trading_execution_activations_slot_created
          ON trading_execution_profile_activations
          (account_slot, created_at_ns DESC, runtime_profile_id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_trading_execution_observations_signal_recovery
          ON trading_execution_observations (runtime_profile_id, signal_id, seq DESC)
          WHERE signal_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_trading_execution_observations_command_recovery
          ON trading_execution_observations (runtime_profile_id, command_id, seq DESC)
          WHERE command_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE TABLE trading_execution_runtime_state (
          account_slot text PRIMARY KEY,
          runtime_profile_id text NOT NULL REFERENCES trading_execution_profile_activations(runtime_profile_id)
            ON DELETE RESTRICT,
          mode text NOT NULL,
          runtime_release text NOT NULL,
          config_sha256 text NOT NULL,
          runtime_id uuid NOT NULL UNIQUE,
          runtime_revision text NOT NULL,
          image_digest text NOT NULL,
          credential_fingerprint text NOT NULL,
          lifecycle_state text NOT NULL,
          ready boolean NOT NULL,
          singleton_ready boolean NOT NULL,
          credential_ready boolean NOT NULL,
          activation_ready boolean NOT NULL,
          startup_reconciled boolean NOT NULL,
          portfolio_ready boolean NOT NULL,
          audit_ready boolean NOT NULL,
          unexpected_exposure boolean NOT NULL,
          account_flat boolean NOT NULL,
          reconciliation_observed_at_ns bigint NOT NULL,
          heartbeat_at_ns bigint NOT NULL,
          unavailable_reason text,
          started_at_ns bigint NOT NULL,
          updated_at_ns bigint NOT NULL,
          CONSTRAINT trading_execution_runtime_slot_check
            CHECK (account_slot ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'),
          CONSTRAINT trading_execution_runtime_profile_check
            CHECK (runtime_profile_id ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'),
          CONSTRAINT trading_execution_runtime_mode_check
            CHECK (mode IN ('paper', 'live')),
          CONSTRAINT trading_execution_runtime_release_check
            CHECK (char_length(runtime_release) BETWEEN 1 AND 128),
          CONSTRAINT trading_execution_runtime_config_check
            CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_execution_runtime_revision_check
            CHECK (char_length(runtime_revision) BETWEEN 1 AND 128),
          CONSTRAINT trading_execution_runtime_image_check
            CHECK (image_digest = 'unversioned' OR image_digest ~ '^sha256:[0-9a-f]{64}$'),
          CONSTRAINT trading_execution_runtime_credential_check
            CHECK (credential_fingerprint ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_execution_runtime_lifecycle_check
            CHECK (lifecycle_state IN ('starting', 'running', 'stopping', 'stopped', 'failed')),
          CONSTRAINT trading_execution_runtime_clock_check
            CHECK (
              reconciliation_observed_at_ns >= 0
              AND heartbeat_at_ns > 0
              AND started_at_ns > 0
              AND updated_at_ns >= started_at_ns
              AND heartbeat_at_ns <= updated_at_ns
            ),
          CONSTRAINT trading_execution_runtime_exposure_check
            CHECK (NOT (account_flat AND unexpected_exposure)),
          CONSTRAINT trading_execution_runtime_ready_check
            CHECK (
              NOT ready OR (
                lifecycle_state = 'running'
                AND singleton_ready
                AND credential_ready
                AND activation_ready
                AND startup_reconciled
                AND portfolio_ready
                AND audit_ready
                AND NOT unexpected_exposure
                AND unavailable_reason IS NULL
              )
            ),
          CONSTRAINT trading_execution_runtime_reason_check
            CHECK (
              unavailable_reason IS NULL OR
              unavailable_reason ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'
            )
        )
        """
    )


def downgrade() -> None:
    raise RuntimeError("irreversible Trading execution Runtime projection; use verified backup restore")
