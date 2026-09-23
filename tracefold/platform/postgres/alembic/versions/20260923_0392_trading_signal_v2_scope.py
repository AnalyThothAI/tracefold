"""Scope-owned SignalV2 and frozen dynamic exit plans (#683).

Revision ID: 20260923_0392
Revises: 20260923_0391

Stop new entries for cutover. Existing plans retain their economic fields and
receive stable legacy scopes. Old unexecuted V1 signals are explicitly retired;
the Runtime reader accepts only V2 scoped to its account and environment.
Transactional failure leaves the old schema intact. Restore requires a verified
pre-cutover archive. Locks: ALTER TABLE ACCESS EXCLUSIVE, lock_timeout 5s.
"""

from __future__ import annotations

from alembic import op

revision = "20260923_0392"
down_revision = "20260923_0391"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("""
        ALTER TABLE public.trading_trade_signals
          ADD COLUMN account_slot text,
          ADD COLUMN runtime_mode text CHECK (runtime_mode IN ('paper','live')),
          ADD COLUMN entry_scope_id text,
          ADD COLUMN asset_id text,
          ADD COLUMN mapping_semantics_digest text
    """)
    op.execute("""
        CREATE INDEX ix_trading_trade_signals_v2_account
          ON public.trading_trade_signals
          (account_slot, runtime_mode, expires_at_ns, seq)
          WHERE account_slot IS NOT NULL
    """)
    op.execute("""
        CREATE TABLE public.trading_signal_retirements (
            signal_id text PRIMARY KEY REFERENCES public.trading_trade_signals(signal_id),
            reason text NOT NULL CHECK (reason = 'v2_cutover'),
            retired_at_ns bigint NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO public.trading_signal_retirements (signal_id,reason,retired_at_ns)
        SELECT signal.signal_id,'v2_cutover',
               (extract(epoch from clock_timestamp()) * 1000000000)::bigint
          FROM public.trading_trade_signals signal
         WHERE signal.payload ->> 'signal_version' = 'trade_signal_v1'
           AND NOT EXISTS (SELECT 1 FROM public.trading_trade_plans plan
                            WHERE plan.entry_id=signal.signal_id)
    """)
    op.execute("""
        ALTER TABLE public.trading_trade_plans ADD COLUMN entry_scope_id text
    """)
    # Existing plans' frozen economic fields remain untouched. Their guard
    # rejects every ordinary update, including this one-time identity
    # backfill. The ALTER lock keeps writers out until the guard is restored.
    op.execute("ALTER TABLE public.trading_trade_plans DISABLE TRIGGER trading_trade_plan_guard")
    op.execute("""
        UPDATE public.trading_trade_plans SET entry_scope_id='legacy:' || entry_id
         WHERE entry_scope_id IS NULL
    """)
    op.execute("ALTER TABLE public.trading_trade_plans ENABLE TRIGGER trading_trade_plan_guard")
    op.execute("ALTER TABLE public.trading_trade_plans ALTER COLUMN entry_scope_id SET NOT NULL")
    op.execute("""
        CREATE UNIQUE INDEX ux_trading_trade_plans_entry_scope
          ON public.trading_trade_plans
          (account_slot, runtime_mode_at_creation, entry_scope_id)
    """)
    op.execute("""
        CREATE TABLE public.trading_entry_validity_checks (
            check_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            entry_id text NOT NULL REFERENCES public.trading_trade_plans(entry_id),
            check_version text NOT NULL CHECK (check_version = 'entry_validity_v1'),
            checked_at_ns bigint NOT NULL,
            allowed boolean NOT NULL,
            reason text NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX ix_trading_entry_validity_checks_entry
          ON public.trading_entry_validity_checks (entry_id, checked_at_ns DESC)
    """)
    op.execute("ALTER TABLE public.trading_trade_plans DROP CONSTRAINT trading_trade_plans_exit_policy_id_check")
    op.execute("""
        ALTER TABLE public.trading_trade_plans ADD CONSTRAINT trading_trade_plans_exit_policy_id_check
          CHECK (exit_policy_id IN ('oi_fixed_v1','analysis_dynamic_v1'))
    """)


def downgrade() -> None:
    raise RuntimeError("trading_signal_v2_scope_forward_only")
