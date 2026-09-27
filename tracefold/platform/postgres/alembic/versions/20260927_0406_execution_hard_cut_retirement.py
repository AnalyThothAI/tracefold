"""Name the one-time retirement of pre-cut execution Signals (#719).

Migration evidence:
- category: Trading execution hard-cut constraint change; no data rewrite.
- why_database_must_change: published Signals remain Case evidence but must never become
  executable again when old Plan/disposition rows are removed.
- current_source_revision: 20260927_0405
- minimum_supported_source_revision: 20260927_0405
- lock_level_and_order: brief ACCESS EXCLUSIVE on trading_signal_retirements.
- statement_timeout: 30s; lock_timeout: 5s.
- estimated_rows/bytes: no rows changed or copied.
- preflight_and_maintenance_boundary: stop old writers for schema/image switch.
- archive_current_compatibility: existing retirement reasons remain valid.
- role_and_grant_impact: none.
- failure_state: transactional rollback leaves the prior constraint.
- roll_forward_or_verified_backup_restore: forward only.
- validation_environment: disposable PostgreSQL, never a live account.
"""

from alembic import op

revision = "20260927_0406"
down_revision = "20260927_0405"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("ALTER TABLE public.trading_signal_retirements DROP CONSTRAINT trading_signal_retirements_reason_check")
    op.execute("""
        ALTER TABLE public.trading_signal_retirements
          ADD CONSTRAINT trading_signal_retirements_reason_check
          CHECK (reason IN ('v2_cutover', 'connection_cutover', 'execution_hard_cut'))
    """)


def downgrade() -> None:
    raise RuntimeError("execution_hard_cut_retirement_forward_only")
