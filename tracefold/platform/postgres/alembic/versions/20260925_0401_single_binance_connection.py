"""Retire business modes while retaining existing Nautilus execution identities.

Migration evidence:
- category: forward-only schema and bounded ledger cutover.
- why_database_must_change: account_slot is the one execution scope; old mode columns and
  indexes would split Signals from Commands and Plans. Existing order namespaces must survive.
- current_source_revision: 20260925_0400.
- minimum_supported_source_revision: 20260925_0400.
- lock_level_and_order: stopped Trading writers; preflight, control backfill, signal
  payload rewrite, then short table DDL locks. Indexes are rebuilt transactionally.
- statement_timeout: 120s; lock_timeout: 5s.
- estimated_rows: existing control rows, old V3 Signals, active Plans and runtime rows.
- preflight_and_maintenance_boundary: stop Analysis and Nautilus and retain a verified
  pre-cut backup. Mixed active modes under one slot fail rather than change account scope.
- archive_current_compatibility: order IDs and opaque namespaces are retained; old
  unsubmitted signals are retired, not replayed. Historical simulation rows are unchanged.
- role_and_grant_impact: none; existing tables and grants remain.
- failure_state: a preflight or DDL failure rolls back the whole transaction.
- roll_forward_or_verified_backup_restore: correct the mixed-slot data under operator
  review or restore the verified backup; never regenerate a submitted order ID.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260925_0401
Revises: 20260925_0400
"""

from __future__ import annotations

from alembic import op

revision = "20260925_0401"
down_revision = "20260925_0400"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM public.trading_trade_plans
             WHERE terminal_at_ns IS NULL GROUP BY account_slot
            HAVING count(DISTINCT runtime_mode_at_creation) > 1
          ) OR EXISTS (
            SELECT 1 FROM public.trading_trade_plans plan
            JOIN public.trading_execution_runtime_state state USING (account_slot)
            WHERE plan.terminal_at_ns IS NULL AND plan.runtime_mode_at_creation <> state.mode
          ) OR EXISTS (
            SELECT 1 FROM public.trading_trade_plans
             GROUP BY account_slot, entry_scope_id HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'connection_cutover_mixed_account_scope';
          END IF;
        END $$
    """)
    op.execute("ALTER TABLE public.trading_execution_runtime_control_state ADD COLUMN execution_namespace text")
    op.execute("""
        INSERT INTO public.trading_execution_runtime_control_state
          (account_slot, execution_namespace, entries_paused, emergency_halted,
           last_command_seq, last_command_id, updated_at_ns)
        SELECT DISTINCT plan.account_slot, NULL, TRUE, FALSE, 0, NULL,
               (extract(epoch FROM transaction_timestamp()) * 1000000000)::bigint
          FROM public.trading_trade_plans plan
        ON CONFLICT (account_slot) DO NOTHING
    """)
    op.execute("""
        UPDATE public.trading_execution_runtime_control_state control
           SET execution_namespace = 'tracefold:' || control.account_slot ||
               COALESCE(':' || state.mode, ':' || last_plan.runtime_mode_at_creation, '')
          FROM (SELECT account_slot FROM public.trading_execution_runtime_control_state) slot
          LEFT JOIN public.trading_execution_runtime_state state USING (account_slot)
          LEFT JOIN LATERAL (
            SELECT runtime_mode_at_creation FROM public.trading_trade_plans plan
             WHERE plan.account_slot = slot.account_slot
             ORDER BY (terminal_at_ns IS NULL) DESC, created_at_ns DESC LIMIT 1
          ) last_plan ON TRUE
         WHERE control.account_slot = slot.account_slot
    """)
    op.execute(
        "ALTER TABLE public.trading_execution_runtime_control_state ALTER COLUMN execution_namespace SET NOT NULL"
    )
    op.execute("""
        ALTER TABLE public.trading_execution_runtime_control_state
          ADD CONSTRAINT trading_execution_namespace_check
          CHECK (execution_namespace ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$')
    """)

    # Old unpublished Signals must not become newly eligible merely because the mode gate disappears.
    op.execute("""
        ALTER TABLE public.trading_signal_retirements DROP CONSTRAINT trading_signal_retirements_reason_check
    """)
    op.execute("""
        ALTER TABLE public.trading_signal_retirements
          ADD CONSTRAINT trading_signal_retirements_reason_check
          CHECK (reason IN ('v2_cutover', 'connection_cutover'))
    """)
    op.execute("""
        INSERT INTO public.trading_signal_retirements (signal_id, reason, retired_at_ns)
        SELECT signal.signal_id, 'connection_cutover',
               (extract(epoch FROM transaction_timestamp()) * 1000000000)::bigint
          FROM public.trading_trade_signals signal
         WHERE signal.payload ->> 'signal_version' = 'trade_signal_v3'
           AND NOT EXISTS (SELECT 1 FROM public.trading_trade_plans plan
                            WHERE plan.entry_id = signal.signal_id)
        ON CONFLICT (signal_id) DO NOTHING
    """)
    op.execute("DROP TRIGGER trg_trading_trade_signals_append_only ON public.trading_trade_signals")
    op.execute("""
        UPDATE public.trading_trade_signals
           SET payload = payload - 'runtime_mode'
         WHERE payload ->> 'signal_version' = 'trade_signal_v3' AND payload ? 'runtime_mode'
    """)
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("""
        CREATE TRIGGER trg_trading_trade_signals_append_only
          BEFORE DELETE OR UPDATE ON public.trading_trade_signals
          FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation()
    """)

    op.execute("DROP INDEX public.ix_trading_trade_signals_account_mode_expiry")
    op.execute("ALTER TABLE public.trading_trade_signals DROP COLUMN runtime_mode")
    op.execute("""
        CREATE INDEX ix_trading_trade_signals_account_expiry
          ON public.trading_trade_signals (account_slot, expires_at_ns, seq)
          WHERE account_slot IS NOT NULL
    """)
    op.execute("DROP INDEX public.ux_trading_trade_plans_active_instrument")
    op.execute("DROP INDEX public.ix_trading_trade_plans_active")
    op.execute("DROP INDEX public.ux_trading_trade_plans_entry_scope")
    op.execute("ALTER TABLE public.trading_trade_plans DROP COLUMN runtime_mode_at_creation")
    op.execute("""
        CREATE UNIQUE INDEX ux_trading_trade_plans_active_instrument
          ON public.trading_trade_plans (account_slot, instrument_id)
          WHERE terminal_at_ns IS NULL
    """)
    op.execute("""
        CREATE INDEX ix_trading_trade_plans_active
          ON public.trading_trade_plans (account_slot, created_at_ns, entry_id)
          WHERE terminal_at_ns IS NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_trading_trade_plans_entry_scope
          ON public.trading_trade_plans (account_slot, entry_scope_id)
    """)
    op.execute(
        "ALTER TABLE public.trading_execution_runtime_state DROP CONSTRAINT trading_execution_runtime_mode_check"
    )
    op.execute("ALTER TABLE public.trading_execution_runtime_state RENAME COLUMN mode TO connection")
    op.execute("""
        UPDATE public.trading_execution_runtime_state
           SET connection = CASE connection WHEN 'paper' THEN 'DEMO' ELSE 'LIVE' END
    """)
    op.execute("""
        ALTER TABLE public.trading_execution_runtime_state
          ADD CONSTRAINT trading_execution_runtime_connection_check
          CHECK (connection IN ('LIVE', 'DEMO', 'TESTNET', 'SDK_DEFAULT'))
    """)


def downgrade() -> None:
    raise RuntimeError("single_connection_forward_only: restore a verified pre-0401 archive")
