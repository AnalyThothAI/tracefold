"""News source observation chains and claim-target lookup indexes.

Migration evidence:
- category: additive source revision metadata and indexes.
- why_database_must_change: content identity cannot represent A->B->A; local observed order and
  predecessor identity must survive retries. Cross-Event target queries need an indexed access path.
- current_source_revision: 20260926_0404
- minimum_supported_source_revision: 20260926_0404
- lock_level_and_order: news_items, then news_item_revisions; index builds on News/Trading ledgers.
- statement_timeout: 300s; lock_timeout: 5s.
- estimated_rows: News revision metadata only; immutable document bodies are not rewritten.
- estimated_bytes: two hashes and an int64 per revision, one int64 per item, target indexes.
- rewrite_or_index_build: backfill revision metadata; build GIN expression/JSONB indexes.
- preflight_and_maintenance_boundary: stop Serve/Workers for the normal schema/image switch.
- archive_current_compatibility: preserve all source bodies, evidence IDs, adopted documents and receipts.
- role_and_grant_impact: none.
- failure_state: transactional DDL rolls back.
- roll_forward_or_verified_backup_restore: forward-only; restore verified backup and matching image.
- validation_environment: disposable PostgreSQL, never the live ledger.
- production_postgres_image: postgres:18-bookworm.
"""

from alembic import op

revision = "20260927_0405"
down_revision = "20260926_0404"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '300s'")
    op.execute("""
        ALTER TABLE news_event_updates DROP CONSTRAINT news_event_updates_document_check;
        ALTER TABLE news_event_updates ADD CONSTRAINT news_event_updates_document_check CHECK (
          jsonb_typeof(document)='object'
          AND document->>'schema_version' IN ('news_event_update_v1','news_event_update_v2')
          AND document->>'event_id'=event_id
          AND document->>'content_revision'=content_revision
          AND document->'input_revision'=to_jsonb(input_revision)
          AND document->>'previous_content_revision' IS NOT DISTINCT FROM previous_content_revision);
        ALTER TABLE news_items ADD COLUMN evidence_observed_at_ms bigint;
        ALTER TABLE news_item_revisions
          ADD COLUMN content_sha256 text,
          ADD COLUMN previous_revision_sha256 text,
          ADD COLUMN revision_sequence bigint;
        WITH ordered AS (
          SELECT item_id, revision_sha256,
                 row_number() OVER (PARTITION BY item_id ORDER BY received_at_ms,revision_sha256) AS seq,
                 lag(revision_sha256) OVER (PARTITION BY item_id ORDER BY received_at_ms,revision_sha256) AS previous
            FROM news_item_revisions
        )
        UPDATE news_item_revisions r
           SET content_sha256=r.revision_sha256, previous_revision_sha256=o.previous, revision_sequence=o.seq
          FROM ordered o WHERE r.item_id=o.item_id AND r.revision_sha256=o.revision_sha256;
        UPDATE news_items i SET evidence_observed_at_ms=GREATEST(i.observed_at_ms,
          (SELECT max(received_at_ms) FROM news_item_revisions r WHERE r.item_id=i.item_id));
        ALTER TABLE news_item_revisions
          ALTER COLUMN content_sha256 SET NOT NULL,
          ALTER COLUMN revision_sequence SET NOT NULL,
          ADD CONSTRAINT news_item_revision_sequence_check CHECK (revision_sequence>0),
          ADD CONSTRAINT news_item_revision_content_check CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
          ADD CONSTRAINT news_item_revision_sequence_unique UNIQUE(item_id, revision_sequence);
        CREATE INDEX news_updates_affected_claims_idx ON news_event_updates USING gin
          (jsonb_path_query_array(document, '$.changes[*].previous_ref'));
        CREATE INDEX trading_amendments_affected_idx ON trading_source_amendments USING gin (affected_claim_refs);
        CREATE INDEX trading_amendments_retired_idx ON trading_source_amendments USING gin (retired_claim_refs);
        CREATE INDEX trading_catalyst_superseded_idx ON trading_triggers USING gin
          ((payload->'superseded_claim_refs')) WHERE kind='catalyst';
    """)


def downgrade() -> None:
    raise RuntimeError("news_revision_ownership_forward_only: restore the verified backup with its matching image")
