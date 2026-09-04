"""Delete the lane columns no rule reads and collapse the admission key to the source (#537 PR-3).

Migration evidence:

- category: destructive hard-cut -- ten column drops across four tables, one primary key narrowed,
  one jsonb key moved from a column into the row's own `evidence`, one payload key deleted from an
  append-only ledger, and one `jsonb` array replaced by the count every reader actually rendered
- why_database_must_change: each column here is written by exactly one statement and read by none.
  `trading_cases.attempt_count` and `lease_expires_at_ms` were the claim mechanism the code's own
  comment said was not the claim mechanism -- `case_id + run_id + state IN ('PENDING','RUNNING')` on
  the terminal transition is -- and a single-process lane never reclaimed an expired lease.
  `supplemental_source_keys` has been the literal `[]` on every row ever written. `strategy_id`,
  `strategy_version` and `strategy_config_digest` restate `manifest -> 'policy_id' / 'policy_version'
  / 'policy_config_digest'`, and the manifest is the copy the lane compares before it decides a Case,
  so the columns could disagree with the decision and nothing would notice.
  `trading_candidate_gate_decisions.release_revision` is written on every upsert and never selected.
  `gate_version` and `gate_config_digest` were the other two thirds of the primary key on the promise
  that a new rulebook re-decides each source in a new row; across the whole v6 -> v8 window the ledger
  holds exactly one row per source, so the promise bought no evidence and cost every reader a
  `DISTINCT ON` plus a rule for which of two rows is *the* answer about a frame. They move into
  `evidence`, beside the numbers the rule read. `trading_trade_signals.alpha_metadata` has been
  `{"policy_rule": <rule>}` on every Signal, which is `trading_cases.policy_reason` with the policy's
  own checks beside it; the contract drops the field, and `extra="forbid"` means the stored payload
  must stop carrying it in the same change or no Signal materialises.
  `trading_execution_runtime_state.routes` published the Runtime's catalogue so the Signal lane could
  refuse a market before freezing a Case; the Runtime answers that itself by name
  (`instrument_unmapped`), so what is left is the count the `/status` projection renders.
- current_source_revision: 20260903_0359
- minimum_supported_source_revision: 20260903_0359
- lock_level_and_order: canonical migration stop, one transaction. The admission ledger first --
  backfill `evidence`, collapse any duplicate source key, then swap the primary key and drop three
  columns -- then the `trading_cases` drops, then the Signal ledger (drop the append-only trigger,
  rewrite `payload`, fire the deferred Case/Signal link, drop the column, restore the trigger), then
  the Runtime projection column swap. Every statement is ACCESS EXCLUSIVE on one small table.
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: production holds about 320 `trading_candidate_gate_decisions`, 240 `trading_cases`,
  11 `trading_trade_signals` and one `trading_execution_runtime_state` row
- estimated_bytes: `DROP COLUMN` reclaims nothing; it marks the attribute dropped. The two `UPDATE`s
  rewrite about 330 small rows. `ADD COLUMN ... DEFAULT 0` is metadata-only (PostgreSQL 11+).
- rewrite_or_index_build: `ix_trading_cases_strategy (strategy_id, created_at_ms DESC)` is dropped
  with `strategy_id`; nothing else names it and no read has since #510. The admission ledger's primary
  key index is rebuilt at one column, and its two secondary indexes (`ix_trading_candidate_gate_open`,
  `ix_trading_candidate_gate_observed`) name only surviving columns and still serve every read: the
  expiry sweep, the retention purge and the 24 h window scan. `ix_trading_trade_signals_unresolved`
  INCLUDEs `payload` but not `alpha_metadata`, so the Signal column drop leaves it alone.
- preflight_and_maintenance_boundary: canonical migration stop, so no lane turn is mid-upsert while
  the admission key changes and no Runtime is mid-projection while `routes` becomes `routes_count`.
  The duplicate collapse is inside the transaction and is the preflight: it applies exactly the
  ordering every reader already used to pick one row per source (`CASE_CREATED` first, then the most
  recent evaluation, then the digest), so the row that survives is the row the console has been
  showing. On production it deletes nothing -- there are no duplicate source keys.
- role_and_grant_impact: none; the single tracefold login is unchanged
- archive_current_compatibility: **not compatible, by design.** The dropped columns' contents go with
  them, apart from the two the `evidence` backfill preserves, so the operator's archive dump is taken
  before the upgrade. Every Case keeps its identity, manifest, state, decision, reasons, checks and
  clocks; every admission row keeps its answer, its evidence and its case link; every Signal keeps its
  identity, market, direction and clocks. A Signal payload written before this revision loses one key
  it can no longer be validated with.
- failure_state: the transaction rolls back completely and the lane keeps the old key
- roll_forward_or_verified_backup_restore: `downgrade` refuses. Restore the operator's pre-0360 dump
  into a scratch database to read a dropped column or a pre-rewrite Signal payload.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260904_0360
Revises: 20260903_0359
Create Date: 2026-09-04 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260904_0360"
down_revision = "20260903_0359"
branch_labels = None
depends_on = None

# The rulebook that decided a row, moved to where the numbers it read already live. Written before the
# key changes, so no row loses the identity of the configuration that answered it.
_BACKFILL_GATE_EVIDENCE = """
UPDATE public.trading_candidate_gate_decisions
   SET evidence = evidence || jsonb_build_object(
         'gate_version', gate_version,
         'gate_config_digest', btrim(gate_config_digest)
       )
