"""Bound source history lookup and distinguish requests expired before dispatch.

Migration evidence:

- category: bounded source lookup index and narrow audit CHECK replacement; existing rows stay unchanged.
- why_database_must_change: source context must filter before its LIMIT without scanning every
  trigger; a recorded request can expire during its durable start receipt.
- current_source_revision: 20260927_0407
- minimum_supported_source_revision: 20260927_0407
- lock_level_and_order: build the Trading trigger index first, then take one brief ACCESS
  EXCLUSIVE lock on trading_model_calls; normal maintenance gate stops writers.
- statement_timeout: 300s locally; lock_timeout: 5s locally.
- estimated_rows: no row rewrite or backfill; index covers non-null asset trigger rows.
- estimated_bytes: one btree index, proportional to non-null asset trigger rows.
- preflight_and_maintenance_boundary: drain Trading Analysis writers before migration.
- archive_current_compatibility: historical requested/completed/result_unknown rows remain readable.
- role_and_grant_impact: none.
- failure_state: transactional DDL rollback.
- roll_forward_or_verified_backup_restore: restore a verified pre-cut archive.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260928_0408
Revises: 20260927_0407
"""

from alembic import op

revision = "20260928_0408"
down_revision = "20260927_0407"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    op.execute(
        "CREATE INDEX trading_triggers_asset_visible_idx ON public.trading_triggers "
        "(asset_id, first_visible_at_ms DESC, trigger_id DESC) WHERE asset_id IS NOT NULL"
    )
    op.execute("ALTER TABLE public.trading_model_calls DROP CONSTRAINT trading_model_calls_status_check")
    op.execute(
        "ALTER TABLE public.trading_model_calls ADD CONSTRAINT trading_model_calls_status_check "
        "CHECK (status IN ('requested','completed','result_unknown','not_dispatched'))"
    )


def downgrade() -> None:
    raise RuntimeError("trading_not_dispatched_forward_only: restore a verified pre-0408 archive")
