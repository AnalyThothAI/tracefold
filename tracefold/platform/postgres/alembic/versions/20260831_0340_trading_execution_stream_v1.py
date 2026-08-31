"""Add the dormant engine-neutral Trading execution stream (#433-A).

Migration evidence:

- category: additive schema, indexes, append-only trigger, and runtime privileges
- why_database_must_change: Signal, OperatorIntent, Observation, and immutable
  activation waterlines must cross the Tracefold/Nautilus process boundary as
  durable PostgreSQL facts without reusing the retiring Intent/OMS tables
- current_source_revision: 20260831_0339
- minimum_supported_source_revision: 20260831_0339
- lock_level_and_order: new catalog objects only; no existing business table lock
- statement_timeout: 10s
- lock_timeout: 1s
- estimated_rows: 0 in every new table
- estimated_bytes: 0 before future writers are connected
- rewrite_or_index_build: no existing table rewrite; empty-table index builds
- preflight_and_maintenance_boundary: ordinary stopped-writer migration gate;
  the new transport remains dormant after upgrade
- archive_current_compatibility: old Capital/Intent rows and paths are untouched
- role_and_grant_impact: Serve reads only; Workers may append Signal, Command,
  and activation facts; Nautilus may read those facts and append Observations
- failure_state: transactional DDL rolls back; no producer or consumer exists
- roll_forward_or_verified_backup_restore: roll forward; downgrade is the
  verified pre-migration backup restore
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260831_0340
Revises: 20260831_0339
"""

from __future__ import annotations

from alembic import op

