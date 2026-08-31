"""Hard-cut Trading execution to Binance USD-M Demo only (#429).

Migration evidence:

- category: destructive execution-contract hard cut
- why_database_must_change: the prior dual-mainnet active pointers can authorize a runtime the
  new Demo-only process never constructs; obsolete global capability/bootstrap fields also retain
  a second readiness truth after #426
- current_source_revision: 20260831_0339
- minimum_supported_source_revision: 20260831_0339
- lock_level_and_order: singleton capital row, then the two binding rows, then metadata-only column
  drops and one function replacement
- statement_timeout: 10s
- lock_timeout: 1s
- estimated_rows: one capital row and two binding rows
- rewrite_or_index_build: none
- preflight_and_maintenance_boundary: no nonterminal Intent and no reported account exposure
- archive_current_compatibility: historical facts remain append-only; only active pointers are cut
- role_and_grant_impact: obsolete column grants disappear; existing table roles remain unchanged
- failure_state: transactional DDL rolls back and stopped writers retain the old contract
- roll_forward_or_verified_backup_restore: roll forward; use the verified pre-migration backup
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
    # Match the production writer order before proving the cutover boundary.  Checking first leaves
    # a race where a capital writer can pass its own guards, wait behind this migration, and publish
    # an Intent after the migration has already accepted a stale empty result.  The binding locks do
    # the same for account/exposure projection.  Both checks below therefore observe the state after
    # every earlier writer has either committed or rolled back.
    op.execute("SELECT id FROM trading_runtime_state WHERE id = 1 FOR UPDATE")
    op.execute(
        "SELECT binding FROM trading_binding_runtime "
        "WHERE binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP') ORDER BY binding FOR UPDATE"
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM trading_intents
             WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
          ) OR EXISTS (
            SELECT 1 FROM trading_binding_runtime WHERE account_state = 'exposure_present'
          ) THEN
            RAISE EXCEPTION 'binance_demo_execution_cut_requires_flat_drained_runtime';
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        UPDATE trading_runtime_state
           SET control = 'PAUSED',
               arm_epoch = arm_epoch + CASE WHEN control = 'PAUSED' THEN 0 ELSE 1 END,
               updated_at_ms = floor(extract(epoch FROM clock_timestamp()) * 1000)::BIGINT
         WHERE id = 1
        """
    )
    op.execute(
        """
        UPDATE trading_binding_runtime
           SET credential_state = 'unconfigured',
               credential_fingerprint = NULL,
               runtime_state = 'stopped',
               account_state = 'unknown',
               capability_state = 'missing',
               capability_snapshot_sha256 = NULL,
               capability_compiled_at_ms = NULL,
               capability_compile_error = NULL,
               execution_binding_sha256 = NULL,
               active_arm_receipt_sha256 = NULL,
               heartbeat_at_ms = NULL,
               reason = CASE binding
                 WHEN 'HYPERLIQUID_PERP' THEN 'execution_binding_disabled'
                 ELSE 'binance_demo_contract_cutover'
               END,
               updated_at_ms = floor(extract(epoch FROM clock_timestamp()) * 1000)::BIGINT
         WHERE binding IN ('BINANCE_USDM', 'HYPERLIQUID_PERP')
        """
    )
    op.execute(
        """
        ALTER TABLE trading_runtime_state
          DROP COLUMN active_capability_snapshot_sha256,
          DROP COLUMN active_capability_included_count,
          DROP COLUMN nautilus_bootstrap_account_zero_at_ms
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION store_trading_venue_catalog_snapshot(
          p_digest TEXT,
          p_binding TEXT,
          p_captured_at_ms BIGINT,
          p_stale_after_ms BIGINT,
          p_instrument_count INTEGER,
          p_payload JSONB,
          p_now_ms BIGINT
        ) RETURNS TABLE(identity_valid BOOLEAN, activated_binding TEXT)
        LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
          PERFORM pg_advisory_xact_lock(hashtextextended(p_digest, 0));
          INSERT INTO trading_venue_catalog_snapshots (
            snapshot_sha256, binding, captured_at_ms, stale_after_ms,
            provider_instrument_count, payload, created_at_ms
          ) VALUES (
            p_digest, p_binding, p_captured_at_ms, p_stale_after_ms,
            p_instrument_count, p_payload, p_now_ms
          )
          ON CONFLICT (snapshot_sha256) DO NOTHING;

          SELECT EXISTS (
            SELECT 1
              FROM trading_venue_catalog_snapshots existing
             WHERE existing.snapshot_sha256 = p_digest
               AND existing.binding = p_binding
               AND existing.captured_at_ms = p_captured_at_ms
               AND existing.stale_after_ms = p_stale_after_ms
               AND existing.provider_instrument_count = p_instrument_count
               AND existing.payload = p_payload
          ) INTO identity_valid;

          activated_binding := NULL;
          IF identity_valid THEN
            UPDATE trading_binding_runtime AS runtime
               SET catalog_state = 'ready',
                   catalog_snapshot_sha256 = p_digest,
                   catalog_captured_at_ms = p_captured_at_ms,
                   capability_state = CASE
                     WHEN p_binding = 'HYPERLIQUID_PERP' THEN 'missing'
                     WHEN runtime.capability_snapshot_sha256 IS NULL THEN 'missing'
                     WHEN EXISTS (
                       SELECT 1 FROM trading_execution_capability_snapshots capability
                        WHERE capability.snapshot_sha256 = runtime.capability_snapshot_sha256
                          AND capability.catalog_snapshot_sha256 = p_digest
                     ) THEN runtime.capability_state
                     ELSE 'stale'
                   END,
                   capability_snapshot_sha256 = CASE
                     WHEN p_binding = 'HYPERLIQUID_PERP' THEN NULL
                     ELSE runtime.capability_snapshot_sha256
                   END,
                   capability_compiled_at_ms = CASE
                     WHEN p_binding = 'HYPERLIQUID_PERP' THEN NULL
                     ELSE runtime.capability_compiled_at_ms
                   END,
                   capability_compile_error = CASE
                     WHEN p_binding = 'HYPERLIQUID_PERP' THEN NULL
                     ELSE runtime.capability_compile_error
                   END,
                   execution_binding_sha256 = CASE
                     WHEN p_binding = 'HYPERLIQUID_PERP' THEN NULL
                     ELSE runtime.execution_binding_sha256
                   END,
                   active_arm_receipt_sha256 = CASE
                     WHEN p_binding = 'HYPERLIQUID_PERP' THEN NULL
                     ELSE runtime.active_arm_receipt_sha256
                   END,
                   reason = CASE
                     WHEN p_binding = 'HYPERLIQUID_PERP' THEN 'execution_binding_disabled'
                     WHEN credential_state = 'unconfigured' THEN 'credentials_unconfigured'
                     WHEN credential_state = 'invalid' THEN 'credentials_invalid'
                     WHEN runtime_state = 'stopped' THEN 'binance_demo_runtime_required'
                     WHEN runtime_state <> 'ready' THEN 'binding_unready'
                     ELSE NULL
                   END,
                   updated_at_ms = p_now_ms
             WHERE runtime.binding = p_binding
         RETURNING runtime.binding INTO activated_binding;
          END IF;
          RETURN NEXT;
        END
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("binance_demo_execution_only_hard_cut_downgrade_unsupported")
