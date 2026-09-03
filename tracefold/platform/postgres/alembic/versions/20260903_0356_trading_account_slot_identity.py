"""Make `account_slot` the execution identity and drop the profile/activation fence (#520 PR-A).

Migration evidence:

- category: destructive hard cut -- two column renames with a value and payload backfill, one primary
  key change with a row fold, three column drops, two table drops
- why_database_must_change: `trading_execution_profile_activations` was the durable half of an
  identity fence. It named a `profile_id`, froze `mode + runtime_release + config_sha256` against it,
  and every other execution row keyed off that name: observations carried `runtime_profile_id`,
  Commands carried `target_profile_id`, the current control projection was keyed by it, and the
  Runtime projection held a foreign key to it. Since #510 PR-3 ownership is rebuilt from
  deterministic client order ids plus durable `order` observations, so the fence's only remaining
  effect was to refuse to start: on 2026-09-03 a `config_sha256` change refused 58 consecutive
  Nautilus starts with `oi_runtime_profile_identity_changed`, and clearing it needed a new
  `profile_id`, a flat account and a fresh authenticated `/resume`. One account slot is executed by
  one Runtime at a time -- that is what the `pg_try_advisory_lock` already enforces -- so
  `account_slot` is the identity, and `runtime_release / config_sha256 / image_digest /
  credential_fingerprint` stay on `trading_execution_runtime_state` as information about what is
  running rather than as a gate on whether it may run. `activated_after_signal_seq` /
  `activated_after_command_seq` go with it: a pending intent is one inside its own TTL, which the
  contract already carries and the read now states directly. `trading_decision_runtime` is the same
  shape one plane over -- a single-row heartbeat whose absence stopped the Signal lane outright, with
  `tracefold trading status` and `/api/trading/status` reading it as a state machine; the newest
  `trading_cases.created_at_ms` is the durable fact those readers actually want.
- current_source_revision: 20260903_0355
- minimum_supported_source_revision: 20260903_0355
- lock_level_and_order: canonical migration stop with Serve, Workers and Nautilus stopped. One guard
  first (see preflight), then the two append-only triggers are dropped so the backfill can run, then
  observations and Commands are backfilled and renamed, then the control projection is folded and
  re-keyed, then the Runtime projection loses three columns, then the two tables are dropped, then the
  triggers are restored. Every statement is ACCESS EXCLUSIVE on one of five small tables, in one
  transaction.
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: production holds 2 `trading_execution_profile_activations` rows, 2
  `trading_execution_runtime_control_state` rows, 1 `trading_execution_runtime_state` row, 1
  `trading_decision_runtime` row, and low thousands of `trading_execution_observations` rows after
  #510 PR-1 took the steady reconciliation heartbeat out of the ledger. The observation backfill
  rewrites every row's `payload` once.
- estimated_bytes: the observation `payload` rewrite doubles that table's heap until the next vacuum;
  everything else is catalog-only or single-digit rows.
- rewrite_or_index_build: `ALTER TABLE ... RENAME COLUMN` is catalog-only and carries every index,
  CHECK and foreign key that names the column with it, so the five observation indexes, the two
  Command indexes and the composite foreign key survive the rename unchanged. Only the two `payload`
  CHECKs are re-stated, because their key name is a string literal the rename cannot see. `DROP
  COLUMN` is catalog-only. The control projection's primary key is rebuilt over at most one row per
  account slot.
- preflight_and_maintenance_boundary: **the guard is inside the transaction.** Folding several
  `profile_id`s onto one `account_slot` can only lose information in one place: two observations that
  disposed of the same Signal or the same Command under different profiles would collide on
  `ux_trading_execution_signal_disposition` / `ux_trading_execution_control_disposition` after the
  rename. The guard counts those collisions before any DDL and raises naming both totals, so an
  operator archives and deletes the duplicates rather than reading a unique-violation traceback. A
  Command's disposition cannot collide (the composite foreign key already ties an observation's
  profile to its Command's target), and a Signal's cannot in the production ledger, where the second
  profile's activation waterline sits above every Signal the first profile disposed of.
- role_and_grant_impact: none; the single tracefold login owns every table here
- archive_current_compatibility: **not compatible, by design.** The activation ledger's eight columns
  and the decision heartbeat's four are deleted with their tables and no forward revision brings them
  back, so the operator's `pg_dump` is taken before the upgrade. Every Signal, Command and Observation
  keeps its identity, correlation, clocks, summary and payload; only the name of the identity column
  and the matching payload key change. Client order ids move namespace with this deploy
  (`tracefold:{profile_id}:{mode}` becomes `tracefold:{account_slot}:{mode}`), so the account must be
  flat when it is applied: an order opened under the old namespace can no longer be reclaimed.
- failure_state: the transaction rolls back completely. Either the guard raised and nothing was
  touched, or a rename found a dependency this revision failed to notice and nothing was touched.
- roll_forward_or_verified_backup_restore: `downgrade` refuses. Restore the operator's pre-0356 dump
  into a scratch database to read an activation waterline or the decision heartbeat.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260903_0356
Revises: 20260903_0355
Create Date: 2026-09-03 11:10:00
"""

