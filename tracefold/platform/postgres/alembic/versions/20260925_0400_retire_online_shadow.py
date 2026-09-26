"""Close pending online shadow evaluations while retaining their historical source.

Migration evidence:
- category: bounded durable-data status updates; no schema change.
- why_database_must_change: the online simulator is retired, so its pending rows
  cannot remain scheduled for a producer that no longer exists.
- current_source_revision: 20260925_0399.
- minimum_supported_source_revision: 20260925_0399.
- lock_level_and_order: row locks on pending shadow evaluations and unpublished decisions.
- statement_timeout: 60s; lock_timeout: 5s.
- estimated_rows: historical pending shadow evaluations and unpublished decisions.
- estimated_bytes: one JSONB status result per affected row; no index build.
- preflight_and_maintenance_boundary: stop Analysis writers and retain the
  verified database backup before switching images.
- archive_current_compatibility: existing simulated results and original quote
  and funding references are untouched; pending results retain their source.
- role_and_grant_impact: none.
- failure_state: transactional UPDATE rolls back.
- roll_forward_or_verified_backup_restore: restore the verified pre-cut backup
  if needed; never relabel a simulated result as an actual venue fill.
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260925_0400
Revises: 20260925_0399
"""

from __future__ import annotations

from alembic import op

revision = "20260925_0400"
down_revision = "20260925_0399"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("""
        UPDATE public.trading_case_evaluations
           SET status = 'unevaluable', reason = 'online_shadow_retired',
               result = COALESCE(result, '{}'::jsonb) || jsonb_build_object(
                   'status', 'unevaluable', 'source', source,
                   'evaluation_version', evaluation_version,
                   'reason', 'online_shadow_retired'),
               evaluated_at_ms = floor(extract(epoch FROM transaction_timestamp()) * 1000)::bigint,
               next_quote_at_ms = NULL
         WHERE source = 'shadow_simulation' AND status = 'pending'
    """)
    # Publication is a Signal state, not an online simulated execution mode.
    op.execute("""
        UPDATE public.trading_case_decisions
           SET publish_status = 'unpublished'
         WHERE publish_status = 'shadow'
    """)


def downgrade() -> None:
    raise RuntimeError("retired_shadow_forward_only: restore a verified pre-0400 archive")
