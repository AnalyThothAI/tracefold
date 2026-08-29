"""No-key Decision Plane, public venue catalogues, and orthogonal capital facts (#350).

Revision ID: 20260829_0327
Revises: 20260829_0326
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0327"
down_revision = "20260829_0326"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $cutover$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM trading_runtime_state WHERE id = 1 AND control = 'PAUSED') THEN
            RAISE EXCEPTION 'no_key_authority_requires_paused';
          END IF;
          IF EXISTS (SELECT 1 FROM trading_cases WHERE state IN ('PENDING', 'RUNNING')) THEN
            RAISE EXCEPTION 'no_key_authority_undecided_case';
          END IF;
          IF EXISTS (
            SELECT 1 FROM trading_intents
             WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
          ) THEN
            RAISE EXCEPTION 'no_key_authority_nonterminal_intent';
          END IF;
        END
        $cutover$
        """
    )

    op.execute(
        """
        CREATE TABLE trading_decision_runtime (
          id              SMALLINT PRIMARY KEY,
          state           TEXT NOT NULL,
          heartbeat_at_ms BIGINT,
          reason          TEXT,
          updated_at_ms   BIGINT NOT NULL,
          CONSTRAINT trading_decision_runtime_singleton CHECK (id = 1),
          CONSTRAINT trading_decision_runtime_state_check
            CHECK (state IN ('DISABLED', 'STARTING', 'RUNNING', 'FAULTED'))
        )
        """
    )
    op.execute(
        "INSERT INTO trading_decision_runtime (id, state, heartbeat_at_ms, reason, updated_at_ms) "
        "VALUES (1, 'DISABLED', NULL, 'trading_disabled', 0)"
    )

    op.execute(
        """
        CREATE TABLE trading_venue_catalog_snapshots (
          snapshot_sha256          TEXT PRIMARY KEY,
          binding                  TEXT NOT NULL,
          captured_at_ms           BIGINT NOT NULL,
          stale_after_ms            BIGINT NOT NULL,
          provider_instrument_count INTEGER NOT NULL,
          payload                  JSONB NOT NULL,
          created_at_ms            BIGINT NOT NULL,
          CONSTRAINT trading_venue_catalog_sha_check CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT trading_venue_catalog_binding_check
            CHECK (binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')),
          CONSTRAINT trading_venue_catalog_count_check CHECK (provider_instrument_count >= 0),
          CONSTRAINT trading_venue_catalog_stale_check CHECK (stale_after_ms > 0),
          CONSTRAINT trading_venue_catalog_payload_check CHECK (
            payload ->> 'snapshot_version' = 'venue_instrument_catalog_snapshot_v1'
            AND payload ->> 'binding' = binding
            AND (payload ->> 'captured_at_ms')::BIGINT = captured_at_ms
            AND (payload ->> 'provider_instrument_count')::INTEGER = provider_instrument_count
            AND jsonb_array_length(payload -> 'instruments') = provider_instrument_count
          )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_trading_venue_catalog_binding_captured "
        "ON trading_venue_catalog_snapshots (binding, captured_at_ms DESC, snapshot_sha256)"
    )
    op.execute(
        "CREATE TRIGGER trg_trading_venue_catalog_snapshots_append_only "
        "BEFORE UPDATE OR DELETE ON trading_venue_catalog_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION reject_trading_append_only_mutation()"
    )

    op.execute(
        """
        CREATE TABLE trading_binding_runtime (
          binding                       TEXT PRIMARY KEY,
          credential_state               TEXT NOT NULL,
          credential_fingerprint         TEXT,
          runtime_state                  TEXT NOT NULL,
          account_state                  TEXT NOT NULL,
          catalog_state                  TEXT NOT NULL,
          catalog_snapshot_sha256        TEXT,
          catalog_captured_at_ms         BIGINT,
          heartbeat_at_ms                BIGINT,
          reason                         TEXT,
          updated_at_ms                  BIGINT NOT NULL,
          CONSTRAINT trading_binding_runtime_binding_check
            CHECK (binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')),
          CONSTRAINT trading_binding_runtime_credential_check
            CHECK (credential_state IN ('unconfigured', 'configured', 'invalid')),
          CONSTRAINT trading_binding_runtime_fingerprint_check CHECK (
            credential_fingerprint IS NULL OR credential_fingerprint ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT trading_binding_runtime_state_check
            CHECK (runtime_state IN ('stopped', 'starting', 'ready', 'stale', 'faulted')),
          CONSTRAINT trading_binding_account_state_check
            CHECK (account_state IN ('unknown', 'reconciled_flat', 'exposure_present')),
          CONSTRAINT trading_binding_catalog_state_check
            CHECK (catalog_state IN ('missing', 'ready', 'stale', 'error')),
          CONSTRAINT trading_binding_catalog_pair_check CHECK (
            (catalog_snapshot_sha256 IS NULL AND catalog_captured_at_ms IS NULL
              AND catalog_state IN ('missing', 'error'))
            OR (catalog_snapshot_sha256 IS NOT NULL AND catalog_captured_at_ms IS NOT NULL
                AND catalog_state IN ('ready', 'stale'))
          ),
          CONSTRAINT trading_binding_catalog_fk FOREIGN KEY (catalog_snapshot_sha256)
            REFERENCES trading_venue_catalog_snapshots(snapshot_sha256) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        INSERT INTO trading_binding_runtime (
          binding, credential_state, credential_fingerprint, runtime_state, account_state,
          catalog_state, catalog_snapshot_sha256, catalog_captured_at_ms, heartbeat_at_ms, reason, updated_at_ms
        ) VALUES
          ('BINANCE_USDM', 'unconfigured', NULL, 'stopped', 'unknown', 'missing', NULL, NULL, NULL,
           'credentials_unconfigured', 0),
          ('HYPERLIQUID_PERP', 'unconfigured', NULL, 'stopped', 'unknown', 'missing', NULL, NULL, NULL,
           'credentials_unconfigured', 0)
        """
    )

    op.execute("ALTER TABLE trading_cases ADD COLUMN capital_disposition TEXT")
    op.execute("ALTER TABLE trading_cases ADD COLUMN capital_reason TEXT")
    op.execute(
        """
        UPDATE trading_cases
           SET policy_decision = COALESCE(
                 policy_decision,
                 CASE WHEN state IN ('INTENT_EMITTED', 'ORDER_PREPARED') THEN 'long' ELSE 'not_run' END
               ),
               capital_disposition = CASE
                 WHEN state IN ('INTENT_EMITTED', 'ORDER_PREPARED') THEN 'allowed'
                 WHEN state = 'BLOCKED' THEN 'blocked'
                 ELSE 'not_applicable'
               END,
               capital_reason = CASE WHEN state = 'BLOCKED' THEN COALESCE(policy_reason, 'historical_block') END
        """
    )
    op.execute("ALTER TABLE trading_cases ALTER COLUMN policy_decision SET NOT NULL")
    op.execute("ALTER TABLE trading_cases ALTER COLUMN capital_disposition SET NOT NULL")
    op.execute(
        "ALTER TABLE trading_cases ADD CONSTRAINT trading_cases_policy_decision_check "
        "CHECK (policy_decision IN ('long', 'no_trade', 'not_run'))"
    )
    op.execute(
        "ALTER TABLE trading_cases ADD CONSTRAINT trading_cases_capital_disposition_check "
        "CHECK (capital_disposition IN ('allowed', 'blocked', 'not_applicable'))"
    )
    op.execute("ALTER TABLE trading_candidate_gate_decisions DROP CONSTRAINT trading_candidate_gate_stage_check")
    op.execute(
        """
        ALTER TABLE trading_candidate_gate_decisions
          ADD CONSTRAINT trading_candidate_gate_stage_check CHECK (
            stage IN ('source', 'venue', 'eligibility', 'catalog', 'routing', 'market_context', 'freeze')
          )
        """
    )

    op.execute(
        "REVOKE ALL ON trading_decision_runtime, trading_binding_runtime, trading_venue_catalog_snapshots FROM PUBLIC"
    )
    op.execute(
        "GRANT SELECT ON trading_decision_runtime, trading_binding_runtime, "
        "trading_venue_catalog_snapshots TO tracefold_serve"
    )
    op.execute("GRANT SELECT, INSERT ON trading_venue_catalog_snapshots TO tracefold_workers")
    op.execute("REVOKE UPDATE, DELETE ON trading_venue_catalog_snapshots FROM tracefold_workers")
    op.execute("GRANT SELECT, UPDATE ON trading_decision_runtime, trading_binding_runtime TO tracefold_workers")
    op.execute("GRANT SELECT ON trading_binding_runtime TO tracefold_nautilus")
    op.execute(
        "GRANT UPDATE (runtime_state, account_state, heartbeat_at_ms, reason, updated_at_ms) "
        "ON trading_binding_runtime TO tracefold_nautilus"
    )


def downgrade() -> None:
    raise RuntimeError("no_key_decision_catalog_downgrade_unsupported")
