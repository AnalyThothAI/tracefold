"""Native economic identity in the existing execution observation ledger (#699).

Migration evidence:
- category: additive nullable identity columns, native-only uniqueness and kind check.
- why_database_must_change: event/report UUIDs do not enforce venue trade uniqueness.
- current_source_revision: 20260926_0402
- minimum_supported_source_revision: 20260926_0402
- lock_level_and_order: ACCESS EXCLUSIVE on execution observations for catalog changes.
- statement_timeout: 60s locally; lock_timeout: 5s locally.
- estimated_rows: incident ledger approximately 12,000; preflight count before cut.
- estimated_bytes: nullable columns have no backfill/rewrite; partial index starts empty.
- preflight_and_maintenance_boundary: stop writers/serve, retain backup, migrate,
  then start matching Runtime and readers together; no live-history apply here.
- archive_current_compatibility: raw historical observations stay byte-for-byte;
  untyped historical references are never promoted to native trade identities.
- role_and_grant_impact: none.
- failure_state: transactional DDL rolls back.
- roll_forward_or_verified_backup_restore: restore pre-cut archive and matching images.
- validation_environment: isolated PostgreSQL 18 testcontainers, never the live ledger.

Revision ID: 20260926_0403
Revises: 20260926_0402
"""

from __future__ import annotations

from alembic import op

revision = "20260926_0403"
down_revision = "20260926_0402"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute(
        "ALTER TABLE public.trading_execution_observations "
        "ADD COLUMN native_environment text, ADD COLUMN native_instrument text, ADD COLUMN native_trade_id text"
    )
    op.execute(
        "ALTER TABLE public.trading_execution_observations DROP CONSTRAINT trading_execution_observation_kind_check"
    )
    op.execute("""
        ALTER TABLE public.trading_execution_observations
        ADD CONSTRAINT trading_execution_observation_kind_check CHECK (normalized_kind IN (
            'signal_disposition', 'control_disposition', 'risk', 'order', 'fill', 'position', 'protection',
            'funding', 'funding_coverage', 'native_fill', 'native_fill_cost', 'native_fill_binding',
            'native_order_result')),
        ADD CONSTRAINT trading_execution_native_identity_check CHECK (
            CASE WHEN normalized_kind IN ('native_fill', 'native_fill_cost', 'native_fill_binding') THEN
                native_environment IS NOT NULL AND native_environment IN ('LIVE', 'DEMO', 'TESTNET')
                AND native_instrument IS NOT NULL AND native_instrument ~ '^[A-Z0-9]+$'
                AND native_trade_id IS NOT NULL AND native_trade_id ~ '^(0|[1-9][0-9]*)$'
                AND native_environment IS NOT DISTINCT FROM summary ->> 'venue_environment'
                AND native_instrument IS NOT DISTINCT FROM summary ->> 'native_instrument'
                AND native_trade_id IS NOT DISTINCT FROM summary ->> 'native_trade_id'
            ELSE native_environment IS NULL AND native_instrument IS NULL AND native_trade_id IS NULL END)
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_trading_observation_native_fact
        ON public.trading_execution_observations
            (account_slot, native_environment, native_instrument, native_trade_id, normalized_kind)
        WHERE native_trade_id IS NOT NULL
    """)

    op.execute("""
        CREATE UNIQUE INDEX ux_trading_observation_native_order_result
        ON public.trading_execution_observations
            (account_slot, (summary ->> 'venue_environment'), (summary ->> 'native_instrument'),
             (summary ->> 'venue_order_id')) WHERE normalized_kind = 'native_order_result'
    """)
    op.execute("""
        CREATE INDEX ix_trading_observation_native_order_fills
        ON public.trading_execution_observations
            (account_slot, native_environment, native_instrument, (summary ->> 'venue_order_id'))
        WHERE normalized_kind = 'native_fill'
    """)


def downgrade() -> None:
    raise RuntimeError("native_fill_identity_forward_only: restore a verified pre-0403 archive")