from __future__ import annotations

from alembic import op

revision = "20260903_0356"
down_revision = "20260903_0355"
branch_labels = None
depends_on = None

_IDENTITY_REGEX = "'^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$'"

# Counted before any DDL. Two profiles that folded onto one account slot may each hold a disposition
# observation for the same Signal or Command; after the rename those two rows are the same key.
_REFUSE_FOLDED_DISPOSITION_COLLISIONS = """
DO $$
DECLARE
  signal_collisions bigint;
  command_collisions bigint;
BEGIN
  SELECT count(*) INTO signal_collisions FROM (
    SELECT 1
      FROM public.trading_execution_observations observation
      JOIN public.trading_execution_profile_activations activation
        ON activation.runtime_profile_id = observation.runtime_profile_id
     WHERE observation.normalized_kind = 'signal_disposition'
     GROUP BY activation.account_slot, observation.execution_strategy, observation.signal_id
    HAVING count(*) > 1
  ) folded;
  SELECT count(*) INTO command_collisions FROM (
    SELECT 1
      FROM public.trading_execution_observations observation
      JOIN public.trading_execution_profile_activations activation
        ON activation.runtime_profile_id = observation.runtime_profile_id
     WHERE observation.normalized_kind = 'control_disposition'
     GROUP BY activation.account_slot, observation.execution_strategy, observation.command_id
    HAVING count(*) > 1
  ) folded;
  IF signal_collisions > 0 OR command_collisions > 0 THEN
    RAISE EXCEPTION
      'trading_folded_disposition_collisions: signals=%, commands=%',
      signal_collisions, command_collisions
      USING HINT =
        'two execution profiles on one account slot disposed of the same Signal or Command; '
        'archive these observations to ~/.tracefold/backups/ and delete the older row of each '
        'pair before upgrading; see docs/MIGRATIONS.md';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM public.trading_execution_observations observation
      LEFT JOIN public.trading_execution_profile_activations activation
        ON activation.runtime_profile_id = observation.runtime_profile_id
     WHERE activation.runtime_profile_id IS NULL
  ) OR EXISTS (
    SELECT 1
      FROM public.trading_operator_intents command
      LEFT JOIN public.trading_execution_profile_activations activation
        ON activation.runtime_profile_id = command.target_profile_id
     WHERE activation.runtime_profile_id IS NULL
  ) THEN
    RAISE EXCEPTION 'trading_execution_identity_unmapped'
      USING HINT =
        'an observation or Command names a profile with no activation row, so this revision cannot '
        'derive its account slot; see docs/MIGRATIONS.md';
  END IF;
END
$$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(_REFUSE_FOLDED_DISPOSITION_COLLISIONS)

    # The ledgers stay append-only; the backfill is the one statement in their history that rewrites a
    # stored fact's identity column, and it happens with every writer stopped.
    op.execute("DROP TRIGGER trg_trading_execution_observations_append_only ON public.trading_execution_observations")
    op.execute("DROP TRIGGER trg_trading_operator_intents_append_only ON public.trading_operator_intents")

    # The composite foreign key spans both tables, so it comes off before either side is backfilled.
    op.execute(
        "ALTER TABLE public.trading_execution_observations DROP CONSTRAINT trading_execution_observation_command_fk"
    )

    # Each payload CHECK names its identity key as a string literal, which `RENAME COLUMN` cannot see
    # and the backfill immediately violates, so both come off first and are re-stated after the rename.
    op.execute(
        "ALTER TABLE public.trading_execution_observations DROP CONSTRAINT trading_execution_observation_payload_check"
    )
    op.execute("ALTER TABLE public.trading_operator_intents DROP CONSTRAINT trading_operator_intent_payload_check")

    op.execute(
        """
        UPDATE public.trading_execution_observations observation
           SET runtime_profile_id = activation.account_slot,
               payload = (observation.payload - 'runtime_profile_id')
                         || jsonb_build_object('account_slot', activation.account_slot)
          FROM public.trading_execution_profile_activations activation
         WHERE activation.runtime_profile_id = observation.runtime_profile_id
        """
    )
    op.execute(
        """
        UPDATE public.trading_operator_intents command
           SET target_profile_id = activation.account_slot,
               payload = (command.payload - 'target_profile_id')
                         || jsonb_build_object('account_slot', activation.account_slot)
          FROM public.trading_execution_profile_activations activation
         WHERE activation.runtime_profile_id = command.target_profile_id
        """
    )

    # `RENAME COLUMN` carries every index, CHECK, unique constraint and foreign key that names the
    # column, so the five observation indexes and the two Command indexes survive it unchanged.
    op.execute(
        """
        ALTER TABLE public.trading_execution_observations
          RENAME COLUMN runtime_profile_id TO account_slot
        """
    )
    op.execute(
        """
        ALTER TABLE public.trading_execution_observations
          ADD CONSTRAINT trading_execution_observation_payload_check CHECK (
            COALESCE(
              jsonb_typeof(payload) = 'object'
              AND public.trading_jsonb_object_size(payload) = 13
              AND payload ->> 'observation_version' = 'execution_observation_v1'
              AND payload ->> 'event_id' = event_id
              AND payload ->> 'account_slot' = account_slot
              AND payload ->> 'runtime_release' = runtime_release
              AND payload ->> 'execution_strategy' = execution_strategy
              AND NOT (payload ->> 'signal_id' IS DISTINCT FROM signal_id)
              AND NOT (payload ->> 'command_id' IS DISTINCT FROM command_id)
              AND payload ->> 'normalized_kind' = normalized_kind
              AND (payload ->> 'occurred_at_ns')::bigint = occurred_at_ns
              AND (payload ->> 'observed_at_ns')::bigint = observed_at_ns
              AND payload -> 'native_identity_references' = native_identity_references
              AND payload -> 'summary' = summary
              AND payload ->> 'payload_digest' = payload_digest,
              false
            )
          ),
          DROP CONSTRAINT trading_execution_observation_profile_check,
          ADD CONSTRAINT trading_execution_observation_slot_check
            CHECK (account_slot ~ """
        + _IDENTITY_REGEX
        + """)
        """
    )
    op.execute("ALTER INDEX ix_trading_execution_observations_runtime RENAME TO ix_trading_execution_observations_slot")
    op.execute(
        "ALTER TABLE public.trading_execution_observations RENAME CONSTRAINT "
        "trading_execution_observations_runtime_profile_id_not_null TO "
        "trading_execution_observations_account_slot_not_null"
    )

    op.execute("ALTER TABLE public.trading_operator_intents RENAME COLUMN target_profile_id TO account_slot")
    op.execute(
        """
        ALTER TABLE public.trading_operator_intents
          ADD CONSTRAINT trading_operator_intent_payload_check CHECK (
            COALESCE(
              jsonb_typeof(payload) = 'object'
              AND public.trading_jsonb_object_size(payload) = 13
              AND payload ->> 'intent_version' = 'operator_intent_v1'
              AND payload ->> 'command_id' = command_id
              AND payload ->> 'account_slot' = account_slot
              AND payload ->> 'action' = action
              AND payload ->> 'scope' = scope
              AND payload ->> 'reason' = reason
              AND payload ->> 'operator_identity' = operator_identity
              AND payload ->> 'authentication_identity' = authentication_identity
              AND (payload ->> 'requested_at_ns')::bigint = requested_at_ns
              AND (payload ->> 'expires_at_ns')::bigint = expires_at_ns
              AND NOT (payload ->> 'confirmation_identity' IS DISTINCT FROM confirmation_identity)
              AND NOT (payload ->> 'market_key' IS DISTINCT FROM market_key)
              AND NOT (payload ->> 'direction' IS DISTINCT FROM direction),
              false
            )
          ),
          DROP CONSTRAINT trading_operator_intent_profile_check,
          ADD CONSTRAINT trading_operator_intent_slot_check
            CHECK (account_slot ~ """
        + _IDENTITY_REGEX
        + """)
        """
    )
    op.execute(
        "ALTER TABLE public.trading_operator_intents "
        "RENAME CONSTRAINT trading_operator_intent_profile_unique TO trading_operator_intent_slot_unique"
    )
    op.execute("ALTER INDEX ix_trading_operator_intents_unresolved RENAME TO ix_trading_operator_intents_pending")
    op.execute(
        "ALTER TABLE public.trading_operator_intents RENAME CONSTRAINT "
        "trading_operator_intents_target_profile_id_not_null TO "
        "trading_operator_intents_account_slot_not_null"
    )

    op.execute(
        """
        ALTER TABLE public.trading_execution_observations
          ADD CONSTRAINT trading_execution_observation_command_fk
            FOREIGN KEY (command_id, account_slot)
            REFERENCES public.trading_operator_intents(command_id, account_slot) ON DELETE RESTRICT
        """
    )

    # Control is a property of the account slot from here on. Several profiles fold onto one row: the
    # newest command wins the pause flag, and `emergency_halted` is sticky across the whole fold
    # because a halt is a refusal that no later profile ever revoked.
    op.execute(
        "ALTER TABLE public.trading_execution_runtime_control_state "
        "DROP CONSTRAINT trading_execution_runtime_control_state_runtime_profile_id_fkey"
    )
    op.execute("ALTER TABLE public.trading_execution_runtime_control_state ADD COLUMN account_slot text")
    op.execute(
        """
        UPDATE public.trading_execution_runtime_control_state control
           SET account_slot = activation.account_slot
          FROM public.trading_execution_profile_activations activation
         WHERE activation.runtime_profile_id = control.runtime_profile_id
        """
    )
    op.execute(
        """
        UPDATE public.trading_execution_runtime_control_state control
           SET emergency_halted = folded.emergency_halted,
               entries_paused = control.entries_paused OR folded.emergency_halted,
               updated_at_ns = folded.updated_at_ns
          FROM (
            SELECT account_slot,
                   bool_or(emergency_halted) AS emergency_halted,
                   max(updated_at_ns) AS updated_at_ns
              FROM public.trading_execution_runtime_control_state
             GROUP BY account_slot
          ) folded
         WHERE folded.account_slot = control.account_slot
        """
    )
    op.execute(
        """
        DELETE FROM public.trading_execution_runtime_control_state control
         USING public.trading_execution_runtime_control_state other
         WHERE control.account_slot = other.account_slot
           AND (control.last_command_seq, control.runtime_profile_id)
             < (other.last_command_seq, other.runtime_profile_id)
        """
    )
    op.execute(
        """
        ALTER TABLE public.trading_execution_runtime_control_state
          DROP CONSTRAINT trading_execution_runtime_control_state_pkey,
          DROP COLUMN runtime_profile_id,
          ALTER COLUMN account_slot SET NOT NULL,
          ADD CONSTRAINT trading_execution_runtime_control_state_pkey PRIMARY KEY (account_slot),
          ADD CONSTRAINT trading_execution_runtime_control_slot_check
            CHECK (account_slot ~ """
        + _IDENTITY_REGEX
        + """)
        """
    )

    # The Runtime projection keeps `runtime_release / config_sha256 / image_digest /
    # credential_fingerprint` as information about what is running. `credential_ready` and
    # `activation_ready` are gone: a Runtime with no credentials fails to start, and there is no
    # activation left to be current. Dropping either column drops the safety CHECK that names it, so
    # the CHECK is re-stated over the gates that survive.
    op.execute(
        """
        ALTER TABLE public.trading_execution_runtime_state
          DROP COLUMN runtime_profile_id,
          DROP COLUMN credential_ready,
          DROP COLUMN activation_ready,
          ADD CONSTRAINT trading_execution_runtime_safe_check
            CHECK (
              NOT execution_safe OR (
                alive
                AND singleton_ready
                AND startup_reconciled
                AND portfolio_ready
                AND NOT unexpected_exposure
              )
            )
        """
    )

    op.execute("DROP TABLE public.trading_execution_profile_activations")
    op.execute("DROP TABLE public.trading_decision_runtime")

    op.execute(
        """
        CREATE TRIGGER trg_trading_execution_observations_append_only
          BEFORE DELETE OR UPDATE ON public.trading_execution_observations
          FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_trading_operator_intents_append_only
          BEFORE DELETE OR UPDATE ON public.trading_operator_intents
          FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260903_0356 deletes the execution profile activation ledger and the decision heartbeat; "
        "restore the operator's pre-0356 archive from ~/.tracefold/backups/ to read them"
    )
