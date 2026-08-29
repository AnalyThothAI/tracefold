"""#331 capital lane V3: one Binance OI entry, three terminal Case states, no per-poll counters.

Revision ID: 20260829_0325
Revises: 20260828_0324
Create Date: 2026-08-29

One-way, and it rewrites no historical fact. What it removes is either a constant (`mode` is `'paper'`
on every row that exists), a per-poll counter that was never business truth (`funnel`,
`dspy_calls_today`, `day_key`), or a table whose only writer this release deletes. What it adds is the
frozen policy evidence a Case now carries, and the two admission words the Gate can now say.

The cutover guard is the migration-level statement of #331 Phase 0: a Case frozen under
`trading_manifest_v6` cannot be decided by the v7 policy, and a nonterminal Intent has no owner across
an execution-identity change. Both must be drained before this runs, with Trading `PAUSED`.
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0325"
down_revision = "20260828_0324"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $cutover$
        BEGIN
          IF EXISTS (SELECT 1 FROM trading_cases WHERE state IN ('PENDING', 'RUNNING')) THEN
            RAISE EXCEPTION 'capital_lane_v3_undecided_case';
          END IF;
          IF EXISTS (
            SELECT 1 FROM trading_intents
             WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
          ) THEN
            RAISE EXCEPTION 'capital_lane_v3_nonterminal_intent';
          END IF;
        END
        $cutover$
        """
    )

    # The frozen per-check evidence a Case is decided on: threshold, operator, measured value, pass or
    # fail. Nullable, because every row written before this release has none and none may be invented.
    op.execute("ALTER TABLE trading_cases ADD COLUMN policy_checks JSONB")

    # `mode` was `'paper'` on every row the table has ever held. There is one execution environment and
    # it is named on the Intent; a column that can only take one value is a choice nobody makes.
    op.execute("ALTER TABLE trading_cases DROP CONSTRAINT trading_cases_mode_check")
    op.execute("ALTER TABLE trading_cases DROP COLUMN mode")

    # `RESEARCH_ONLY` is a real market fact from a venue this lane may study and never trade; `venue`
    # and `capability` are the two stages the Gate gained. `routing` stays admissible because rows
    # written under it are still in the table.
    op.execute("ALTER TABLE trading_candidate_gate_decisions DROP CONSTRAINT trading_candidate_gate_status_check")
    op.execute(
        """
        ALTER TABLE trading_candidate_gate_decisions
          ADD CONSTRAINT trading_candidate_gate_status_check CHECK (
            status IN ('DEFERRED', 'REJECTED', 'RESEARCH_ONLY', 'CASE_CREATED', 'EXPIRED')
          )
        """
    )
    op.execute("ALTER TABLE trading_candidate_gate_decisions DROP CONSTRAINT trading_candidate_gate_stage_check")
    op.execute(
        """
        ALTER TABLE trading_candidate_gate_decisions
          ADD CONSTRAINT trading_candidate_gate_stage_check CHECK (
            stage IN ('source', 'venue', 'eligibility', 'capability', 'routing', 'market_context', 'freeze')
          )
        """
    )

    # The two shadow tables. `trading_strategy_evaluations` holds zero rows and
    # `trading_strategy_registrations` holds two, both naming liquidation shadow strategies this
    # release deletes. Neither carries a capital decision, an Intent or an Outcome.
    op.execute("DROP TABLE IF EXISTS trading_strategy_evaluations")
    op.execute("DROP TABLE IF EXISTS trading_strategy_registrations")

    # Per-poll counters, never business truth. `funnel` counted one entry per *re-read* of the same
    # frame and reset at UTC midnight; `dspy_calls_today` budgeted a model call the lane no longer
    # makes; `day_key` existed only to roll the other two. Product statistics are now bounded
    # aggregations over the admission ledger and the Case table.
    op.execute("REVOKE UPDATE ON trading_runtime_state FROM tracefold_workers")
    op.execute("ALTER TABLE trading_runtime_state DROP COLUMN funnel")
    op.execute("ALTER TABLE trading_runtime_state DROP COLUMN dspy_calls_today")
    op.execute("ALTER TABLE trading_runtime_state DROP COLUMN day_key")
    op.execute("GRANT UPDATE (control, updated_at_ms) ON trading_runtime_state TO tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("capital_lane_v3_downgrade_unsupported")
