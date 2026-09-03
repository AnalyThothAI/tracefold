"""Make Pydantic the only validator: drop the JSON-shape CHECKs, digests and readiness gates (#520 PR-C).

Migration evidence:

- category: destructive hard cut -- twelve CHECK constraints dropped, two re-stated, nine columns
  dropped, three `payload` rewrites, one index rebuilt, four functions dropped
- why_database_must_change: every execution `payload` was checked twice, once by the contract that
  produced it and once by a CHECK that restated the same rule in SQL. The two rules cannot be kept
  identical: `trading_execution_string_array_valid` demanded the database's default collation order
  while `ExecutionObservationV1` sorted by code point, and on 2026-09-02 that single disagreement
  refused every observation in the queue from 04:04 to 09:58 (#510 PR-1 patched the collation; it did
  not remove the second rule). A per-key equality CHECK also cannot say anything the writer did not
  already say: `payload ->> 'event_id' = event_id` re-derives one INSERT's own column list, so it
  fails only when this module's own SQL is wrong, never on data. So the contract is the validator and
  the database keeps what only the database can enforce: primary keys, foreign keys, NOT NULL, the
  enumerated value sets, the identity regexes, the clock inequalities and the append-only triggers.
  Three summary columns go with the CHECKs because nothing reads them: `payload_digest` and
  `alpha_contract_sha256` have no reader at all, and `evidence_sha256` is written and never read
  back. `trading_cases.manifest_sha256` stays -- Case idempotency is still stated with it. The five
  readiness booleans are deleted here rather than in #520 PR-B so that PR needs no migration:
  `singleton_ready` and `portfolio_ready` are true whenever the process is running at all (the
  advisory lock is taken before the Runtime starts and a Nautilus node with no portfolio never
  reaches the loop), `control_plane_ready` gated entries on an input plane that is the only source of
  entry requests, `audit_ready` refused entries because the local audit copy of what Binance already
  stores was unwritable, and `day_start_ready` refused them because a baseline the Runtime can
  compute from current equity was missing. `alive`, `execution_safe`, `entries_armed`,
  `startup_reconciled`, `unexpected_exposure` and `account_flat` are the readiness facts that remain,
  so the two safety CHECKs are re-stated over exactly those.
- current_source_revision: 20260903_0356
- minimum_supported_source_revision: 20260903_0356
- lock_level_and_order: canonical migration stop with Serve, Workers and Nautilus stopped. Constraints
  first (a CHECK naming a column blocks that column's drop, and a CHECK calling a function blocks that
  function's drop), then the three append-only triggers come off so the `payload` keys can be dropped,
  then the deferred Case/Signal link trigger is fired (a deferred event blocks `DROP COLUMN`), then
  the columns, then the one index that named a dropped column is rebuilt, then the functions, then
  the append-only triggers are restored. Every statement is ACCESS EXCLUSIVE on one of four small tables, in
  one transaction.
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: production holds low thousands of `trading_execution_observations` rows, low
  hundreds of `trading_trade_signals` rows, single-digit `trading_operator_intents` rows and one
  `trading_execution_runtime_state` row. Each of the three ledgers has its `payload` rewritten once.
- estimated_bytes: the three `payload` rewrites double those tables' heap until the next vacuum;
  everything else is catalog-only.
- rewrite_or_index_build: `DROP CONSTRAINT`, `DROP COLUMN` and `DROP FUNCTION` are catalog-only.
  `ix_trading_trade_signals_unresolved` INCLUDEs `alpha_contract_sha256`, so dropping that column
  would drop the index with it; the revision states the drop and the rebuild itself rather than
  leaving the bridge read unindexed. The index covers low hundreds of rows.
- preflight_and_maintenance_boundary: ordinary canonical migration stop, no guard. Nothing here can
  lose a fact another row depends on: the dropped CHECKs admit strictly more, and the dropped columns
  have no foreign key, no index besides the one rebuilt here and no reader.
- role_and_grant_impact: none; the single tracefold login owns every table and function here
- archive_current_compatibility: **not compatible, by design.** `payload_digest`,
  `alpha_contract_sha256`, `evidence_sha256`, `confirmation_identity` and the five readiness booleans
  are deleted from their columns *and* from the stored `payload` of every historical row, and no
  forward revision brings them back, so the operator's `pg_dump` is taken before the upgrade. The
  `payload` rewrite is required rather than cosmetic: the contracts forbid unknown keys, so a stored
  payload still carrying `payload_digest` would fail to materialize on the next read. Every Signal,
  Command and Observation keeps its identity, correlation, clocks, summary and every other payload
  key, and its `seq` never moves.
- failure_state: the transaction rolls back completely. Either a CHECK this revision failed to notice
  still names a dropped column and nothing was touched, or nothing was touched.
- roll_forward_or_verified_backup_restore: `downgrade` refuses. Restore the operator's pre-0357 dump
  into a scratch database to read a digest or a readiness boolean.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260903_0357
Revises: 20260903_0356
Create Date: 2026-09-03 12:20:00
"""

from __future__ import annotations

from alembic import op

revision = "20260903_0357"
down_revision = "20260903_0356"
branch_labels = None
depends_on = None

