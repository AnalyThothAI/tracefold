"""Replace Nautilus execution facts with a venue-reconciled DEMO ledger (#746 PR-1).

Migration evidence:
- category: stopped-writer, irreversible Trading execution hard cut.
- why_database_must_change: a Signal needs exactly one disposition and every
  client order/fill needs a durable native identity independent of Nautilus.
- current/minimum_source_revision: 20260929_0416.
- lock_level_and_order: stop Analysis and Nautilus, confirm 0 DEMO exposure,
  pg_dump all trading_* tables; ACCESS EXCLUSIVE on the eight retired tables.
- statement_timeout: 60s; lock_timeout: 5s.
- estimated_rows/bytes: old execution rows discarded; new tables start empty.
- preflight: #746 Step 0 receipt and verified backup are required before deploy.
- archive_current_compatibility: no old Signal, Plan or observation is read.
- role_and_grant_impact: application role owns new tables via existing migration role.
- failure_state: transactional rollback leaves old tables intact.
- roll_forward_or_verified_backup_restore: forward only or restore the verified backup.

Revision ID: 20260929_0417
Revises: 20260929_0416
"""

from alembic import op

revision = "20260929_0417"
down_revision = "20260929_0416"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("DROP TRIGGER trading_cases_signal_link ON public.trading_cases")
    for table in (
        "trading_entry_validity_checks",
        "trading_signal_retirements",
        "trading_execution_observations",
        "trading_trade_plans",
        "trading_trade_signals",
        "trading_operator_intents",
        "trading_execution_runtime_state",
        "trading_execution_runtime_control_state",
    ):
        op.execute(f"DROP TABLE public.{table}")
    op.execute("DROP FUNCTION public.enforce_trading_case_signal_link()")
    op.execute("DROP FUNCTION public.reject_trading_execution_stream_mutation()")
    op.execute("DROP FUNCTION public.trading_trade_plan_guard()")
    op.execute("""
        CREATE TABLE public.trading_signals (
            signal_id text PRIMARY KEY CHECK (signal_id ~ '^[0-9a-f]{64}$'),
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            case_id text NOT NULL REFERENCES public.trading_cases(case_id),
            decision_id text NOT NULL,
            account_slot text NOT NULL,
            native_symbol text NOT NULL,
            decided_at_ns bigint NOT NULL,
            expires_at_ns bigint NOT NULL,
            payload jsonb NOT NULL,
            created_at_ns bigint NOT NULL,
            CHECK (expires_at_ns > decided_at_ns),
            CHECK (expires_at_ns <= decided_at_ns + 300000000000)
        )
    """)
    op.execute("CREATE INDEX ix_trading_signals_cursor ON public.trading_signals (account_slot, seq)")
    op.execute("""
        CREATE TABLE public.trading_operator_intents (
            command_id text PRIMARY KEY CHECK (command_id ~ '^[0-9a-f]{64}$'),
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            account_slot text NOT NULL,
            action text NOT NULL CHECK (action IN
                ('pause_entries','resume_entries','emergency_halt','flatten','manual_entry')),
            scope text NOT NULL,
            reason text NOT NULL,
            operator_identity text NOT NULL,
            authentication_identity text NOT NULL,
            requested_at_ns bigint NOT NULL,
            expires_at_ns bigint NOT NULL,
            payload jsonb NOT NULL,
            CHECK (expires_at_ns > requested_at_ns)
        )
    """)
    op.execute("CREATE INDEX ix_trading_operator_intents_cursor ON public.trading_operator_intents (account_slot, seq)")
    op.execute("""
        CREATE TABLE public.trading_dispositions (
            input_kind text NOT NULL CHECK (input_kind IN ('signal','intent')),
            input_id text NOT NULL,
            account_slot text NOT NULL,
            disposition text NOT NULL CHECK (disposition IN ('accepted','refused','expired')),
            reason text NOT NULL,
            plan_id text,
            decided_at_ns bigint NOT NULL,
            PRIMARY KEY (input_kind, input_id)
        )
    """)
    op.execute(
        "CREATE INDEX ix_trading_dispositions_account ON public.trading_dispositions (account_slot, decided_at_ns)"
    )
    op.execute("""
        CREATE TABLE public.trading_plans (
            plan_id text PRIMARY KEY CHECK (plan_id ~ '^[0-9a-f]{64}$'),
            signal_id text UNIQUE REFERENCES public.trading_signals(signal_id),
            command_id text UNIQUE REFERENCES public.trading_operator_intents(command_id),
            account_slot text NOT NULL,
            environment text NOT NULL CHECK (environment = 'DEMO'),
            native_symbol text NOT NULL,
            side text NOT NULL CHECK (side IN ('long','short')),
            quantity numeric NOT NULL CHECK (quantity > 0),
            reference_price numeric NOT NULL CHECK (reference_price > 0),
            reserved_notional numeric NOT NULL CHECK (reserved_notional > 0),
            stop_bps integer NOT NULL CHECK (stop_bps > 0),
            tp_bps integer NOT NULL CHECK (tp_bps > 0),
            max_hold_s integer NOT NULL CHECK (max_hold_s > 0),
            status text NOT NULL CHECK (status IN
                ('accepted','entry_unknown','entry_working','open','closing','terminal')),
            opened_at_ns bigint,
            terminal_at_ns bigint,
            terminal_reason text,
            pnl_status text NOT NULL DEFAULT 'pending' CHECK (pnl_status IN
                ('pending','complete','evidence_incomplete')),
            realized_pnl numeric,
            fees numeric,
            net_pnl numeric,
            pnl_deadline_ns bigint,
            updated_at_ns bigint NOT NULL,
            CHECK ((signal_id IS NULL) <> (command_id IS NULL)),
            CHECK ((status = 'terminal') = (terminal_at_ns IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_trading_plans_active_symbol
        ON public.trading_plans(account_slot, environment, native_symbol)
        WHERE terminal_at_ns IS NULL
    """)
    op.execute(
        "CREATE INDEX ix_trading_plans_active ON public.trading_plans(account_slot, updated_at_ns) "
        "WHERE terminal_at_ns IS NULL"
    )
    op.execute("""
        CREATE TABLE public.trading_orders (
            client_order_id text PRIMARY KEY CHECK (client_order_id ~ '^[.A-Z:/a-z0-9_-]{1,36}$'),
            plan_id text REFERENCES public.trading_plans(plan_id),
            command_id text REFERENCES public.trading_operator_intents(command_id),
            environment text NOT NULL CHECK (environment = 'DEMO'),
            native_symbol text NOT NULL,
            leg text NOT NULL CHECK (leg IN ('entry','sl','tp','time_exit','safety_flatten','account_flatten')),
            attempt integer NOT NULL CHECK (attempt BETWEEN 1 AND 3),
            venue_order_id text,
            status text NOT NULL CHECK (status IN
                ('reserved','unknown','submitted','working','filled','cancelled','rejected','not_submitted')),
            error_code integer,
            submitted_at_ns bigint,
            resolved_at_ns bigint,
            evidence jsonb,
            updated_at_ns bigint NOT NULL,
            CHECK ((plan_id IS NULL) <> (command_id IS NULL)),
            UNIQUE (plan_id, leg, attempt),
            UNIQUE (command_id, native_symbol, leg, attempt),
            UNIQUE (environment, native_symbol, venue_order_id)
        )
    """)
    op.execute("CREATE INDEX ix_trading_orders_plan ON public.trading_orders(plan_id, leg, attempt)")
    op.execute("""
        CREATE TABLE public.trading_fills (
            environment text NOT NULL CHECK (environment = 'DEMO'),
            native_symbol text NOT NULL,
            trade_id bigint NOT NULL,
            venue_order_id text NOT NULL,
            quantity numeric NOT NULL CHECK (quantity > 0),
            price numeric NOT NULL CHECK (price > 0),
            realized_pnl numeric NOT NULL,
            fee numeric NOT NULL,
            fee_asset text NOT NULL,
            traded_at_ns bigint NOT NULL,
            evidence jsonb NOT NULL,
            PRIMARY KEY (environment, native_symbol, trade_id)
        )
    """)
    op.execute(
        "CREATE INDEX ix_trading_fills_venue_order ON public.trading_fills(environment,native_symbol,venue_order_id)"
    )
    op.execute("""
        CREATE TABLE public.trading_fill_attributions (
            environment text NOT NULL CHECK (environment = 'DEMO'),
            native_symbol text NOT NULL,
            trade_id bigint NOT NULL,
            plan_id text REFERENCES public.trading_plans(plan_id),
            command_id text REFERENCES public.trading_operator_intents(command_id),
            client_order_id text NOT NULL REFERENCES public.trading_orders(client_order_id),
            attributed_at_ns bigint NOT NULL,
            CHECK ((plan_id IS NULL) <> (command_id IS NULL)),
            PRIMARY KEY (environment, native_symbol, trade_id),
            FOREIGN KEY (environment, native_symbol, trade_id)
                REFERENCES public.trading_fills(environment, native_symbol, trade_id)
        )
    """)
    op.execute("CREATE INDEX ix_trading_fill_attributions_plan ON public.trading_fill_attributions(plan_id)")
    op.execute("""
        CREATE TABLE public.trading_trade_cursors (
            environment text NOT NULL CHECK (environment = 'DEMO'),
            native_symbol text NOT NULL,
            next_trade_id bigint NOT NULL CHECK (next_trade_id >= 0),
            checked_at_ns bigint NOT NULL,
            PRIMARY KEY (environment, native_symbol)
        )
    """)
    op.execute("""
        CREATE TABLE public.trading_executor_state (
            account_slot text PRIMARY KEY,
            environment text NOT NULL CHECK (environment = 'DEMO'),
            heartbeat_at_ns bigint NOT NULL,
            last_signal_seq bigint NOT NULL DEFAULT 0,
            last_intent_seq bigint NOT NULL DEFAULT 0,
            last_full_reconcile_at_ns bigint,
            account_snapshot jsonb,
            unexpected_exposure boolean NOT NULL DEFAULT false,
            last_error text,
            CHECK (last_signal_seq >= 0 AND last_intent_seq >= 0)
        )
    """)
    op.execute("""
        CREATE TABLE public.trading_control_state (
            account_slot text PRIMARY KEY,
            entries_paused boolean NOT NULL DEFAULT true,
            emergency_halted boolean NOT NULL DEFAULT false,
            flatten_command_id text,
            updated_at_ns bigint NOT NULL,
            CHECK (NOT emergency_halted OR entries_paused)
        )
    """)
    op.execute("""
        CREATE FUNCTION public.reject_trading_executor_append_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'trading executor fact is append-only';
        END
        $$
    """)
    for table in (
        "trading_signals",
        "trading_operator_intents",
        "trading_dispositions",
        "trading_fills",
        "trading_fill_attributions",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.reject_trading_executor_append_mutation()"
        )


def downgrade() -> None:
    raise RuntimeError("trading_execution_hard_cut_forward_only_restore_verified_backup")
