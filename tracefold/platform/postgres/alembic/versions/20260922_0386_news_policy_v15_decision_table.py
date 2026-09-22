"""Open the News judgment CHECK to `news_triage_policy_v15` (#675 PR-1).

Migration evidence:

- category: one constraint rewrite, additive; no table, column, index or function changes
- why_database_must_change: `news_verdicts_current_judgment_check` enumerates the policy versions each
  judgment origin may be written under, and the model, OI and degraded branches stop at
  `news_triage_policy_v14`. `decide()` gains the #675 §3 decision table -- three ordered rows that read
  the taxonomy axes, `source_authority`, the count of independent member texts and the told ledger, and
  downgrade an already-resolved realtime push to `drop` under its own rule name -- so
  `TRIAGE_POLICY_VERSION` moves to v15 and every verdict of every origin is written under it. Without
  this revision the deployed workers cannot persist a single verdict.
- current_source_revision: 20260920_0385
- minimum_supported_source_revision: 20260920_0385
- lock_level_and_order: maintenance stop; one ACCESS EXCLUSIVE constraint drop and add, in one
  transaction, with no other object touched
- statement_timeout: 1800s set locally by the revision. ADD CONSTRAINT re-validates every `news_verdicts`
  row through the canonical-JSON sha256 predicate; the 2026-09-15 production run of the same predicate in
  `0379` over 20,453 rows exceeded the 120s the pre-0378 CHECK restatements used.
- lock_timeout: 5s set locally by the revision
- estimated_rows: `news_verdicts` under the 30-day retention, low tens of thousands
- estimated_bytes: one catalog entry; no heap rewrite, no index build
- rewrite_or_index_build: none; ADD CONSTRAINT validates existing rows in place
- preflight_and_maintenance_boundary: News workers stopped and the News queues drained by the canonical
  migration gate. The revision additionally refuses to run against a constraint it does not recognize:
  a definition without `news_triage_policy_v14`, or one that already names v15, raises rather than
  rewriting a predicate this revision did not read.
- archive_current_compatibility: compatible. The three branches keep every policy version they already
  admit, so every verdict written under v11-v14 keeps validating, and none is rewritten. The new row
  names (`price_report_without_basis`, `conflict_claim_uncorroborated`, `conflict_running_storyline`)
  need no constraint change: `override_rule` is an unconstrained text column and the decision predicate
  `news_current_decision_valid` checks its shape, not its vocabulary.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the v14 policy list stays
- roll_forward_or_verified_backup_restore: correct with a new forward revision or restore the verified
  pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260922_0386
Revises: 20260920_0385
Create Date: 2026-09-22 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260922_0386"
down_revision = "20260920_0385"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '1800s'")
    # Restated in place from whatever the database actually holds, the way `0385` restates it: the
    # predicate is one 200-line expression shared by four judgment origins, and retyping it to change
    # three list literals is how a branch silently loses a clause. The three occurrences are the model,
    # OI and degraded branches; `replace` in PostgreSQL is global, so one call covers all three, and the
    # liquidation branch carries its own policy version and is untouched.
    op.execute("""
        DO $migration$
        DECLARE definition text;
        BEGIN
          SELECT pg_get_constraintdef(oid) INTO STRICT definition FROM pg_constraint
            WHERE conrelid='news_verdicts'::regclass AND conname='news_verdicts_current_judgment_check';
          IF position('news_triage_policy_v14' in definition) = 0
             OR position('news_triage_policy_v15' in definition) > 0 THEN
            RAISE EXCEPTION 'unexpected_news_judgment_constraint';
          END IF;
          definition := replace(definition,
            '''news_triage_policy_v14''::text]))',
            '''news_triage_policy_v14''::text, ''news_triage_policy_v15''::text]))');
          IF position('news_triage_policy_v15' in definition) = 0 THEN
            RAISE EXCEPTION 'news_policy_v15_not_admitted';
          END IF;
          ALTER TABLE news_verdicts DROP CONSTRAINT news_verdicts_current_judgment_check;
          EXECUTE 'ALTER TABLE news_verdicts ADD CONSTRAINT news_verdicts_current_judgment_check ' || definition;
        END $migration$;
    """)


def downgrade() -> None:
    raise RuntimeError("news_policy_v15_decision_table_forward_only: restore a verified backup")
