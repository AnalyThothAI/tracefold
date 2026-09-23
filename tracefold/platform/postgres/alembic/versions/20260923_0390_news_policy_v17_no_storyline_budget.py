"""Open the News judgment CHECK to `news_triage_policy_v17`: the per-storyline budget is gone.

Migration evidence:

- category: one constraint rewrite, additive; no table, column, index, function or row changes
- why_database_must_change: `news_verdicts_current_judgment_check` enumerates the policy versions each
  judgment origin may be written under, and the model, OI and degraded branches stop at
  `news_triage_policy_v16`. The owner withdrew the #504 D2 per-storyline budget on 2026-09-23 (reversing
  #675 §6): `decide()` no longer withholds an ordinary push as `storyline:<key>:budget` because the reader
  already received two cards on its storyline key inside an hour. Removing a withhold changes decisions,
  so `TRIAGE_POLICY_VERSION` moves to v17 and every verdict of every origin is written under it, exactly
  as #504 moved v11 to v12 to add the rule. Without this revision the deployed workers cannot persist a
  single verdict.
- current_source_revision: 20260922_0389
- minimum_supported_source_revision: 20260922_0389
- lock_level_and_order: maintenance stop; one ACCESS EXCLUSIVE constraint drop and add on
  `news_verdicts`, in one transaction, with no other object touched
- statement_timeout: 1800s set locally by the revision. ADD CONSTRAINT re-validates every `news_verdicts`
  row through the canonical-JSON sha256 predicate; the 2026-09-15 production run of the same predicate in
  `0379` over 20,453 rows exceeded the 120s the pre-0378 CHECK restatements used.
- lock_timeout: 5s set locally by the revision
- estimated_rows: `news_verdicts` under the 30-day retention, low tens of thousands
- estimated_bytes: one catalog entry; no heap rewrite, no index build
- rewrite_or_index_build: none; ADD CONSTRAINT validates existing rows in place. `ADD CONSTRAINT ... NOT
  VALID` followed by `VALIDATE CONSTRAINT` was considered because the new predicate is a strict superset
  of the old one, and rejected on two counts. `env.py` runs every pending revision in one transaction and
  the `DROP CONSTRAINT` already holds ACCESS EXCLUSIVE until it commits, so `VALIDATE`'s weaker SHARE
  UPDATE EXCLUSIVE lock buys nothing here: the scan runs under the exclusive lock either way. Stopping at
  `NOT VALID` would skip the scan, but `pg_get_constraintdef` then appends `NOT VALID`, and every later
  revision that restates this predicate from the live definition -- the pattern `0385`-`0387` and this
  revision follow -- would carry an unvalidated CHECK forward, including one that narrows it.
  `docs/MIGRATIONS.md` sanctions neither shape, so this follows `0386` exactly.
- preflight_and_maintenance_boundary: News workers stopped and the News queues drained by the canonical
  migration gate. The revision additionally refuses to run against a constraint it does not recognize:
  a definition without `news_triage_policy_v16`, or one that already names v17, raises rather than
  rewriting a predicate this revision did not read.
- archive_current_compatibility: compatible. The three branches keep every policy version they already
  admit, so every verdict written under v11-v16 keeps validating, and none is rewritten -- including the
  v12-v16 rows whose `throttled_by` is `storyline:<key>:budget`, which stay in the ledger as the history
  of the deleted rule. `throttled_by` is an unconstrained text column, so the key needs no vocabulary
  change either way.
- role_and_grant_impact: none; the single tracefold login is unchanged
- failure_state: the transaction rolls back completely and the v16 policy list stays
- roll_forward_or_verified_backup_restore: correct with a new forward revision or restore the verified
  pre-cut backup
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260923_0390
Revises: 20260922_0389
Create Date: 2026-09-23 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260923_0390"
down_revision = "20260922_0389"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '1800s'")
    # Restated in place from whatever the database actually holds, the way `0386` and `0387` restate it:
    # the predicate is one 200-line expression shared by four judgment origins, and `0387` had already
    # rewritten it in place, so a copy of any revision's source text would silently drop a clause. The
    # three occurrences are the model, OI and degraded branches; `replace` in PostgreSQL is global, so one
    # call covers all three, and the liquidation branch carries its own policy version and is untouched.
    op.execute("""
        DO $migration$
        DECLARE definition text;
        BEGIN
          SELECT pg_get_constraintdef(oid) INTO STRICT definition FROM pg_constraint
            WHERE conrelid='news_verdicts'::regclass AND conname='news_verdicts_current_judgment_check';
          IF position('news_triage_policy_v16' in definition) = 0
             OR position('news_triage_policy_v17' in definition) > 0 THEN
            RAISE EXCEPTION 'unexpected_news_judgment_constraint';
          END IF;
          definition := replace(definition,
            '''news_triage_policy_v16''::text]))',
            '''news_triage_policy_v16''::text, ''news_triage_policy_v17''::text]))');
          IF position('news_triage_policy_v17' in definition) = 0 THEN
            RAISE EXCEPTION 'news_policy_v17_not_admitted';
          END IF;
          ALTER TABLE news_verdicts DROP CONSTRAINT news_verdicts_current_judgment_check;
          EXECUTE 'ALTER TABLE news_verdicts ADD CONSTRAINT news_verdicts_current_judgment_check ' || definition;
        END $migration$;
    """)


def downgrade() -> None:
    raise RuntimeError("news_policy_v17_no_storyline_budget_forward_only: restore a verified backup")
