"""Freeze the News current Event projection to explicit columns (#375).

Migration evidence:

- category: destructive hard-cut
- why_database_must_change: the security-barrier database view is a public/cross-context projection;
  replacing its published wildcard prevents a later base-table column from silently widening that contract
- current_source_revision: 20260830_0333
- minimum_supported_source_revision: 20260830_0333
- lock_level_and_order: ACCESS EXCLUSIVE on news_current_events_v1 only; no base-table lock is requested
- statement_timeout: 5s
- lock_timeout: 1s
- estimated_rows: 0 (view definition only)
- estimated_bytes: 0 (no heap/index rewrite)
- rewrite_or_index_build: none
- preflight_and_maintenance_boundary: normal stopped-writer migration gate
- archive_current_compatibility: unchanged NOT current_contract_archive_only predicate and identical current columns
- role_and_grant_impact: none; CREATE OR REPLACE preserves view owner and grants
- failure_state: transactional DDL rolls back to the prior definition and business processes remain stopped
- roll_forward_or_verified_backup_restore: roll forward; use the verified pre-migration backup for rollback
- production_postgres_image:
  postgres:18-bookworm@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296

Revision ID: 20260830_0334
Revises: 20260830_0333
"""

from __future__ import annotations

from alembic import op

revision = "20260830_0334"
down_revision = "20260830_0333"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '1s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute(
        """
        CREATE OR REPLACE VIEW news_current_events_v1 WITH (security_barrier = true) AS
        SELECT event_id, leader_item_id, dedupe_family, comparison_fingerprint,
               comparison_title, leader_title, opened_at_ms, last_member_at_ms,
               expires_at_ms, member_count, admission, queue_priority,
               provider_score_max, engine_type, asset_class, grounded_assets,
               watchlist_hits, macro_lexicon, storyline_key, context_line,
               search_doc, published_at_ms, followup_of, ingest_mode, trace_id,
               created_at_ms, updated_at_ms, focus_fact_id, focus_fact_text,
               focus_fact_context, focus_fact_method, focus_span_start,
               focus_span_end, event_kind, source_contract_reason,
               current_contract_archive_only
          FROM news_events
         WHERE NOT current_contract_archive_only
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260830_0334 is an irreversible current-projection hard cut; restore a backup")
