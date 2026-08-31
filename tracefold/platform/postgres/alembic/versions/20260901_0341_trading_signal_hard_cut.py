"""Hard-cut Trading from Capital/Intent writers to atomic Case/Signal facts.

Migration evidence:

- category: destructive hard-cut
- why_database_must_change: admit SIGNAL_EMITTED, bind Signal to Case, and reject
  every retired execution mutation
- current_source_revision: 20260831_0340
- minimum_supported_source_revision: 20260831_0340
- lock_level_and_order: maintenance stop; preflight reads, then ACCESS EXCLUSIVE
  constraint/trigger DDL in one transaction
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: current Trading tables are below 100000 rows; no row rewrite or backfill
- estimated_bytes: metadata-only constraints/triggers plus two empty Signal-table btree indexes
- rewrite_or_index_build: no heap rewrite; Signal preflight is empty before both index builds
- preflight_and_maintenance_boundary: Trading PAUSED, no pending/running Case,
  no nonterminal Intent/order or exposure, and every previously executable
  binding explicitly reconciled flat
- archive_current_compatibility: old rows and old Case states remain readable;
  every old execution table becomes immutable
- role_and_grant_impact: none; the single tracefold login remains and the database invariant rejects retired writes
- failure_state: the transaction rolls back completely and all business processes remain stopped
- roll_forward_or_verified_backup_restore: correct with a new forward revision or restore the verified pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260901_0341
Revises: 20260831_0340
Create Date: 2026-09-01 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0341"
down_revision = "20260831_0340"
branch_labels = None
depends_on = None

_RETIRED_EXECUTION_TABLES = (
    "trading_binding_runtime",
    "trading_capital_authorization_receipts",
    "trading_capital_risk_events",
    "trading_capital_risk_reservation_state",
    "trading_capital_risk_reservations",
    "trading_daily_risk_policies",
    "trading_execution_bindings",
    "trading_execution_capability_snapshots",
    "trading_evidence_clock_receipts",
    "trading_evidence_future_capture_batches",
    "trading_intents",
    "trading_nautilus_runtime_starts",
    "trading_operator_arm_receipts",
    "trading_order_observations",
    "trading_orders",
    "trading_production_promotion_grants",
    "trading_production_release_registrations",
    "trading_promotion_grant_revocations",
    "trading_replay_runs",
    "trading_runtime_state",
    "trading_symbol_blacklist",
    "trading_venue_catalog_snapshots",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(
        """
        DO $preflight$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM trading_runtime_state WHERE id = 1 AND control = 'PAUSED'
          ) THEN
            RAISE EXCEPTION 'trading_signal_cutover_requires_paused';
          END IF;
          IF EXISTS (SELECT 1 FROM trading_cases WHERE state IN ('PENDING', 'RUNNING')) THEN
            RAISE EXCEPTION 'trading_signal_cutover_case_nonterminal';
          END IF;
          IF EXISTS (
            SELECT 1 FROM trading_intents
             WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
          ) THEN
            RAISE EXCEPTION 'trading_signal_cutover_intent_nonterminal';
          END IF;
          IF EXISTS (
            SELECT 1 FROM trading_orders
             WHERE state NOT IN ('CLOSED', 'REJECTED', 'REJECTED_BY_OPERATOR')
          ) THEN
            RAISE EXCEPTION 'trading_signal_cutover_order_nonterminal';
          END IF;
          IF EXISTS (SELECT 1 FROM trading_binding_runtime WHERE account_state = 'exposure_present') THEN
            RAISE EXCEPTION 'trading_signal_cutover_exposure_present';
          END IF;
          IF EXISTS (
            SELECT 1
              FROM trading_binding_runtime AS runtime
             WHERE runtime.account_state IS DISTINCT FROM 'reconciled_flat'
               AND (
                 runtime.account_generation > 0
                 OR runtime.credential_state IS DISTINCT FROM 'unconfigured'
                 OR runtime.credential_fingerprint IS NOT NULL
                 OR runtime.runtime_state IS DISTINCT FROM 'stopped'
                 OR runtime.heartbeat_at_ms IS NOT NULL
                 OR runtime.execution_binding_sha256 IS NOT NULL
                 OR runtime.active_arm_receipt_sha256 IS NOT NULL
                 OR EXISTS (
                   SELECT 1
                     FROM trading_execution_bindings AS execution_history
                    WHERE execution_history.binding = runtime.binding
                 )
                 OR EXISTS (
                   SELECT 1
                     FROM trading_intents AS intent_history
                    WHERE intent_history.binding = runtime.binding
                 )
               )
          ) THEN
            RAISE EXCEPTION 'trading_signal_cutover_account_not_reconciled_flat';
          END IF;
          IF EXISTS (SELECT 1 FROM trading_trade_signals) THEN
            RAISE EXCEPTION 'trading_signal_cutover_preexisting_signal';
          END IF;
        END
        $preflight$;
        """
    )
    op.execute("ALTER TABLE trading_cases DROP CONSTRAINT trading_cases_state_check")
    op.execute(
        """
        ALTER TABLE trading_cases
        ADD CONSTRAINT trading_cases_state_check CHECK (
          state IN (
            'PENDING', 'RUNNING', 'NO_TRADE', 'POLICY_REJECTED', 'INTENT_EMITTED',
            'SIGNAL_EMITTED', 'ORDER_PREPARED', 'BLOCKED'
          )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE trading_trade_signals
        ADD CONSTRAINT trading_trade_signals_case_fkey
        FOREIGN KEY (case_id) REFERENCES trading_cases(case_id) ON DELETE RESTRICT
        """
    )
    op.execute("CREATE INDEX ix_trading_trade_signals_observed_at ON trading_trade_signals (observed_at_ns)")
    op.execute("CREATE INDEX ix_trading_trade_signals_expires_at ON trading_trade_signals (expires_at_ns)")
    op.execute(
        """
        CREATE FUNCTION reject_retired_trading_case_state() RETURNS trigger
        LANGUAGE plpgsql SET search_path TO pg_catalog, public AS $function$
        BEGIN
          IF NEW.state IN ('POLICY_REJECTED', 'INTENT_EMITTED', 'ORDER_PREPARED')
             AND (TG_OP = 'INSERT' OR OLD.state IS DISTINCT FROM NEW.state) THEN
            RAISE EXCEPTION 'retired_trading_case_state:%', NEW.state;
          END IF;
          RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_retired_case_state
        BEFORE INSERT OR UPDATE OF state ON trading_cases
        FOR EACH ROW EXECUTE FUNCTION reject_retired_trading_case_state()
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_trading_case_signal_link() RETURNS trigger
        LANGUAGE plpgsql SET search_path TO pg_catalog, public AS $function$
        DECLARE
          v_case_id text := CASE WHEN TG_TABLE_NAME = 'trading_cases' THEN NEW.case_id ELSE NEW.case_id END;
          v_state text;
          v_signal_count bigint;
        BEGIN
          SELECT state INTO v_state FROM public.trading_cases WHERE case_id = v_case_id;
          SELECT count(*) INTO v_signal_count FROM public.trading_trade_signals WHERE case_id = v_case_id;
          IF v_state = 'SIGNAL_EMITTED' AND v_signal_count <> 1 THEN
            RAISE EXCEPTION 'trading_case_signal_link_invalid';
          END IF;
          IF v_signal_count <> 0 AND v_state IS DISTINCT FROM 'SIGNAL_EMITTED' THEN
            RAISE EXCEPTION 'trading_case_signal_state_invalid';
          END IF;
          RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trading_cases_signal_link
        AFTER INSERT OR UPDATE OF state ON trading_cases
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION enforce_trading_case_signal_link()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trading_trade_signals_case_link
        AFTER INSERT OR UPDATE ON trading_trade_signals
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION enforce_trading_case_signal_link()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_retired_trading_execution_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path TO pg_catalog, public AS $function$
        BEGIN
          RAISE EXCEPTION 'retired_trading_execution_table_read_only:%', TG_TABLE_NAME;
        END
        $function$
        """
    )
    for table in _RETIRED_EXECUTION_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER reject_retired_execution_mutation
            BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION reject_retired_trading_execution_mutation()
            """
        )


def downgrade() -> None:
    raise RuntimeError("irreversible Trading hard cut; use the verified backup-restore path recorded above")
