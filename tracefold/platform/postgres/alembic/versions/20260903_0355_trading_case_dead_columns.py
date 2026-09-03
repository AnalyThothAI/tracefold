"""Drop the six dead `trading_cases` columns and the values no writer can reach (#510 PR-5a).

Migration evidence:

- category: destructive hard-cut -- six column drops plus three narrowed CHECKs and two guard
  triggers removed
- why_database_must_change: `regime`, `program_version`, `program_sha256` and `program_output`
  belonged to the pre-#433 model-runner writer; `capital_disposition` and `capital_reason` belonged to
  the capital authority whose 22 tables `20260901_0347` already dropped. The current lane writes the
  literal `'not_applicable'` and `NULL` into the last two on every insert and every settle and reads
  neither back, so the columns are a NOT NULL clause an operator must satisfy to write a Case and a
  shape every reader has to look past. The three retired `trading_cases.state` values and the retired
  `trading_candidate_gate_decisions` `status` / `stage` values are the same fact one level down: they
  are admitted by a CHECK, refused by a trigger, and named a third time in `CaseState`, in
  `schemas/trading.py` and in the console's label tables. One closed vocabulary per column, expressed
  once, is the whole change. Removing `reject_retired_trading_case_state` and
  `reject_retired_candidate_gate_stage` is not a loosening: the narrowed CHECKs refuse strictly more
  than the triggers did (the triggers only fired on a *transition into* a retired value, and only
  `capability` of the three retired stages), and a CHECK is also what `information_schema` shows an
  operator reading the column's domain.
- current_source_revision: 20260903_0354
- minimum_supported_source_revision: 20260903_0354
- lock_level_and_order: canonical migration stop. Two `SELECT count(*)` guards first, so a database
  that still holds a retired value aborts before any DDL; then `DROP TRIGGER` / `DROP FUNCTION` (the
  functions have no other caller, and `DROP FUNCTION` without `CASCADE` proves it); then one
  `ALTER TABLE trading_cases` naming all six `DROP COLUMN`s and the state CHECK swap; then the two
  `trading_candidate_gate_decisions` CHECK swaps. Every statement is ACCESS EXCLUSIVE on one of two
  small tables, in one transaction.
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: production holds 125 `trading_cases` and 159 `trading_candidate_gate_decisions`
  rows. `DROP COLUMN` reads none of them; each re-added CHECK validates the whole (small) table once.
- estimated_bytes: no space is reclaimed by `DROP COLUMN` -- it marks the attribute dropped and leaves
  the tuples alone until they are next rewritten. This revision is about the schema an operator and a
  writer must satisfy, not about disk.
- rewrite_or_index_build: none. `DROP COLUMN` is catalog-only; no index names any of the six columns
  (`ix_trading_cases_created`, `_state`, `_strategy`, `_underlying` and
  `ux_trading_case_in_flight_underlying` all name surviving columns), and no view, trigger, function or
  foreign key reads one.
- preflight_and_maintenance_boundary: **the two `count(*)` guards are the preflight, and they are
  inside the transaction.** A row whose `state` is `POLICY_REJECTED` / `INTENT_EMITTED` /
  `ORDER_PREPARED`, or whose `status` is `RESEARCH_ONLY` or whose `stage` is `capability` / `catalog` /
  `routing`, would fail the narrowed CHECK during validation anyway; raising on the count first is what
  turns that into a named refusal an operator can act on instead of a constraint-violation traceback.
  This revision never deletes a historical row: archiving and deleting them is the operator's step,
  recorded in `docs/MIGRATIONS.md`, and it is the same `pg_dump` to `~/.tracefold/backups/` that
  `20260901_0347` used.
- role_and_grant_impact: none; the single tracefold login is unchanged
- archive_current_compatibility: **not compatible, by design.** The six columns' contents are deleted
  with them and no forward revision can bring them back, which is why the operator's archive dump is
  taken before the upgrade and not after. Every surviving row keeps its Case identity, manifest,
  state, decision, reasons, checks and clocks; the admission ledger keeps every column it has.
- failure_state: the transaction rolls back completely. Either a guard raised and nothing was touched,
  or a `DROP FUNCTION` found a caller this revision failed to notice and nothing was touched.
- roll_forward_or_verified_backup_restore: `downgrade` refuses. Restore the operator's dump into a
  scratch database to read an archived Case column or a deleted retired-value row.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260903_0355
Revises: 20260903_0354
Create Date: 2026-09-03 09:20:00
"""

from __future__ import annotations

from alembic import op

revision = "20260903_0355"
down_revision = "20260903_0354"
branch_labels = None
depends_on = None

# Counted before any DDL so a database that still holds one gets a named refusal naming both totals,
# rather than a CHECK validation error naming whichever constraint happened to be added first.
_REFUSE_RETIRED_VALUES = """
DO $$
DECLARE
  retired_cases bigint;
  retired_decisions bigint;
BEGIN
  SELECT count(*) INTO retired_cases
    FROM public.trading_cases
   WHERE state IN ('POLICY_REJECTED', 'INTENT_EMITTED', 'ORDER_PREPARED');
  SELECT count(*) INTO retired_decisions
    FROM public.trading_candidate_gate_decisions
   WHERE status = 'RESEARCH_ONLY' OR stage IN ('capability', 'catalog', 'routing');
  IF retired_cases > 0 OR retired_decisions > 0 THEN
    RAISE EXCEPTION
      'trading_retired_values_present: trading_cases=%, trading_candidate_gate_decisions=%',
      retired_cases, retired_decisions
      USING HINT =
        'archive these rows to ~/.tracefold/backups/ and delete them before upgrading; '
        'see docs/MIGRATIONS.md';
  END IF;
END
$$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(_REFUSE_RETIRED_VALUES)

    # The narrowed CHECKs below are the single owner of each closed vocabulary.
    op.execute("DROP TRIGGER reject_retired_case_state ON public.trading_cases")
    op.execute("DROP FUNCTION public.reject_retired_trading_case_state()")
    op.execute("DROP TRIGGER trg_trading_candidate_gate_stage_hard_cut ON public.trading_candidate_gate_decisions")
    op.execute("DROP FUNCTION public.reject_retired_candidate_gate_stage()")

    # `trading_cases_capital_disposition_check` is dropped by its column.
    op.execute(
        """
        ALTER TABLE public.trading_cases
          DROP COLUMN regime,
          DROP COLUMN program_version,
          DROP COLUMN program_sha256,
          DROP COLUMN program_output,
          DROP COLUMN capital_disposition,
          DROP COLUMN capital_reason,
          DROP CONSTRAINT trading_cases_state_check,
          ADD CONSTRAINT trading_cases_state_check
            CHECK (state IN ('PENDING', 'RUNNING', 'NO_TRADE', 'SIGNAL_EMITTED', 'BLOCKED'))
        """
    )
    op.execute(
        """
        ALTER TABLE public.trading_candidate_gate_decisions
          DROP CONSTRAINT trading_candidate_gate_status_check,
          ADD CONSTRAINT trading_candidate_gate_status_check
            CHECK (status IN ('DEFERRED', 'REJECTED', 'CASE_CREATED', 'EXPIRED')),
          DROP CONSTRAINT trading_candidate_gate_stage_check,
          ADD CONSTRAINT trading_candidate_gate_stage_check
            CHECK (stage IN ('source', 'venue', 'eligibility', 'market_context', 'freeze'))
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260903_0355 deletes the six dead trading_cases columns; restore the operator's "
        "pre-0355 archive from ~/.tracefold/backups/ to read them"
    )
