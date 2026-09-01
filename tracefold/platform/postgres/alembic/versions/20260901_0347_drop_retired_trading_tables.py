"""Drop the 22 read-only execution tables the Signal cut retired, and their orphaned functions.

Migration evidence:

- category: destructive hard-cut
- why_database_must_change: `20260901_0341` made these 22 tables immutable by putting a
  `RAISE EXCEPTION` trigger on every statement, which is what a *retirement* looks like while an
  execution owner is still being rewritten. #433 closed and #449 landed, so the rewrite is over and
  nothing will read or write them again: production `pg_stat_user_tables` shows `ins/upd/del = 0` and
  `idx_scan = 0` on all 22 since the statistics reset, and the tree has no SQL that names one. A table
  that only exists to refuse writes is schema an operator still has to read past, and 33 MB of
  `information_schema` a fresh install still has to create.
- current_source_revision: 20260901_0346
- minimum_supported_source_revision: 20260901_0346
- lock_level_and_order: maintenance stop; one `DROP TABLE` naming all 22 (so the foreign keys
  *between* them do not force an order), then the functions their triggers, defaults and CHECKs held
  down, all in one transaction
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: 390 across the ten tables that have any (`trading_venue_catalog_snapshots` 268,
  `trading_execution_capability_snapshots` 106, the other eight in single digits); twelve are empty
- estimated_bytes: 33 MB total relation size, almost all of it TOASTed catalogue payloads
- rewrite_or_index_build: none; `DROP TABLE` unlinks files rather than rewriting them
- preflight_and_maintenance_boundary: **the absence of `CASCADE` is the preflight.** `DROP TABLE`
  without it refuses if any object outside the drop set still depends on one of these tables, and
  `DROP FUNCTION` without it refuses if a surviving trigger, default or CHECK still calls one. A
  dependency this revision failed to notice therefore aborts the transaction instead of silently
  taking a live object down with it.
- archive_current_compatibility: **not compatible, by design.** The 390 rows are the pre-cut
  execution owner's own record -- reservations, arm receipts, capability snapshots and two orders
  from an environment that no longer exists. They were dumped to
  `~/.tracefold/backups/pre-0347-retired-trading-tables-20260901.sql` before this revision was
  written; the dump is exact because the 0341 triggers had already made every one of these tables
  unwritable, so no row could change between the dump and the drop. `trading_cases`,
  `trading_trade_signals`, `trading_candidate_gate_decisions` and the four execution-stream tables --
  everything the current lane writes and the console reads -- are untouched.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and all 22 tables stay read-only
- roll_forward_or_verified_backup_restore: restore the named dump into a scratch database to read an
  archived row; a forward revision cannot recreate rows this revision deleted
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260901_0347
Revises: 20260901_0346
Create Date: 2026-09-01 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0347"
down_revision = "20260901_0346"
branch_labels = None
depends_on = None

# The exact set `20260901_0341` froze, in `_RETIRED_EXECUTION_TABLES` order. Naming all of them in one
# `DROP TABLE` is what lets the foreign keys among them (intent -> binding -> capability -> catalogue,
# reservation -> arm receipt -> grant -> risk policy) drop without a topological order; a key pointing
# *out* of the set would still abort, which is the check this revision wants.
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

# Every function whose only callers were the tables above: their statement triggers, their row
# validators, their `DEFAULT` clocks, and the two stored procedures the retired owner called directly.
#
# What is deliberately *not* here: `reject_retired_candidate_gate_stage`,
# `reject_retired_trading_case_state`, `enforce_trading_case_signal_link` and
# `reject_trading_execution_stream_mutation` still guard `trading_candidate_gate_decisions`,
# `trading_cases`, `trading_trade_signals`, `trading_operator_intents`,
# `trading_execution_observations` and `trading_execution_profile_activations`; and
# `trading_jsonb_object_size`, `trading_execution_metadata_valid` and
# `trading_execution_string_array_valid` are in live CHECKs on the Signal and observation payloads.
_ORPHANED_FUNCTIONS = (
    # 0341's own read-only guard, and the append-only guard that was only ever on retired tables.
    "reject_retired_trading_execution_mutation()",
    "reject_trading_append_only_mutation()",
    # Row validators for the retired evidence-clock, promotion and capability ledgers.
    "validate_trading_evidence_parent()",
    "validate_trading_future_capture_batch()",
    "validate_trading_promotion_future_evidence()",
    "reject_new_execution_capability_v1()",
    "reject_new_legacy_trade_intent()",
    "reject_trading_terminal_intent_revival()",
    "stamp_trading_release_registration()",
    # Called by the retired owner rather than by a trigger: one materialized the blacklist's TTL, the
    # other wrote a venue catalogue snapshot.
    "materialize_trading_blacklist_expiry()",
    ("store_trading_venue_catalog_snapshot(text, text, bigint, bigint, integer, jsonb, bigint)"),
    # `trading_evidence_now_ms` was the `DEFAULT` on the two evidence ledgers and the release
    # registry; `trading_canonical_jsonb` was called only from `validate_trading_evidence_parent`.
    "trading_evidence_now_ms()",
    "trading_canonical_jsonb(jsonb)",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    tables = ", ".join(f"public.{table}" for table in _RETIRED_EXECUTION_TABLES)
    op.execute(f"DROP TABLE {tables}")

    for signature in _ORPHANED_FUNCTIONS:
        op.execute(f"DROP FUNCTION public.{signature}")


def downgrade() -> None:
    raise RuntimeError(
        "20260901_0347 deletes the retired execution rows; restore "
        "pre-0347-retired-trading-tables-20260901.sql to read them"
    )