revision = "20260831_0340"
down_revision = "20260831_0339"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '1s'")
    op.execute("SET LOCAL statement_timeout = '10s'")

    op.execute(
        r"""
        CREATE FUNCTION trading_execution_metadata_valid(value JSONB) RETURNS BOOLEAN
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT jsonb_typeof(value) = 'object'
             AND trading_jsonb_object_size(value) <= 16
             AND octet_length(value::text) <= 2048
             AND NOT EXISTS (
               SELECT 1
                 FROM jsonb_each(value) item
                WHERE item.key !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'
                   OR CASE jsonb_typeof(item.value)
                        WHEN 'string' THEN char_length(item.value #>> '{}') > 256
                        WHEN 'number' THEN
                          (item.value #>> '{}') !~ '^-?[0-9]+$'
                          OR (item.value #>> '{}')::numeric < -9223372036854775808
                          OR (item.value #>> '{}')::numeric > 9223372036854775807
                        WHEN 'boolean' THEN false
                        ELSE true
                      END
             )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION trading_execution_string_array_valid(value JSONB) RETURNS BOOLEAN
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= 16
             AND octet_length(value::text) <= 4096
             AND NOT EXISTS (
               SELECT 1 FROM jsonb_array_elements(value) item
                WHERE jsonb_typeof(item) <> 'string' OR char_length(item #>> '{}') NOT BETWEEN 1 AND 256
             )
             AND value = COALESCE(
               (SELECT jsonb_agg(item ORDER BY item #>> '{}') FROM jsonb_array_elements(value) item),
               '[]'::jsonb
             )
             AND jsonb_array_length(value) = (
               SELECT count(DISTINCT item) FROM jsonb_array_elements(value) item
             )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_execution_stream_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'trading_execution_stream_append_only';
        END
        $$
        """
    )

    op.execute(
        """
        CREATE TABLE trading_trade_signals (
          seq BIGINT GENERATED ALWAYS AS IDENTITY,
          signal_id TEXT PRIMARY KEY,
          case_id TEXT NOT NULL UNIQUE,
          alpha_contract_sha256 TEXT NOT NULL,
          market_key TEXT NOT NULL,
          direction TEXT NOT NULL,
          observed_at_ns BIGINT NOT NULL,
          expires_at_ns BIGINT NOT NULL,
          evidence_sha256 TEXT NOT NULL,
          alpha_metadata JSONB NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_trade_signal_id_check CHECK (signal_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_trade_signal_case_check CHECK (char_length(case_id) BETWEEN 1 AND 128),
          CONSTRAINT trading_trade_signal_alpha_sha_check
            CHECK (alpha_contract_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_trade_signal_market_check
            CHECK (market_key ~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$'),
          CONSTRAINT trading_trade_signal_direction_check CHECK (direction IN ('long', 'short')),
          CONSTRAINT trading_trade_signal_clock_check
            CHECK (observed_at_ns > 0 AND expires_at_ns > observed_at_ns),
          CONSTRAINT trading_trade_signal_evidence_sha_check
            CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_trade_signal_metadata_check
            CHECK (trading_execution_metadata_valid(alpha_metadata)),
          CONSTRAINT trading_trade_signal_payload_check CHECK (COALESCE((
            jsonb_typeof(payload) = 'object'
            AND trading_jsonb_object_size(payload) = 10
            AND payload ->> 'signal_version' = 'trade_signal_v1'
            AND payload ->> 'signal_id' = signal_id
            AND payload ->> 'case_id' = case_id
            AND payload ->> 'alpha_contract_sha256' = alpha_contract_sha256
            AND payload ->> 'market_key' = market_key
            AND payload ->> 'direction' = direction
            AND (payload ->> 'observed_at_ns')::bigint = observed_at_ns
            AND (payload ->> 'expires_at_ns')::bigint = expires_at_ns
            AND payload ->> 'evidence_sha256' = evidence_sha256
            AND payload -> 'alpha_metadata' = alpha_metadata
          ), FALSE))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ix_trading_trade_signals_unresolved
          ON trading_trade_signals (seq)
          INCLUDE (signal_id, alpha_contract_sha256, expires_at_ns, payload)
        """
    )

    op.execute(
        """
        CREATE TABLE trading_operator_intents (
          seq BIGINT GENERATED ALWAYS AS IDENTITY,
          command_id TEXT PRIMARY KEY,
          target_profile_id TEXT NOT NULL,
          action TEXT NOT NULL,
          scope TEXT NOT NULL,
          reason TEXT NOT NULL,
          operator_identity TEXT NOT NULL,
          authentication_identity TEXT NOT NULL,
          requested_at_ns BIGINT NOT NULL,
          expires_at_ns BIGINT NOT NULL,
          confirmation_identity TEXT,
          market_key TEXT,
          direction TEXT,
          payload JSONB NOT NULL,
          CONSTRAINT trading_operator_intent_profile_unique UNIQUE (command_id, target_profile_id),
          CONSTRAINT trading_operator_intent_id_check CHECK (command_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_operator_intent_profile_check
            CHECK (target_profile_id ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'),
          CONSTRAINT trading_operator_intent_action_check CHECK (
            action IN ('pause_entries', 'resume_entries', 'emergency_halt', 'flatten', 'manual_entry')
          ),
          CONSTRAINT trading_operator_intent_text_check CHECK (
            char_length(scope) BETWEEN 1 AND 128
            AND char_length(reason) BETWEEN 1 AND 256
            AND char_length(operator_identity) BETWEEN 1 AND 128
            AND char_length(authentication_identity) BETWEEN 1 AND 256
          ),
          CONSTRAINT trading_operator_intent_clock_check CHECK (
            requested_at_ns > 0
            AND expires_at_ns > requested_at_ns
            AND expires_at_ns - requested_at_ns <= 3600000000000
          ),
          CONSTRAINT trading_operator_intent_confirmation_check CHECK (
            (action IN ('resume_entries', 'emergency_halt', 'flatten')
              AND confirmation_identity IS NOT NULL
              AND confirmation_identity ~ '^[0-9a-f]{64}$')
            OR (action NOT IN ('resume_entries', 'emergency_halt', 'flatten')
              AND confirmation_identity IS NULL)
          ),
          CONSTRAINT trading_operator_intent_manual_entry_check CHECK (
            (action = 'manual_entry'
              AND market_key IS NOT NULL
              AND market_key ~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$'
              AND direction IS NOT NULL
              AND direction IN ('long', 'short'))
            OR (action <> 'manual_entry' AND market_key IS NULL AND direction IS NULL)
          ),
          CONSTRAINT trading_operator_intent_payload_check CHECK (COALESCE((
            jsonb_typeof(payload) = 'object'
            AND trading_jsonb_object_size(payload) = 13
            AND payload ->> 'intent_version' = 'operator_intent_v1'
            AND payload ->> 'command_id' = command_id
            AND payload ->> 'target_profile_id' = target_profile_id
            AND payload ->> 'action' = action
            AND payload ->> 'scope' = scope
            AND payload ->> 'reason' = reason
            AND payload ->> 'operator_identity' = operator_identity
            AND payload ->> 'authentication_identity' = authentication_identity
            AND (payload ->> 'requested_at_ns')::bigint = requested_at_ns
            AND (payload ->> 'expires_at_ns')::bigint = expires_at_ns
            AND payload ->> 'confirmation_identity' IS NOT DISTINCT FROM confirmation_identity
            AND payload ->> 'market_key' IS NOT DISTINCT FROM market_key
            AND payload ->> 'direction' IS NOT DISTINCT FROM direction
          ), FALSE))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ix_trading_operator_intents_unresolved
          ON trading_operator_intents (target_profile_id, seq) INCLUDE (command_id, expires_at_ns)
        """
    )

    op.execute(
        """
        CREATE TABLE trading_execution_profile_activations (
          runtime_profile_id TEXT PRIMARY KEY,
          account_slot TEXT NOT NULL,
          activated_after_signal_seq BIGINT NOT NULL,
          activated_after_command_seq BIGINT NOT NULL,
          mode TEXT NOT NULL,
          runtime_release TEXT NOT NULL,
          config_sha256 TEXT NOT NULL,
          created_at_ns BIGINT NOT NULL,
          CONSTRAINT trading_execution_activation_profile_check
            CHECK (runtime_profile_id ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'),
          CONSTRAINT trading_execution_activation_slot_check
            CHECK (account_slot ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'),
          CONSTRAINT trading_execution_activation_fence_check
            CHECK (activated_after_signal_seq >= 0 AND activated_after_command_seq >= 0),
          CONSTRAINT trading_execution_activation_mode_check CHECK (mode IN ('disabled', 'paper', 'live')),
          CONSTRAINT trading_execution_activation_release_check
            CHECK (char_length(runtime_release) BETWEEN 1 AND 128),
          CONSTRAINT trading_execution_activation_config_check
            CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_execution_activation_clock_check CHECK (created_at_ns > 0)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE trading_execution_observations (
          seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
          event_id TEXT PRIMARY KEY,
          runtime_profile_id TEXT NOT NULL,
          runtime_release TEXT NOT NULL,
          execution_strategy TEXT NOT NULL,
          signal_id TEXT REFERENCES trading_trade_signals(signal_id) ON DELETE RESTRICT,
          command_id TEXT,
          normalized_kind TEXT NOT NULL,
          occurred_at_ns BIGINT NOT NULL,
          observed_at_ns BIGINT NOT NULL,
          native_identity_references JSONB NOT NULL,
          summary JSONB NOT NULL,
          payload_digest TEXT NOT NULL,
          payload JSONB NOT NULL,
          CONSTRAINT trading_execution_observation_command_fk
            FOREIGN KEY (command_id, runtime_profile_id)
            REFERENCES trading_operator_intents(command_id, target_profile_id) ON DELETE RESTRICT,
          CONSTRAINT trading_execution_observation_id_check CHECK (event_id ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_execution_observation_profile_check
            CHECK (runtime_profile_id ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'),
          CONSTRAINT trading_execution_observation_release_check
            CHECK (char_length(runtime_release) BETWEEN 1 AND 128),
          CONSTRAINT trading_execution_observation_strategy_check
            CHECK (execution_strategy ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'),
          CONSTRAINT trading_execution_observation_kind_check CHECK (
            normalized_kind IN (
              'signal_disposition', 'control_disposition', 'risk', 'order', 'fill',
              'position', 'protection', 'reconciliation', 'readiness', 'audit_gap'
            )
          ),
          CONSTRAINT trading_execution_observation_correlation_check CHECK (
            NOT (signal_id IS NOT NULL AND command_id IS NOT NULL)
            AND (normalized_kind <> 'signal_disposition' OR signal_id IS NOT NULL)
            AND (normalized_kind <> 'control_disposition' OR command_id IS NOT NULL)
          ),
          CONSTRAINT trading_execution_observation_clock_check
            CHECK (occurred_at_ns > 0 AND observed_at_ns >= occurred_at_ns),
          CONSTRAINT trading_execution_observation_native_refs_check
            CHECK (trading_execution_string_array_valid(native_identity_references)),
          CONSTRAINT trading_execution_observation_summary_check
            CHECK (trading_execution_metadata_valid(summary)),
          CONSTRAINT trading_execution_observation_digest_check
            CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_execution_observation_payload_check CHECK (COALESCE((
            jsonb_typeof(payload) = 'object'
            AND trading_jsonb_object_size(payload) = 13
            AND payload ->> 'observation_version' = 'execution_observation_v1'
            AND payload ->> 'event_id' = event_id
            AND payload ->> 'runtime_profile_id' = runtime_profile_id
            AND payload ->> 'runtime_release' = runtime_release
            AND payload ->> 'execution_strategy' = execution_strategy
            AND payload ->> 'signal_id' IS NOT DISTINCT FROM signal_id
            AND payload ->> 'command_id' IS NOT DISTINCT FROM command_id
            AND payload ->> 'normalized_kind' = normalized_kind
            AND (payload ->> 'occurred_at_ns')::bigint = occurred_at_ns
            AND (payload ->> 'observed_at_ns')::bigint = observed_at_ns
            AND payload -> 'native_identity_references' = native_identity_references
            AND payload -> 'summary' = summary
            AND payload ->> 'payload_digest' = payload_digest
          ), FALSE))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_trading_execution_signal_disposition
          ON trading_execution_observations (runtime_profile_id, execution_strategy, signal_id)
          WHERE normalized_kind = 'signal_disposition'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_trading_execution_control_disposition
          ON trading_execution_observations (runtime_profile_id, execution_strategy, command_id)
          WHERE normalized_kind = 'control_disposition'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_trading_execution_observations_runtime
          ON trading_execution_observations (runtime_profile_id, seq)
        """
    )

    for table in (
        "trading_trade_signals",
        "trading_operator_intents",
        "trading_execution_observations",
        "trading_execution_profile_activations",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_trading_execution_stream_mutation()
            """
        )

    tables = (
        "trading_trade_signals, trading_operator_intents, "
        "trading_execution_observations, trading_execution_profile_activations"
    )
    op.execute(f"REVOKE ALL ON {tables} FROM tracefold_serve, tracefold_workers, tracefold_nautilus")
    op.execute(f"GRANT SELECT ON {tables} TO tracefold_serve")
    op.execute(f"GRANT SELECT ON {tables} TO tracefold_workers")
    op.execute("GRANT INSERT ON trading_trade_signals, trading_operator_intents TO tracefold_workers")
    op.execute("GRANT INSERT ON trading_execution_profile_activations TO tracefold_workers")
    op.execute(f"GRANT SELECT ON {tables} TO tracefold_nautilus")
    op.execute("GRANT INSERT ON trading_execution_observations TO tracefold_nautilus")

    sequences = (
        "trading_trade_signals_seq_seq, trading_operator_intents_seq_seq, trading_execution_observations_seq_seq"
    )
    op.execute(f"REVOKE ALL ON {sequences} FROM tracefold_serve, tracefold_workers, tracefold_nautilus")
    op.execute(
        "GRANT USAGE, SELECT ON trading_trade_signals_seq_seq, trading_operator_intents_seq_seq TO tracefold_workers"
    )
    op.execute("GRANT USAGE, SELECT ON trading_execution_observations_seq_seq TO tracefold_nautilus")


def downgrade() -> None:
    raise RuntimeError("20260831_0340 is an irreversible durable-stream migration; restore the verified backup")