"""

# One row per source is what the ledger already holds and what every reader already showed. This is
# that same choice, applied once and durably: `CASE_CREATED` wins over recency, because a source that
# produced a Case is re-read under the next configuration and refused as `already_consumed`, and the
# newer row would report a refusal for a frame that is linked to a live Case.
_COLLAPSE_DUPLICATE_SOURCE_KEYS = """
DELETE FROM public.trading_candidate_gate_decisions
 WHERE ctid IN (
   SELECT ctid FROM (
     SELECT ctid,
            row_number() OVER (
              PARTITION BY source_key
              ORDER BY (status = 'CASE_CREATED') DESC, last_evaluated_at_ms DESC, gate_config_digest
            ) AS rank
       FROM public.trading_candidate_gate_decisions
   ) ranked
    WHERE ranked.rank > 1
 )
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    op.execute(_BACKFILL_GATE_EVIDENCE)
    op.execute(_COLLAPSE_DUPLICATE_SOURCE_KEYS)
    op.execute(
        """
        ALTER TABLE public.trading_candidate_gate_decisions
          DROP CONSTRAINT trading_candidate_gate_decisions_pkey,
          ADD CONSTRAINT trading_candidate_gate_decisions_pkey PRIMARY KEY (source_key),
          DROP CONSTRAINT trading_candidate_gate_release_nonempty,
          DROP COLUMN gate_version,
          DROP COLUMN gate_config_digest,
          DROP COLUMN release_revision
        """
    )

    # `trading_cases_strategy_digest_check` names only a column this statement drops; dropping it by
    # name says so rather than leaving it to the cascade.
    op.execute(
        """
        ALTER TABLE public.trading_cases
          DROP CONSTRAINT trading_cases_strategy_digest_check,
          DROP COLUMN attempt_count,
          DROP COLUMN lease_expires_at_ms,
          DROP COLUMN supplemental_source_keys,
          DROP COLUMN strategy_id,
          DROP COLUMN strategy_version,
          DROP COLUMN strategy_config_digest
        """
    )

    # The Signal ledger stays append-only; this is the second statement in its history that removes a
    # stored key (`20260903_0357` was the first), and it happens with every writer stopped.
    op.execute("DROP TRIGGER trg_trading_trade_signals_append_only ON public.trading_trade_signals")
    op.execute("UPDATE public.trading_trade_signals SET payload = payload - 'alpha_metadata'")
    # `trading_trade_signals_case_link` is DEFERRABLE INITIALLY DEFERRED, so the rewrite above queues a
    # trigger event per Signal and `DROP COLUMN` would then refuse with `pending trigger events`.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("ALTER TABLE public.trading_trade_signals DROP COLUMN alpha_metadata")
    op.execute(
        """
        CREATE TRIGGER trg_trading_trade_signals_append_only
          BEFORE DELETE OR UPDATE ON public.trading_trade_signals
          FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation()
        """
    )

    # The catalogue's size, not its keys. `20260903_0357` already dropped the array's CHECK and its
    # validator function, so the column leaves nothing behind.
    op.execute(
        """
        ALTER TABLE public.trading_execution_runtime_state
          DROP COLUMN routes,
          ADD COLUMN routes_count integer NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260904_0360 deletes the lane's unread case, admission and Signal columns and narrows the "
        "admission key to the source; restore the operator's pre-0360 archive from "
        "~/.tracefold/backups/ to read them"
    )
