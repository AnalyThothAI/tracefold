"""Hard-cut Runtime readiness and add one O(1) current control projection.

Migration evidence:

- category: runtime projection and control-state hard cut
- why_database_must_change: one ``ready`` bit and a 999-row startup fold cannot
  represent liveness, existing-exposure safety, new-entry admission, or bounded
  current control recovery without competing owners
- current_source_revision: 20260901_0347
- minimum_supported_source_revision: 20260901_0347
- lock_level_and_order: short ALTER/CREATE DDL plus one stopped-writer bounded
  backfill over the append-only Command/Observation ledgers
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: one Runtime row per account slot, one control row per profile;
  retained Command/Observation history is bounded by existing retention
- estimated_bytes: one small current control row per activation plus Runtime
  projection columns; no new history or secondary index
- rewrite_or_index_build: PostgreSQL may rewrite the tiny Runtime projection;
  the new current table is populated once and has only its primary key
- preflight_and_maintenance_boundary: ordinary canonical migration stop with
  Serve, Workers, and Nautilus stopped
- role_and_grant_impact: none; the single tracefold login owns both projections
- failure_state: the transaction rolls back completely
- roll_forward_or_verified_backup_restore: correct with a forward revision or
  restore the verified pre-cut backup
- archive_current_compatibility: Signal, Command, and Observation facts remain
  append-only; only their current control projection is added
- evidence_postgresql_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260902_0348
Revises: 20260901_0347
Create Date: 2026-09-02 00:20:00
"""

from __future__ import annotations

from alembic import op

