"""Admit the reader-history title-similarity band into the News verdict trace and pin pg_trgm.

Migration evidence:

- category: additive contract widening (one CHECK function restated, one extension pinned)
- why_database_must_change: `news_current_told_trace_valid` enumerates the `retrieval_reason` a told
  entry may carry. #491 adds a fourth reason, `title_similarity` — the delivered cards of the last
  24 h whose normalized `comparison_title` is closest to the candidate's by pg_trgm — and a verdict
  whose trace names it would otherwise be refused by `news_verdicts_current_judgment_check`. The
  band itself is computed by `similarity()` from pg_trgm, which the production database already
  has installed but no revision ever declared; a fresh database must get it from the migration
  chain, not from an operator remembering to create it.
- current_source_revision: 20260902_0349
- minimum_supported_source_revision: 20260902_0349
- lock_level_and_order: `CREATE EXTENSION IF NOT EXISTS pg_trgm`, then one
  `CREATE OR REPLACE FUNCTION` on an IMMUTABLE SQL function referenced by the `news_verdicts`
  CHECK constraint; existing rows are not re-validated, and the widened enumeration still accepts
  every value the old body accepted
- statement_timeout: 30s set locally by the revision
- lock_timeout: 5s set locally by the revision
- estimated_rows: none written; `news_verdicts` is not touched
- estimated_bytes: the pg_trgm extension objects only
- rewrite_or_index_build: none; the band is a 24 h window over `news_deliveries(settled_at_ms)` with
  `ORDER BY similarity(...) DESC LIMIT 32`, ~600 rows at the 2026-09-01 delivery rate, so no trigram
  index is created
- preflight_and_maintenance_boundary: ordinary canonical migration stop with Serve and Workers
  stopped; a verdict written by pre-#491 Workers against the new function stays valid because the
  old reasons remain in the enumeration
- role_and_grant_impact: none; the function keeps its owner and the extension is schema `public`
- failure_state: the transaction rolls back completely
- roll_forward_or_verified_backup_restore: `downgrade()` restores the previous function body; rows
  written with `title_similarity` would then fail re-validation on their next UPDATE, so downgrade
  only before Workers on this revision have judged
- archive_current_compatibility: additive; every existing verdict validates under the new body
- evidence_postgresql_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260902_0350
Revises: 20260902_0349
Create Date: 2026-09-02 08:00:00
"""

from __future__ import annotations

from alembic import op

revision = "20260902_0350"
down_revision = "20260902_0349"
branch_labels = None
depends_on = None


def _told_trace_valid(retrieval_reasons: str) -> str:
    # S608: the only interpolation is one of the two module-owned literal lists below; no value is bound.
    return f"""
        CREATE OR REPLACE FUNCTION public.news_current_told_trace_valid(value jsonb) RETURNS boolean
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
            AS $$
                  SELECT jsonb_typeof(value) = 'array'
                     AND jsonb_array_length(value) <= 16
                     AND NOT EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements(value) WITH ORDINALITY AS told(entry, position)
                            WHERE NOT (
                              news_jsonb_exact_keys(entry, ARRAY[
                                'i','event_id','at_ms','ago_min','storyline_key','comparison_title',
                                'comparison_fingerprint','symbols','magnitude','direction','headline_zh',
                                'why_zh','tier','similarity','history_scope','retrieval_reason'
                              ])
                              AND news_jsonb_int64_valid(entry -> 'i')
                              AND (entry ->> 'i')::numeric = position - 1
                              AND jsonb_typeof(entry -> 'event_id') = 'string' AND entry ->> 'event_id' <> ''
                              AND news_jsonb_int64_valid(entry -> 'at_ms') AND (entry ->> 'at_ms')::numeric >= 0
                              AND news_jsonb_int64_valid(entry -> 'ago_min') AND (entry ->> 'ago_min')::numeric >= 0
                              AND jsonb_typeof(entry -> 'storyline_key') = 'string'
                              AND jsonb_typeof(entry -> 'comparison_title') = 'string'
                              AND jsonb_typeof(entry -> 'comparison_fingerprint') = 'string'
                              AND jsonb_typeof(entry -> 'symbols') = 'array'
                              AND jsonb_array_length(entry -> 'symbols') <= 6
                              AND NOT EXISTS (
                                SELECT 1 FROM jsonb_array_elements(entry -> 'symbols') symbol
                                 WHERE jsonb_typeof(symbol) <> 'string' OR symbol #>> '{{}}' = ''
                              )
                              AND news_jsonb_int64_valid(entry -> 'magnitude')
                              AND (entry ->> 'magnitude')::numeric BETWEEN 0 AND 3
                              AND entry ->> 'direction' IN ('bullish','bearish','neutral','unclear')
                              AND jsonb_typeof(entry -> 'headline_zh') = 'string'
                              AND length(entry ->> 'headline_zh') <= 60
                              AND jsonb_typeof(entry -> 'why_zh') = 'string'
                              AND length(entry ->> 'why_zh') <= 140
                              AND entry ->> 'tier' IN (
                                'exact_fact','storyline','asset_overlap','fact_similarity','recency'
                              )
                              AND jsonb_typeof(entry -> 'similarity') = 'number'
                              AND (entry ->> 'similarity')::numeric BETWEEN 0 AND 1
                              AND entry ->> 'history_scope' IN ('recent','targeted')
                              AND entry ->> 'retrieval_reason' IN ({retrieval_reasons})
                            )
                         )
                $$
    """  # noqa: S608


_PREVIOUS_REASONS = "'recent','exact_fingerprint','canonical_asset_overlap'"
_CURRENT_REASONS = "'recent','exact_fingerprint','canonical_asset_overlap','title_similarity'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    op.execute(_told_trace_valid(_CURRENT_REASONS))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(_told_trace_valid(_PREVIOUS_REASONS))
