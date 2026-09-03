"""Order `native_identity_references` by code point, as the contract always has (#510 PR-1).

Migration evidence:

- category: function rewrite, no table or column change
- why_database_must_change: `trading_execution_string_array_valid` demanded that the array equal
  `jsonb_agg(item ORDER BY item #>> '{}')`, which sorts under the database default collation
  (`en_US.utf8` in production and in the pinned test image). `ExecutionObservationV1` sorts by code
  point. The two agree only while every element is in one case, and real Nautilus identities are not:
  Binance contract and position ids are upper case (`UNIUSDT-PERP.BINANCE-...`), deterministic client
  order ids are lower case (`tf...`). The first fill that carried a position was therefore rejected by
  `trading_execution_observation_native_refs_check`, and with it every later observation in the same
  queue, for the whole 04:04-09:58 window of 2026-09-02. Adding `COLLATE "C"` is also what makes the
  function honestly `IMMUTABLE`: its previous result depended on the database's default collation.
- current_source_revision: 20260903_0352
- minimum_supported_source_revision: 20260903_0352
- lock_level_and_order: `CREATE OR REPLACE FUNCTION` takes ACCESS EXCLUSIVE on the pg_proc entry only.
  No table is locked, no constraint is dropped or revalidated.
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: none read or written
- estimated_bytes: one catalog row
- rewrite_or_index_build: none
- preflight_and_maintenance_boundary: ordinary canonical migration stop
- role_and_grant_impact: none; the single tracefold login is unchanged
- archive_current_compatibility: strictly widening for every row that exists. Each stored array was
  produced by the code-point-sorting contract and had to satisfy the collation order as well to have
  been admitted, so every historical row satisfies the new predicate too. `trading_execution_observations`
  is append-only and immutable by trigger, so no stored row is ever re-checked in any case.
- failure_state: the transaction rolls back completely and the collation-ordered predicate stays
- roll_forward_or_verified_backup_restore: `downgrade` restores the previous definition exactly
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260903_0353
Revises: 20260903_0352
Create Date: 2026-09-03 03:50:00
"""

from __future__ import annotations

from alembic import op

revision = "20260903_0353"
down_revision = "20260903_0352"
branch_labels = None
depends_on = None

# `trading_execution_observation_native_refs_check` is the only consumer of this function.
_CODE_POINT_ORDER = """
CREATE OR REPLACE FUNCTION public.trading_execution_string_array_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= 16
             AND octet_length(value::text) <= 4096
             AND NOT EXISTS (
               SELECT 1 FROM jsonb_array_elements(value) item
                WHERE jsonb_typeof(item) <> 'string' OR char_length(item #>> '{}') NOT BETWEEN 1 AND 256
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

_DATABASE_COLLATION_ORDER = """
CREATE OR REPLACE FUNCTION public.trading_execution_string_array_valid(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$
          SELECT jsonb_typeof(value) = 'array'
             AND jsonb_array_length(value) <= 16
             AND octet_length(value::text) <= 4096
             AND NOT EXISTS (
               SELECT 1 FROM jsonb_array_elements(value) item
                WHERE jsonb_typeof(item) <> 'string' OR char_length(item #>> '{}') NOT BETWEEN 1 AND 256
             )
             AND value = COALESCE(
               (SELECT jsonb_agg(item ORDER BY item #>> '{}') FROM jsonb_array_elements(value) item),
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
    op.execute(_CODE_POINT_ORDER)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(_DATABASE_COLLATION_ORDER)