# Every CHECK that restated a contract rule in SQL, by the table it sat on. The three `payload`
# CHECKs compared jsonb key by key against the row's own columns; the three `*_valid` CHECKs called a
# function that re-derived the contract's bounds, ordering and key shapes; the four digest and
# confirmation CHECKs only constrained columns this revision deletes.
_DROPPED_CHECKS: tuple[tuple[str, str], ...] = (
    ("trading_execution_observations", "trading_execution_observation_payload_check"),
    ("trading_execution_observations", "trading_execution_observation_native_refs_check"),
    ("trading_execution_observations", "trading_execution_observation_summary_check"),
    ("trading_execution_observations", "trading_execution_observation_digest_check"),
    ("trading_trade_signals", "trading_trade_signal_payload_check"),
    ("trading_trade_signals", "trading_trade_signal_metadata_check"),
    ("trading_trade_signals", "trading_trade_signal_alpha_sha_check"),
    ("trading_trade_signals", "trading_trade_signal_evidence_sha_check"),
    ("trading_operator_intents", "trading_operator_intent_payload_check"),
    ("trading_operator_intents", "trading_operator_intent_confirmation_check"),
    ("trading_execution_runtime_state", "trading_execution_runtime_account_snapshot_check"),
    ("trading_execution_runtime_state", "trading_execution_runtime_routes_check"),
    # Re-stated below over the readiness facts that survive.
    ("trading_execution_runtime_state", "trading_execution_runtime_safe_check"),
    ("trading_execution_runtime_state", "trading_execution_runtime_armed_check"),
)

# The three append-only ledgers, each with the trigger the payload rewrite has to lift and the
# stored keys it stops carrying alongside the columns of the same name. The Runtime projection is
# absent because it is mutable by design and holds no payload.
_APPEND_ONLY_LEDGERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "trading_execution_observations",
        "trg_trading_execution_observations_append_only",
        ("payload_digest",),
    ),
    (
        "trading_trade_signals",
        "trg_trading_trade_signals_append_only",
        ("alpha_contract_sha256", "evidence_sha256"),
    ),
    (
        "trading_operator_intents",
        "trg_trading_operator_intents_append_only",
        ("confirmation_identity",),
    ),
)

_DROPPED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("trading_execution_observations", "payload_digest"),
    ("trading_trade_signals", "alpha_contract_sha256"),
    ("trading_trade_signals", "evidence_sha256"),
    ("trading_operator_intents", "confirmation_identity"),
    ("trading_execution_runtime_state", "singleton_ready"),
    ("trading_execution_runtime_state", "portfolio_ready"),
    ("trading_execution_runtime_state", "control_plane_ready"),
    ("trading_execution_runtime_state", "audit_ready"),
    ("trading_execution_runtime_state", "day_start_ready"),
)

# Nothing but the dropped CHECKs ever called these; no trigger, default or generated column does.
_DROPPED_FUNCTIONS: tuple[str, ...] = (
    "trading_execution_metadata_valid(jsonb)",
    "trading_execution_string_array_valid(jsonb)",
    "trading_execution_market_key_array_valid(jsonb)",
    "trading_jsonb_object_size(jsonb)",
)

_UNRESOLVED_SIGNAL_INDEX = """
CREATE UNIQUE INDEX ix_trading_trade_signals_unresolved
    ON public.trading_trade_signals USING btree (seq)
    INCLUDE (signal_id, expires_at_ns, payload)
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    for table, constraint in _DROPPED_CHECKS:
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {constraint}")

    # `execution_safe` and `entries_armed` are still refusals the database can state cheaply; they now
    # name only the readiness facts that survive this revision.
    op.execute(
        """
        ALTER TABLE public.trading_execution_runtime_state
          ADD CONSTRAINT trading_execution_runtime_safe_check
            CHECK (NOT execution_safe OR (alive AND startup_reconciled AND NOT unexpected_exposure)),
          ADD CONSTRAINT trading_execution_runtime_armed_check
            CHECK (NOT entries_armed OR execution_safe)
        """
    )

    # The ledgers stay append-only; this rewrite is the one statement in their history that removes a
    # stored key, and it happens with every writer stopped.
    for table, trigger, keys in _APPEND_ONLY_LEDGERS:
        op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
        removals = " ".join(f"- '{key}'" for key in keys)
        op.execute(f"UPDATE public.{table} SET payload = payload {removals}")  # noqa: S608 -- fixed literals

    # `trading_trade_signals_case_link` is DEFERRABLE INITIALLY DEFERRED, so the rewrite above queues
    # a trigger event per Signal and `DROP COLUMN` then refuses with `pending trigger events`. Firing
    # them here is also the honest place to learn that the Case/Signal link still holds.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")

    # `ix_trading_trade_signals_unresolved` INCLUDEs `alpha_contract_sha256`, so the column drop would
    # take the index with it. Stated here rather than left to `DROP COLUMN`'s cascade.
    op.execute("DROP INDEX ix_trading_trade_signals_unresolved")
    for table, column in _DROPPED_COLUMNS:
        op.execute(f"ALTER TABLE public.{table} DROP COLUMN {column}")
    op.execute(_UNRESOLVED_SIGNAL_INDEX)

    for function in _DROPPED_FUNCTIONS:
        op.execute(f"DROP FUNCTION public.{function}")

    for table, trigger, _ in _APPEND_ONLY_LEDGERS:
        op.execute(
            f"""
            CREATE TRIGGER {trigger}
              BEFORE DELETE OR UPDATE ON public.{table}
              FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation()
            """
        )


def downgrade() -> None:
    raise RuntimeError(
        "20260903_0357 deletes the unread execution digests, the confirmation identity and the five "
        "readiness booleans from their columns and from every stored payload; restore the operator's "
        "pre-0357 archive from ~/.tracefold/backups/ to read them"
    )
