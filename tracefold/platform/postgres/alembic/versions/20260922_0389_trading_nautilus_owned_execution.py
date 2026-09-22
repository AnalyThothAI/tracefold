"""Nautilus owns execution state: drop the private account proof's ledger, columns and words (#680 PR-1).

Migration evidence:

- category: destructive hard cut of Trading execution facts that lost their writer; the three kinds
  the private account proof, the lifecycle stages and the audit-loss records wrote are deleted with
  their rows, five projection columns and one plan column are dropped, and the three CHECKs that named
  the old vocabulary are restated over the new one. No plan, Signal, Command, disposition, order,
  fill, position or protection row is touched.
- why_database_must_change: the Runtime no longer writes `reconciliation`, `readiness` or `audit_gap`
  observations (Nautilus reconciles the venue itself, a flatten is one disposition, and a refused
  journal row is logged rather than recorded as a gap), so the kind CHECK must stop admitting them and
  the ~95 % of `trading_execution_observations` they are is dead weight every correlated read skips.
  `trading_execution_runtime_state` loses `execution_safe`, `startup_reconciled`, `account_flat`,
  `reconciliation_observed_at_ns` and `facts_expire_at_ns`, the facts of the deleted proof; the two
  CHECKs built on them are restated over what remains, and its `account_snapshot` document changes shape
  (`execution_account_snapshot_v2`), so the stored v1 document is cleared for the next generation to
  rewrite. `trading_trade_plans` loses `history_gap_reason` (the cold-restart PnL label the fill journal
  replaces), its status vocabulary narrows to the three statuses a plan now has, and its exit reasons
  gain `external`: a close this Runtime observed but did not originate.
- current_source_revision: 20260922_0388
- minimum_supported_source_revision: 20260922_0388
- lock_level_and_order: Runtime stopped and the Binance account flat (the deploy sequence). ACCESS
  EXCLUSIVE on `trading_execution_observations` for the trigger lift, the DELETE and the CHECK swap, then
  on `trading_execution_runtime_state`, then on `trading_trade_plans`, in one transaction.
- statement_timeout: 300s set locally by the revision; the DELETE is bounded by the ledger's size
- lock_timeout: 5s set locally by the revision
- estimated_rows: production had ~12,000-20,000 observation rows, ~95 % of them `reconciliation`;
  one runtime-state row per account slot; tens of plan rows
- estimated_bytes: the deleted rows' heap and index entries become dead tuples for autovacuum; the
  column drops are catalog-only
- rewrite_or_index_build: none; `DROP COLUMN` is catalog-only and `ADD CONSTRAINT` validates in place
- preflight_and_maintenance_boundary: `make runtime-down` with the Demo account flat and every plan
  terminal, then `make up` (which migrates), then `make runtime-up`. A plan that is not terminal and
  carries a retired status (`entry_working`, `closing`, `unresolved`) fails the status CHECK and aborts
  the whole revision -- exactly the in-flight state this cut does not migrate.
- archive_current_compatibility: none; the deleted kinds have no reader after this revision
- role_and_grant_impact: unchanged application login
- failure_state: the transaction rolls back completely and the prior schema and rows are intact
- roll_forward_or_verified_backup_restore: `downgrade` refuses. Restore the operator's pre-0389 archive
  from ~/.tracefold/backups/ into a scratch database to read a deleted proof row.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260922_0389
Revises: 20260922_0388
Create Date: 2026-09-22 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260922_0389"
down_revision = "20260922_0388"
branch_labels = None
depends_on = None

_RETIRED_KINDS = ("reconciliation", "readiness", "audit_gap")
_KINDS = ("signal_disposition", "control_disposition", "risk", "order", "fill", "position", "protection")
_RUNTIME_COLUMNS = (
    "execution_safe",
    "startup_reconciled",
    "account_flat",
    "reconciliation_observed_at_ns",
    "facts_expire_at_ns",
)
_PLAN_STATUSES = ("prepared", "open", "closed")
# The historical reasons stay admissible: the rows that carry them are kept.
_EXIT_REASONS = (
    "stop_filled",
    "take_profit",
    "time_exit",
    "operator_flatten",
    "external",
    "venue_unknown",
    "not_submitted",
    "protection_failure",
    "recovery_safety_flatten",
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")

    # The ledger is append-only; this is the one statement in its history that deletes rows, and it
    # runs with the only writer stopped.
    op.execute("DROP TRIGGER trg_trading_execution_observations_append_only ON public.trading_execution_observations")
    op.execute(
        f"DELETE FROM public.trading_execution_observations WHERE normalized_kind IN ({_sql_list(_RETIRED_KINDS)})"  # noqa: S608
    )
    op.execute(
        "ALTER TABLE public.trading_execution_observations DROP CONSTRAINT trading_execution_observation_kind_check"
    )
    op.execute(
        f"""
        ALTER TABLE public.trading_execution_observations
          ADD CONSTRAINT trading_execution_observation_kind_check
            CHECK (normalized_kind IN ({_sql_list(_KINDS)}))
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_trading_execution_observations_append_only
          BEFORE DELETE OR UPDATE ON public.trading_execution_observations
          FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation()
        """
    )

    for constraint in (
        "trading_execution_runtime_safe_check",
        "trading_execution_runtime_armed_check",
        "trading_execution_runtime_clock_check",
        "trading_execution_runtime_protection_check",
    ):
        op.execute(f"ALTER TABLE public.trading_execution_runtime_state DROP CONSTRAINT {constraint}")
    for column in _RUNTIME_COLUMNS:
        op.execute(f"ALTER TABLE public.trading_execution_runtime_state DROP COLUMN {column}")
    # The stored snapshot is the v1 shape and the protection word may be one this schema retires; the
    # row belongs to a stopped Runtime, and the next generation rewrites both on its first heartbeat.
    op.execute(
        """
        UPDATE public.trading_execution_runtime_state
           SET account_snapshot = NULL,
               protection_status = CASE WHEN protection_status IN ('protected', 'unprotected')
                                        THEN protection_status ELSE 'not_applicable' END,
               entries_armed = FALSE,
               entry_block_reason = coalesce(entry_block_reason, 'runtime_stopped')
        """
    )
    op.execute(
        """
        ALTER TABLE public.trading_execution_runtime_state
          ADD CONSTRAINT trading_execution_runtime_armed_check
            CHECK (NOT entries_armed OR (alive AND NOT unexpected_exposure)),
          ADD CONSTRAINT trading_execution_runtime_clock_check
            CHECK (heartbeat_at_ns > 0 AND started_at_ns > 0 AND updated_at_ns >= started_at_ns
                   AND heartbeat_at_ns <= updated_at_ns),
          ADD CONSTRAINT trading_execution_runtime_protection_check
            CHECK (protection_status IN ('not_applicable', 'protected', 'unprotected'))
        """
    )

    op.execute("DROP TRIGGER trading_trade_plan_guard ON public.trading_trade_plans")
    op.execute("ALTER TABLE public.trading_trade_plans DROP COLUMN history_gap_reason")
    op.execute("ALTER TABLE public.trading_trade_plans DROP CONSTRAINT trading_trade_plans_status_check")
    op.execute(
        f"""
        ALTER TABLE public.trading_trade_plans
          ADD CONSTRAINT trading_trade_plans_status_check CHECK (status IN ({_sql_list(_PLAN_STATUSES)}))
        """
    )
    op.execute("ALTER TABLE public.trading_trade_plans DROP CONSTRAINT trading_trade_plans_exit_reason_check")
    op.execute(
        f"""
        ALTER TABLE public.trading_trade_plans
          ADD CONSTRAINT trading_trade_plans_exit_reason_check CHECK (exit_reason IN ({_sql_list(_EXIT_REASONS)}))
        """
    )
    # The same guard, minus the dropped column: intent is frozen, a terminal plan is final, the open
    # clock is written once, and the update clock never runs backwards.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.trading_trade_plan_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'trade_plan_delete_forbidden';
            END IF;
            IF (to_jsonb(NEW) - ARRAY['status','opened_at_ns','terminal_at_ns','exit_reason','updated_at_ns'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['status','opened_at_ns','terminal_at_ns','exit_reason','updated_at_ns']) THEN
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
        """
    )
    op.execute(
        """
        CREATE TRIGGER trading_trade_plan_guard BEFORE UPDATE OR DELETE ON public.trading_trade_plans
        FOR EACH ROW EXECUTE FUNCTION public.trading_trade_plan_guard()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "trading_nautilus_owned_execution_forward_only: 20260922_0389 deletes the private account proof's "
        "observations and projection columns; restore the operator's pre-0389 archive from "
        "~/.tracefold/backups/ to read them"
    )
