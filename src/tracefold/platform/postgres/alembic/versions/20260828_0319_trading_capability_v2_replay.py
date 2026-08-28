"""Capability-governed TradeIntentV2 and immutable replay receipts (#286).

Revision ID: 20260828_0319
Revises: 20260828_0318
"""

from __future__ import annotations

from alembic import op

revision = "20260828_0319"
down_revision = "20260828_0318"
branch_labels = None
depends_on = None

_V2_POLICY_SHA256 = "5788964eb8e210bb09b2cfc5d540c4d680bc9982ae023f3d72227194ab2c1ff0"


def upgrade() -> None:
    op.execute(
        """
        DO $cutover$
        BEGIN
          LOCK TABLE trading_runtime_state, trading_intents IN SHARE ROW EXCLUSIVE MODE;
          IF NOT EXISTS (SELECT 1 FROM trading_runtime_state WHERE id = 1 AND control = 'PAUSED') THEN
            RAISE EXCEPTION 'trading_v2_cutover_not_paused';
          END IF;
          IF EXISTS (
            SELECT 1 FROM trading_intents
             WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
          ) THEN
            RAISE EXCEPTION 'trading_v2_cutover_nonterminal_intent';
          END IF;
        END
        $cutover$
        """
    )
    op.execute(
        """
        CREATE TABLE trading_execution_capability_snapshots (
          snapshot_sha256      TEXT PRIMARY KEY,
          created_at_ms        BIGINT NOT NULL,
          execution_environment TEXT NOT NULL,
          included_count       INTEGER NOT NULL,
          excluded_count       INTEGER NOT NULL,
          payload              JSONB NOT NULL,
          CONSTRAINT trading_capability_snapshot_sha_check
            CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_capability_snapshot_environment_check
            CHECK (execution_environment = 'BINANCE_USDM_DEMO'),
          CONSTRAINT trading_capability_snapshot_counts_check
            CHECK (included_count > 0 AND excluded_count >= 0),
          CONSTRAINT trading_capability_snapshot_payload_check
            CHECK (payload ->> 'snapshot_version' = 'execution_capability_snapshot_v1')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE trading_replay_runs (
          run_id                   TEXT PRIMARY KEY,
          spec_sha256              TEXT NOT NULL,
          created_at_ms            BIGINT NOT NULL,
          terminal_status          TEXT NOT NULL,
          artifact_path            TEXT NOT NULL,
          artifact_sha256          TEXT NOT NULL,
          source_count             INTEGER NOT NULL,
          directional_count        INTEGER NOT NULL,
          terminal_outcome_count   INTEGER NOT NULL,
          CONSTRAINT trading_replay_runs_sha_check CHECK (
            run_id ~ '^[0-9a-f]{64}$'
            AND spec_sha256 ~ '^[0-9a-f]{64}$'
            AND artifact_sha256 ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT trading_replay_runs_spec_identity_check CHECK (run_id = spec_sha256),
          CONSTRAINT trading_replay_runs_status_check CHECK (terminal_status = 'SUCCEEDED'),
          CONSTRAINT trading_replay_runs_counts_check CHECK (
            source_count >= 0 AND directional_count >= 0 AND terminal_outcome_count >= 0
          )
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_trading_append_only_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'trading_append_only_mutation_forbidden';
        END
        $$
        """
    )
    for table in ("trading_execution_capability_snapshots", "trading_replay_runs"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
        )

    op.execute(
        """
        ALTER TABLE trading_runtime_state
          ADD COLUMN active_capability_snapshot_sha256 TEXT,
          ADD COLUMN active_capability_included_count INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN nautilus_bootstrap_account_zero_at_ms BIGINT,
          ADD COLUMN blacklist_revision BIGINT NOT NULL DEFAULT 0,
          ADD CONSTRAINT trading_runtime_capability_snapshot_fk
            FOREIGN KEY (active_capability_snapshot_sha256)
            REFERENCES trading_execution_capability_snapshots(snapshot_sha256) ON DELETE RESTRICT,
          ADD CONSTRAINT trading_runtime_capability_count_check
            CHECK (active_capability_included_count >= 0),
          ADD CONSTRAINT trading_runtime_bootstrap_zero_at_check
            CHECK (nautilus_bootstrap_account_zero_at_ms IS NULL
                   OR nautilus_bootstrap_account_zero_at_ms >= 0)
        """
    )
    op.execute(
        """
        ALTER TABLE trading_intents
          ADD COLUMN execution_capability_snapshot_sha256 TEXT,
          ADD COLUMN blacklist_revision_at_emission BIGINT,
          ADD COLUMN blacklist_snapshot_sha256_at_emission TEXT,
          ADD COLUMN blacklist_snapshot_payload_at_emission JSONB,
          ADD COLUMN underlying_key TEXT,
          ADD COLUMN blacklist_revision_at_fence BIGINT,
          ADD COLUMN blacklist_snapshot_sha256_at_fence TEXT,
          ADD COLUMN blacklist_snapshot_payload_at_fence JSONB,
          ADD CONSTRAINT trading_intents_capability_snapshot_fk
            FOREIGN KEY (execution_capability_snapshot_sha256)
            REFERENCES trading_execution_capability_snapshots(snapshot_sha256) ON DELETE RESTRICT
        """
    )
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_policy_identity_check")
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_version_check")
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_instrument_check")
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_reason_check")
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_rejected_flat_check")
    op.execute(
        f"""
        ALTER TABLE trading_intents
          ADD CONSTRAINT trading_intents_version_check
            CHECK (intent_version IN ('trade_intent_v1', 'trade_intent_v2')),
          ADD CONSTRAINT trading_intents_v2_shape_check CHECK (
            (intent_version = 'trade_intent_v1'
              AND execution_capability_snapshot_sha256 IS NULL
              AND blacklist_revision_at_emission IS NULL
              AND blacklist_snapshot_sha256_at_emission IS NULL
              AND blacklist_snapshot_payload_at_emission IS NULL
              AND underlying_key IS NULL)
            OR
            (intent_version = 'trade_intent_v2'
              AND intent_policy_sha256 = '{_V2_POLICY_SHA256}'
              AND execution_capability_snapshot_sha256 IS NOT NULL
              AND blacklist_revision_at_emission IS NOT NULL
              AND blacklist_snapshot_sha256_at_emission ~ '^[0-9a-f]{{64}}$'
              AND blacklist_snapshot_payload_at_emission ->> 'snapshot_version' = 'blacklist_snapshot_v1'
              AND underlying_key ~ '^crypto:[A-Z0-9]{{1,32}}$')
          ),
          ADD CONSTRAINT trading_intents_reason_check CHECK (
            reason_code IS NULL OR reason_code IN (
              'intent_expired', 'runtime_not_ready', 'external_exposure', 'blacklisted',
              'capability_mismatch', 'market_unacceptable', 'quantity_unexecutable', 'risk_denied',
              'entry_outcome_unknown', 'protection_unproven',
              'close_outcome_unknown', 'operator_intervention'
            )
          ),
          ADD CONSTRAINT trading_intents_rejected_flat_check CHECK (
            terminal_outcome <> 'REJECTED' OR (
              actual_quantity IS NULL AND opened_at_ms IS NULL AND position_id IS NULL
              AND reason_code IN (
                'runtime_not_ready', 'external_exposure', 'blacklisted', 'capability_mismatch',
                'market_unacceptable', 'quantity_unexecutable', 'risk_denied'
              )
              AND (entry_fenced_at_ms IS NULL OR flat_verified_at_ms IS NOT NULL)
            )
          ),
          ADD CONSTRAINT trading_intents_fence_blacklist_shape CHECK (
            (blacklist_revision_at_fence IS NULL
              AND blacklist_snapshot_sha256_at_fence IS NULL
              AND blacklist_snapshot_payload_at_fence IS NULL)
            OR
            (blacklist_revision_at_fence IS NOT NULL
              AND blacklist_snapshot_sha256_at_fence ~ '^[0-9a-f]{{64}}$'
              AND blacklist_snapshot_payload_at_fence ->> 'snapshot_version' = 'blacklist_snapshot_v1')
          )
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_new_trade_intent_v1() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.intent_version <> 'trade_intent_v2' THEN
            RAISE EXCEPTION 'new_trade_intent_v1_forbidden';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_trading_intents_v2_only BEFORE INSERT ON trading_intents "
        "FOR EACH ROW EXECUTE FUNCTION reject_new_trade_intent_v1()"
    )
    # Expiry is the only authorised blacklist deletion path available to
    # Nautilus. It accepts no caller time and changes rows plus revision under
    # the runtime lock; the role keeps no direct INSERT/UPDATE/DELETE grant.
    op.execute(
        """
        CREATE FUNCTION materialize_trading_blacklist_expiry() RETURNS bigint
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
          v_now_ms bigint := floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint;
          v_removed integer := 0;
          v_revision bigint;
        BEGIN
          PERFORM id FROM public.trading_runtime_state WHERE id = 1 FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'trading_runtime_state_missing';
          END IF;
          DELETE FROM public.trading_symbol_blacklist
           WHERE expires_at_ms IS NOT NULL AND expires_at_ms <= v_now_ms;
          GET DIAGNOSTICS v_removed = ROW_COUNT;
          IF v_removed > 0 THEN
            UPDATE public.trading_runtime_state
               SET blacklist_revision = blacklist_revision + 1,
                   updated_at_ms = v_now_ms
             WHERE id = 1
         RETURNING blacklist_revision INTO v_revision;
          ELSE
            SELECT blacklist_revision INTO v_revision
              FROM public.trading_runtime_state WHERE id = 1;
          END IF;
          RETURN v_revision;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION materialize_trading_blacklist_expiry() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION materialize_trading_blacklist_expiry() TO tracefold_workers, tracefold_nautilus"
    )

    op.execute(
        "REVOKE ALL ON trading_execution_capability_snapshots, trading_replay_runs "
        "FROM tracefold_workers, tracefold_serve, tracefold_nautilus"
    )
    op.execute(
        "GRANT SELECT, INSERT ON trading_execution_capability_snapshots, trading_replay_runs TO tracefold_workers"
    )
    op.execute("GRANT SELECT ON trading_execution_capability_snapshots, trading_replay_runs TO tracefold_serve")
    op.execute("GRANT SELECT ON trading_execution_capability_snapshots TO tracefold_nautilus")
    op.execute("GRANT SELECT ON trading_symbol_blacklist TO tracefold_nautilus")
    op.execute(
        """
        GRANT INSERT (
          execution_capability_snapshot_sha256, blacklist_revision_at_emission,
          blacklist_snapshot_sha256_at_emission, blacklist_snapshot_payload_at_emission,
          underlying_key
        ) ON trading_intents TO tracefold_workers
        """
    )
    op.execute(
        """
        GRANT UPDATE (
          blacklist_revision_at_fence, blacklist_snapshot_sha256_at_fence,
          blacklist_snapshot_payload_at_fence
        ) ON trading_intents TO tracefold_nautilus
        """
    )
    op.execute(
        "GRANT UPDATE (active_capability_snapshot_sha256, active_capability_included_count, "
        "nautilus_bootstrap_account_zero_at_ms, blacklist_revision, nautilus_ready, "
        "nautilus_readiness_reason, updated_at_ms) "
        "ON trading_runtime_state TO tracefold_workers"
    )
    op.execute(
        "GRANT SELECT (active_capability_snapshot_sha256, active_capability_included_count, "
        "nautilus_bootstrap_account_zero_at_ms, blacklist_revision) "
        "ON trading_runtime_state TO tracefold_nautilus"
    )
    op.execute(
        "GRANT UPDATE (nautilus_bootstrap_account_zero_at_ms, updated_at_ms) "
        "ON trading_runtime_state TO tracefold_nautilus"
    )


def downgrade() -> None:
    raise RuntimeError("20260828_0319 owns TradeIntentV2 and replay receipts and cannot be downgraded")
