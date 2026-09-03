"""Publish the Runtime's executable market catalogue on its own projection (#510 PR-2).

Migration evidence:

- category: additive current-projection column plus one validator function
- why_database_must_change: the route catalogue is discovered once per Runtime start and lived only
  in that process (`root.py:_discover_routes`). The Signal lane could not see it, so on 2026-09-02
  three of six Signals were emitted for markets Binance USD-M does not list and came back
  `instrument_unmapped` from the Runtime; each had already spent the lane's one Case freeze for the
  turn. The catalogue is current state about one account slot, which is exactly what
  `trading_execution_runtime_state` already is, so it becomes a column there rather than a new table,
  a new interface or a second read.
- current_source_revision: 20260903_0353
- minimum_supported_source_revision: 20260903_0353
- lock_level_and_order: one `ALTER TABLE ... ADD COLUMN ... DEFAULT` (metadata-only since
  PostgreSQL 11) then `ADD CONSTRAINT` with a full-table validation scan, both ACCESS EXCLUSIVE on a
  table that holds one row per account slot, taken after canonical migration has stopped every
  steady application process. `CREATE FUNCTION` first, because the constraint calls it.
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: one current row per account slot, currently one
- estimated_bytes: a bounded string array; the production catalogue is about 500 keys, roughly 12 KiB
  per row, and the column is capped at 1024 keys and 65536 bytes
- rewrite_or_index_build: no heap rewrite (constant default) and no index
- preflight_and_maintenance_boundary: ordinary canonical migration stop
- role_and_grant_impact: none; the single tracefold login owns the projection and the function
- archive_current_compatibility: Signal, Command and Observation history is untouched. This is the
  replaceable current read model, and the existing row is backfilled with the empty catalogue, which
  the Signal lane reads as "no Runtime catalogue is published" and therefore admits exactly as it did
  before the cut. The first start after deploy replaces it with the real catalogue.
- failure_state: the transaction rolls back completely; the column and the function are both absent
- roll_forward_or_verified_backup_restore: `downgrade` drops the constraint, the column and the
  function, which is exact because nothing else reads or writes either
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260903_0354
Revises: 20260903_0353
Create Date: 2026-09-03 06:10:00
"""

from __future__ import annotations

from alembic import op

revision = "20260903_0354"
down_revision = "20260903_0353"
branch_labels = None
depends_on = None

# `trading_execution_string_array_valid` cannot be reused: it caps an array at 16 elements because it
# validates one observation's native identity references, and the Binance USD-M USDT-perpetual
# catalogue is about 500 keys. This validator is the same shape at catalogue scale, and it orders by
# code point (`COLLATE "C"`) for the reason `20260903_0353` had to: Python sorts by code point, and
# `en_US.utf8` does not agree with it once a key mixes cases.
_MARKET_KEY_ARRAY_VALID = """
CREATE OR REPLACE FUNCTION public.trading_execution_market_key_array_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= 1024
             AND octet_length(value::text) <= 65536
             AND NOT EXISTS (
               SELECT 1 FROM jsonb_array_elements(value) item
                WHERE jsonb_typeof(item) <> 'string'
                   OR (item #>> '{}') !~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$'
             )
             AND value = COALESCE(
               (SELECT jsonb_agg(item ORDER BY (item #>> '{}') COLLATE "C") FROM jsonb_array_elements(value) item),
               '[]'::jsonb
             )
             AND jsonb_array_length(value) = (
               SELECT count(DISTINCT item) FROM jsonb_array_elements(value) item
             )
        $$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(_MARKET_KEY_ARRAY_VALID)
    op.execute("ALTER TABLE trading_execution_runtime_state ADD COLUMN routes jsonb NOT NULL DEFAULT '[]'::jsonb")
    op.execute(
        """
        ALTER TABLE trading_execution_runtime_state
          ADD CONSTRAINT trading_execution_runtime_routes_check
          CHECK (trading_execution_market_key_array_valid(routes))
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("ALTER TABLE trading_execution_runtime_state DROP CONSTRAINT trading_execution_runtime_routes_check")
    op.execute("ALTER TABLE trading_execution_runtime_state DROP COLUMN routes")
    op.execute("DROP FUNCTION public.trading_execution_market_key_array_valid(jsonb)")
