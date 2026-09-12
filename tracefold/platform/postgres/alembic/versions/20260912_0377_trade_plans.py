"""Persist execution ownership before entry (#644).

Migration evidence:
- category: forward execution ownership hard cut.
- why_database_must_change: an audit observation cannot authorize crash-safe ownership.
- current_source_revision: 20260912_0376
- minimum_supported_source_revision: 20260912_0376
- lock_level_and_order: ACCESS EXCLUSIVE new Trading table and runtime projection.
- statement_timeout: 60s
- lock_timeout: 5s
- estimated_rows: new table empty; one current runtime row per account slot.
- estimated_bytes: bounded plan rows; no history copy or rewrite.
- rewrite_or_index_build: empty indexes; runtime deadline initially unproven.
- preflight_and_maintenance_boundary: stop runtime and application writers; verify
  backup and complete Binance Demo proof. Backfill only uniquely proven old intent
  and parameters, or explicitly flatten paper before cutover. Never guess old risk.
- archive_current_compatibility: observations retained as history, never recovery input.
- role_and_grant_impact: unchanged application login.
- failure_state: transactional rollback leaves prior schema intact.
- roll_forward_or_verified_backup_restore: forward repair or verified stopped restore.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296
"""

from alembic import op

revision = "20260912_0377"
down_revision = "20260912_0376"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("""
        CREATE TABLE trading_trade_plans (
            entry_id text PRIMARY KEY CHECK (entry_id ~ '^[0-9a-f]{64}$'),
            source text NOT NULL CHECK (source IN ('signal', 'manual')),
            case_id text,
            account_slot text NOT NULL,
            runtime_mode_at_creation text NOT NULL CHECK (runtime_mode_at_creation IN ('paper', 'live')),
            market_key text NOT NULL,
            instrument_id text NOT NULL,
            direction text NOT NULL CHECK (direction IN ('long', 'short')),
            entry_client_order_id text NOT NULL UNIQUE,
            created_at_ns bigint NOT NULL,
            entry_expires_at_ns bigint NOT NULL,
            entry_quantity numeric NOT NULL,
            stop_distance_bps integer NOT NULL,
            risk_budget_usd numeric NOT NULL,
            max_leverage_at_creation integer NOT NULL,
            exit_policy_id text NOT NULL CHECK (exit_policy_id IN ('oi_fixed_v1')),
            take_profit_bps integer NOT NULL,
            max_holding_ns bigint NOT NULL,
            status text NOT NULL CHECK (
                status IN ('prepared', 'entry_working', 'open', 'closing', 'closed', 'unresolved')),
            opened_at_ns bigint,
            terminal_at_ns bigint,
            exit_reason text CHECK (exit_reason IN ('stop_filled', 'take_profit', 'time_exit',
                'operator_flatten', 'protection_failure', 'recovery_safety_flatten', 'venue_unknown', 'not_submitted')),
            history_gap_reason text,
            updated_at_ns bigint NOT NULL,
            CHECK (entry_expires_at_ns > created_at_ns),
            CHECK (updated_at_ns >= created_at_ns),
            CHECK ((status = 'closed') = (terminal_at_ns IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_trading_trade_plans_active_instrument
        ON trading_trade_plans (account_slot, runtime_mode_at_creation, instrument_id)
        WHERE terminal_at_ns IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_trading_trade_plans_active
        ON trading_trade_plans (account_slot, runtime_mode_at_creation, created_at_ns, entry_id)
        WHERE terminal_at_ns IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_trading_trade_plans_history
        ON trading_trade_plans (account_slot, created_at_ns DESC, entry_id)
    """)
    op.execute("""
        CREATE FUNCTION trading_trade_plan_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'trade_plan_delete_forbidden';
            END IF;
            IF (to_jsonb(NEW) - ARRAY[
                    'status','opened_at_ns','terminal_at_ns','exit_reason','history_gap_reason','updated_at_ns'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY[
                    'status','opened_at_ns','terminal_at_ns','exit_reason','history_gap_reason','updated_at_ns']) THEN
                RAISE EXCEPTION 'trade_plan_frozen_intent_immutable';
            END IF;
            IF OLD.terminal_at_ns IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'trade_plan_terminal_immutable';
            END IF;
            IF OLD.opened_at_ns IS NOT NULL AND NEW.opened_at_ns IS DISTINCT FROM OLD.opened_at_ns THEN
                RAISE EXCEPTION 'trade_plan_open_clock_immutable';
            END IF;
            IF NEW.updated_at_ns < OLD.updated_at_ns THEN
                RAISE EXCEPTION 'trade_plan_update_clock_regressed';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trading_trade_plan_guard BEFORE UPDATE OR DELETE ON trading_trade_plans
        FOR EACH ROW EXECUTE FUNCTION trading_trade_plan_guard()
    """)
    op.execute("ALTER TABLE trading_execution_runtime_state ADD COLUMN facts_expire_at_ns bigint NOT NULL DEFAULT 0")


def downgrade() -> None:
    raise RuntimeError("trade_plan_forward_only")
