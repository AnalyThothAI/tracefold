"""Delete the Runtime identity columns nothing reads and the release string on every fact (#537 PR-4).

Migration evidence:

- category: destructive hard-cut -- six column drops on the Runtime projection, one column drop plus
  one payload key removal on the append-only observation ledger, and the seven CHECK constraints that
  only ever constrained those columns
- why_database_must_change: `trading_execution_runtime_state` carried what was running beside what it
  was doing. `runtime_release` is a build-time literal, `config_sha256` a digest of the whole
  configuration, `runtime_revision` and `image_digest` the deployment's own identity and
  `credential_fingerprint` a hash of the API key -- five values written on the insert and rewritten on
  every 500 ms heartbeat, read only into the `/status` JSON, where no page and no operator command
  ever named one. Since #520 removed the activation fence none of them decides anything: the
  account-slot advisory lock is the single-writer proof and `runtime_id` is the generation fence.
  `lifecycle_state` restated `alive` with a five-value vocabulary whose `failed` member no writer has
  ever produced, and its one remaining CHECK (`NOT alive OR lifecycle_state IN (...)`) could only fail
  if the projection contradicted itself. `trading_execution_observations.runtime_release` is the same
  literal again, stored per row in a column *and* inside `payload`; `account_slot` and
  `execution_strategy` are the two identities every read actually correlates on, and both stay. The
  contract forbids extra keys, so the stored payload has to stop carrying it in this same change or
  no observation materialises -- including the day-start equity fact the Runtime reads back before it
  will size an entry.
- current_source_revision: 20260904_0360
- minimum_supported_source_revision: 20260904_0360
- lock_level_and_order: canonical migration stop, one transaction. The Runtime projection first (one
  `ALTER TABLE`: seven constraint drops and six column drops on a one-row table), then the observation
  ledger (drop the append-only trigger, rewrite `payload`, fire any deferred constraint, drop the
  column and its CHECK, restore the trigger). Both are ACCESS EXCLUSIVE on one table at a time.
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: production holds one `trading_execution_runtime_state` row and low tens of
  thousands of `trading_execution_observations` rows
- estimated_bytes: `DROP COLUMN` reclaims nothing; it marks the attribute dropped. The one `UPDATE`
  rewrites every observation row once, which is the ledger's own size again until the next vacuum.
- rewrite_or_index_build: no index names any dropped column. `ix_trading_execution_observations_slot`
  is `(account_slot, seq)`, `ux_trading_execution_signal_disposition` and
  `ux_trading_execution_control_disposition` are `(account_slot, execution_strategy, signal_id |
  command_id)`, and the two recovery indexes are `(account_slot, signal_id | command_id, seq DESC)`.
  The payload rewrite makes every observation row a new tuple, so those indexes take new entries for
  rows they already held; none is rebuilt.
- preflight_and_maintenance_boundary: canonical migration stop with the Runtime container down, so no
  generation is mid-projection while its row loses six columns and no audit flush is mid-append while
  the ledger's payloads are rewritten. The account must be flat, as it is for every Runtime cutover:
  a Runtime built from this revision derives its Nautilus instance id from `account_slot:mode` rather
  than from the configuration digest, and derives protection client order ids from the replacement
  generation alone, so a stop resting at the venue under an id an older build chose is not this
  build's and would be refused as unowned exposure.
- role_and_grant_impact: none; the single tracefold login is unchanged
- archive_current_compatibility: **not compatible, by design.** The dropped columns' contents go with
  them and the payload rewrite is in place. Every observation keeps its identity, account slot,
  strategy, correlation, kind, clocks, native references and summary; the Runtime projection keeps
  its slot, mode, generation, readiness, counts, account snapshot and clocks. An observation payload
  written before this revision loses one key it can no longer be validated with.
- failure_state: the transaction rolls back completely and the projection keeps the old columns
- roll_forward_or_verified_backup_restore: `downgrade` refuses. Restore the operator's pre-0361 dump
  into a scratch database to read a dropped column or a pre-rewrite observation payload.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260904_0361
Revises: 20260904_0360
Create Date: 2026-09-04 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260904_0361"
down_revision = "20260904_0360"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    # `trading_execution_runtime_alive_check` names `lifecycle_state` beside `alive`; dropping it by
    # name says so rather than leaving it to the column drop's cascade. `alive` keeps no CHECK of its
    # own because there is nothing left for it to contradict.
    op.execute(
        """
        ALTER TABLE public.trading_execution_runtime_state
          DROP CONSTRAINT trading_execution_runtime_release_check,
          DROP CONSTRAINT trading_execution_runtime_config_check,
          DROP CONSTRAINT trading_execution_runtime_revision_check,
          DROP CONSTRAINT trading_execution_runtime_image_check,
          DROP CONSTRAINT trading_execution_runtime_credential_check,
          DROP CONSTRAINT trading_execution_runtime_lifecycle_check,
          DROP CONSTRAINT trading_execution_runtime_alive_check,
          DROP COLUMN runtime_release,
          DROP COLUMN config_sha256,
          DROP COLUMN runtime_revision,
          DROP COLUMN image_digest,
          DROP COLUMN credential_fingerprint,
          DROP COLUMN lifecycle_state
        """
    )

    # The ledger stays append-only; this is the third statement in its history that removes a stored
    # key (`20260903_0357` and `20260904_0360` were the first two), and it happens with every writer
    # stopped.
    op.execute("DROP TRIGGER trg_trading_execution_observations_append_only ON public.trading_execution_observations")
    op.execute("UPDATE public.trading_execution_observations SET payload = payload - 'runtime_release'")
    # Any deferred constraint the rewrite queued fires here rather than refusing `DROP COLUMN` with
    # `pending trigger events`, which is also the honest place to learn the ledger still holds.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute(
        """
        ALTER TABLE public.trading_execution_observations
          DROP CONSTRAINT trading_execution_observation_release_check,
          DROP COLUMN runtime_release
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_trading_execution_observations_append_only
          BEFORE DELETE OR UPDATE ON public.trading_execution_observations
          FOR EACH ROW EXECUTE FUNCTION public.reject_trading_execution_stream_mutation()
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260904_0361 deletes the Runtime projection's identity columns and the release string on "
        "every execution observation; restore the operator's pre-0361 archive from "
        "~/.tracefold/backups/ to read them"
    )
