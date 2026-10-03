"""Preserve current execution responsibilities and add admission/fault evidence.

Migration evidence:
- category: additive current-ledger evidence; no historical fact reconstruction.
- why_database_must_change: pending margin, exact sends and unresolved risk must survive restart.
- current_source_revision: 20261002_0429
- minimum_supported_source_revision: 20261002_0429
- lock_level_and_order: ACCESS EXCLUSIVE accounts, entries, orders; Trading writers stopped.
- statement_timeout: 600s; lock_timeout: 5s.
- estimated_rows: existing rows retained, nullable documents and zero-default fault map.
- estimated_bytes: small JSON per accepted entry/order and unresolved responsibility.
- rewrite_or_index_build: additive columns only; no new index or table.
- preflight_and_maintenance_boundary: verified backup, stop-write migration window.
- archive_current_compatibility: identity, controls, frozen cases, orders and fills retained.
- role_and_grant_impact: none.
- failure_state: transactional DDL rollback leaves 0429 and all existing responsibilities.
- roll_forward_or_verified_backup_restore: forward repair; downgrade refuses any new evidence.
- production_postgres_image: postgres:18-bookworm; isolated PostgreSQL 18 verification.
"""

from alembic import op

revision = "20261003_0430"
down_revision = "20261002_0429"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.execute(
        "ALTER TABLE trading_accounts ADD COLUMN execution_faults jsonb NOT NULL DEFAULT '{}'::jsonb "
        "CHECK (jsonb_typeof(execution_faults)='object')"
    )
    op.execute(
        "ALTER TABLE trading_entries ADD COLUMN admission jsonb, ADD COLUMN reserved_margin_usdt numeric "
        "CHECK (reserved_margin_usdt>=0)"
    )
    op.execute("ALTER TABLE trading_orders ADD COLUMN request jsonb, ADD COLUMN resolution jsonb")

    op.execute("""CREATE OR REPLACE TRIGGER trading_entries_write_once
      BEFORE UPDATE OR DELETE ON trading_entries FOR EACH ROW
      EXECUTE FUNCTION trading_reject_rewrite('entry_id','source','case_id','command_id','account_slot','native_symbol',
      'side','request','requested_at_ns','expires_at_ns','created_at_ns','reason','disposed_at_ns','quantity',
      'reference_price','reserved_notional','stop_bps','tp_bps','max_hold_s','opened_at_ns','terminal_at_ns',
      'terminal_reason','realized_pnl','fees','net_pnl','pnl_deadline_ns','admission','reserved_margin_usdt')""")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM trading_accounts WHERE execution_faults <> '{}'::jsonb)
         OR EXISTS (SELECT 1 FROM trading_entries WHERE admission IS NOT NULL OR reserved_margin_usdt IS NOT NULL)
         OR EXISTS (SELECT 1 FROM trading_orders WHERE request IS NOT NULL OR resolution IS NOT NULL)
      THEN RAISE EXCEPTION 'execution evidence requires forward repair, not downgrade'; END IF;
    END $$""")
    op.execute("""CREATE OR REPLACE TRIGGER trading_entries_write_once
      BEFORE UPDATE OR DELETE ON trading_entries FOR EACH ROW
      EXECUTE FUNCTION trading_reject_rewrite('entry_id','source','case_id','command_id','account_slot','native_symbol',
      'side','request','requested_at_ns','expires_at_ns','created_at_ns','reason','disposed_at_ns','quantity',
      'reference_price','reserved_notional','stop_bps','tp_bps','max_hold_s','opened_at_ns','terminal_at_ns',
      'terminal_reason','realized_pnl','fees','net_pnl','pnl_deadline_ns')""")
    op.execute("ALTER TABLE trading_orders DROP COLUMN request, DROP COLUMN resolution")
    op.execute("ALTER TABLE trading_entries DROP COLUMN admission, DROP COLUMN reserved_margin_usdt")
    op.execute("ALTER TABLE trading_accounts DROP COLUMN execution_faults")
