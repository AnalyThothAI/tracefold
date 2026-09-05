"""Record which Workers capabilities are working, beside whether the process is alive (#553 PR-3).

Migration evidence:

- category: one nullable-free column addition with a constant default; no row, index or constraint
  changes to existing data
- why_database_must_change: `workers_runtime` is the only row Serve reads to answer "what is the
  Workers process doing", and until now it could answer only with a process-wide lifecycle state.
  That was enough while every business fault was fatal: a faulted Trading lane, an unconstructable
  push sender or an unassemblable News Program all ended as `lifecycle_state = 'failed'`, and the
  console could not tell them apart but also did not have to, because nothing was running.

  #553 PR-3 stops those three from killing the process, so the process now legitimately stays
  `running` while one capability is dead. Without a place to record which, Serve would report a
  healthy runtime beside a lane that stopped hours ago — a green status covering a real fault, which
  is precisely what the Issue forbids. `capabilities` is that place: a small object keyed by
  capability name, each value `{"state", "reason"}`, written by the one process that knows.

  It is deliberately not a second lifecycle: `ok` on `/api/status` still comes from PostgreSQL plus
  the runtime state, and a faulted capability does not switch off healthy fact APIs.
- current_source_revision: 20260904_0363
- minimum_supported_source_revision: 20260904_0363
- lock_level_and_order: one `ACCESS EXCLUSIVE` catalog update on `workers_runtime`, held for the
  duration of a catalog write; no other table is touched
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: 1 row, and not rewritten. `workers_runtime` is a singleton table, and PostgreSQL 11
  and later store a non-volatile column default in the catalog rather than rewriting the heap
- estimated_bytes: one catalog tuple; the stored default is two bytes of `jsonb`
- rewrite_or_index_build: neither
- preflight_and_maintenance_boundary: none required. Adding a defaulted column is compatible in both
  directions: a writer running the previous revision inserts and updates without naming the column
  and gets `'{}'`, and a reader on the new revision sees an empty report, which reads as "this
  runtime published nothing" rather than as a fault. `make up` stops Workers anyway
- archive_current_compatibility: fully compatible. No stored value changes and no reader of the
  existing columns is affected
- role_and_grant_impact: none; the single `tracefold` login is unchanged
- failure_state: the transaction rolls back completely and `workers_runtime` keeps its current shape
- roll_forward_or_verified_backup_restore: `downgrade` drops the column, losing only the current
  process's capability report, which the next Workers start republishes. No business fact lives here
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260905_0364
Revises: 20260904_0363
Create Date: 2026-09-05 00:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260905_0364"
down_revision = "20260904_0363"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    op.execute(
        """
        ALTER TABLE public.workers_runtime
          ADD COLUMN capabilities jsonb NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute(
        """
        ALTER TABLE public.workers_runtime
          ADD CONSTRAINT workers_runtime_capabilities_object
            CHECK (jsonb_typeof(capabilities) = 'object')
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    op.execute("ALTER TABLE public.workers_runtime DROP COLUMN capabilities")