revision = "20260902_0348"
down_revision = "20260901_0347"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(
        """
        CREATE TABLE trading_execution_runtime_control_state (
          runtime_profile_id text PRIMARY KEY
            REFERENCES trading_execution_profile_activations(runtime_profile_id) ON DELETE RESTRICT,
          entries_paused boolean NOT NULL,
          emergency_halted boolean NOT NULL,
          last_command_seq bigint NOT NULL,
          last_command_id text,
          updated_at_ns bigint NOT NULL,
          CONSTRAINT trading_execution_runtime_control_profile_check
            CHECK (runtime_profile_id ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'),
          CONSTRAINT trading_execution_runtime_control_seq_check CHECK (last_command_seq >= 0),
          CONSTRAINT trading_execution_runtime_control_command_check
            CHECK (last_command_id IS NULL OR last_command_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_execution_runtime_control_clock_check CHECK (updated_at_ns > 0),
          CONSTRAINT trading_execution_runtime_control_halt_check
            CHECK (NOT emergency_halted OR entries_paused)
        )
        """
    )
    op.execute(
        """
        WITH applied AS (
          SELECT activation.runtime_profile_id,
                 command.seq AS command_seq,
                 command.command_id,
                 command.action,
                 observation.observed_at_ns,
                 row_number() OVER (
                   PARTITION BY activation.runtime_profile_id
                   ORDER BY command.seq DESC, observation.seq DESC
                 ) AS newest,
                 bool_or(command.action = 'emergency_halt') OVER (
                   PARTITION BY activation.runtime_profile_id
                 ) AS emergency_halted
            FROM trading_execution_profile_activations activation
            JOIN trading_operator_intents command
              ON command.target_profile_id = activation.runtime_profile_id
             AND command.seq > activation.activated_after_command_seq
            JOIN trading_execution_observations observation
              ON observation.runtime_profile_id = activation.runtime_profile_id
             AND observation.command_id = command.command_id
           WHERE command.action IN ('pause_entries', 'resume_entries', 'emergency_halt', 'flatten')
             AND (
               (
                 observation.normalized_kind = 'control_disposition'
                 AND observation.summary ->> 'disposition' IN ('accepted', 'completed')
               ) OR (
                 command.action = 'flatten'
                 AND observation.normalized_kind = 'readiness'
                 AND observation.summary ->> 'control_stage' = 'runtime_accepted'
               )
             )
        )
        INSERT INTO trading_execution_runtime_control_state (
          runtime_profile_id, entries_paused, emergency_halted,
          last_command_seq, last_command_id, updated_at_ns
        )
        SELECT activation.runtime_profile_id,
               COALESCE(newest.emergency_halted OR newest.action <> 'resume_entries', TRUE),
               COALESCE(newest.emergency_halted, FALSE),
               COALESCE(newest.command_seq, activation.activated_after_command_seq),
               newest.command_id,
               GREATEST(COALESCE(newest.observed_at_ns, activation.created_at_ns), activation.created_at_ns)
          FROM trading_execution_profile_activations activation
          LEFT JOIN applied newest
            ON newest.runtime_profile_id = activation.runtime_profile_id
           AND newest.newest = 1
        """
    )

    op.execute("ALTER TABLE trading_execution_runtime_state DROP CONSTRAINT trading_execution_runtime_ready_check")
    op.execute("ALTER TABLE trading_execution_runtime_state RENAME COLUMN ready TO alive")
    op.execute("ALTER TABLE trading_execution_runtime_state RENAME COLUMN unavailable_reason TO entry_block_reason")
    op.execute(
        """
        ALTER TABLE trading_execution_runtime_state
          ADD COLUMN execution_safe boolean NOT NULL DEFAULT FALSE,
          ADD COLUMN entries_armed boolean NOT NULL DEFAULT FALSE,
          ADD COLUMN control_plane_ready boolean NOT NULL DEFAULT FALSE,
          ADD COLUMN day_start_ready boolean NOT NULL DEFAULT FALSE,
          ADD COLUMN positions_count integer NOT NULL DEFAULT 0,
          ADD COLUMN open_orders_count integer NOT NULL DEFAULT 0,
          ADD COLUMN protection_status text NOT NULL DEFAULT 'unknown'
        """
    )
    op.execute(
        """
        UPDATE trading_execution_runtime_state
           SET alive = FALSE,
               entry_block_reason = 'migration_restart_required',
               updated_at_ns = GREATEST(updated_at_ns, heartbeat_at_ns)
        """
    )
    op.execute(
        """
        ALTER TABLE trading_execution_runtime_state
          ADD CONSTRAINT trading_execution_runtime_counts_check
            CHECK (positions_count >= 0 AND open_orders_count >= 0),
          ADD CONSTRAINT trading_execution_runtime_protection_check
            CHECK (protection_status IN ('not_applicable', 'protected', 'pending', 'unprotected', 'unknown')),
          ADD CONSTRAINT trading_execution_runtime_alive_check
            CHECK (NOT alive OR lifecycle_state IN ('starting', 'running', 'stopping')),
          ADD CONSTRAINT trading_execution_runtime_safe_check
            CHECK (
              NOT execution_safe OR (
                alive
                AND singleton_ready
                AND credential_ready
                AND activation_ready
                AND startup_reconciled
                AND portfolio_ready
                AND NOT unexpected_exposure
              )
            ),
          ADD CONSTRAINT trading_execution_runtime_armed_check
            CHECK (
              NOT entries_armed OR (
                execution_safe
                AND control_plane_ready
                AND audit_ready
                AND day_start_ready
              )
            ),
          ADD CONSTRAINT trading_execution_runtime_entry_reason_check
            CHECK (
              (entries_armed AND entry_block_reason IS NULL)
              OR (NOT entries_armed AND entry_block_reason IS NOT NULL)
            )
        """
    )
    op.execute("ALTER TABLE trading_execution_runtime_state ALTER COLUMN execution_safe DROP DEFAULT")
    op.execute("ALTER TABLE trading_execution_runtime_state ALTER COLUMN entries_armed DROP DEFAULT")
    op.execute("ALTER TABLE trading_execution_runtime_state ALTER COLUMN control_plane_ready DROP DEFAULT")
    op.execute("ALTER TABLE trading_execution_runtime_state ALTER COLUMN day_start_ready DROP DEFAULT")
    op.execute("ALTER TABLE trading_execution_runtime_state ALTER COLUMN positions_count DROP DEFAULT")
    op.execute("ALTER TABLE trading_execution_runtime_state ALTER COLUMN open_orders_count DROP DEFAULT")
    op.execute("ALTER TABLE trading_execution_runtime_state ALTER COLUMN protection_status DROP DEFAULT")


def downgrade() -> None:
    raise RuntimeError("trading_runtime_control_state_forward_only")
