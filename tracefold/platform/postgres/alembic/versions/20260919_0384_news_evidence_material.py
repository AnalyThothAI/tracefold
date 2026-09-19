"""Preserve editorial material and bind delivered history (#664, based on #663).

Forward-only additive cut from 0383. Stop writers during migration; ACCESS EXCLUSIVE
locks news_items then news_deliveries for metadata-only nullable additions. Existing
material/timestamps are NOT backfilled. New tables/indexes start empty; the bounded
time-window index scans news_events once under the maintenance gate. lock_timeout=5s,
statement_timeout=600s. Failure rolls back atomically; roll forward or restore the
verified pre-cut backup. Old snapshots/reviews/executions remain readable archives.
The existing judgment CHECK hashes full historical evidence JSON; a restored
production ledger of 26,615 verdicts exceeds the former 120-second scan budget.
Test target: PostgreSQL 18; no production application is authorized by this revision.
"""

from alembic import op

revision = "20260919_0384"
down_revision = "20260919_0383"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.execute("""
        ALTER TABLE news_items
          ADD COLUMN provider_params_available_at_ms bigint,
          ADD COLUMN provider_params_sha256 text,
          ADD COLUMN evidence_text text,
          ADD COLUMN evidence_text_sha256 text,
          ADD COLUMN provider_params_conflict_sha256 text,
          ADD COLUMN provider_params_conflict_at_ms bigint;
        ALTER TABLE news_deliveries ADD COLUMN history_context jsonb;
        CREATE TABLE news_evidence_documents (
          document_id text PRIMARY KEY,
          requested_url text NOT NULL,
          final_url text NOT NULL,
          normalized_url text NOT NULL,
          response_sha256 text NOT NULL,
          extracted_text_sha256 text NOT NULL,
          extractor_version text NOT NULL,
          extracted_text text NOT NULL CHECK (length(extracted_text) <= 100000),
          reported_published_at_ms bigint,
          observed_at_ms bigint NOT NULL,
          available_at_ms bigint NOT NULL,
          content_type text NOT NULL,
          extraction_status text NOT NULL CHECK (extraction_status = 'success'),
          UNIQUE(normalized_url, response_sha256, extractor_version)
        );
        CREATE INDEX ix_news_evidence_documents_url_time
          ON news_evidence_documents(normalized_url, available_at_ms DESC);
        CREATE FUNCTION public.reject_news_document_mutation() RETURNS trigger
          LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'news_document_append_only'; END;
          $$;
        CREATE TRIGGER trg_news_document_append_only BEFORE UPDATE OR DELETE ON news_evidence_documents
          FOR EACH ROW EXECUTE FUNCTION public.reject_news_document_mutation();
        CREATE INDEX ix_news_events_evidence_time
          ON news_events(created_at_ms DESC, event_id);
    """)
    op.execute("""
        DO $migration$
        DECLARE definition text;
        BEGIN
          SELECT pg_get_constraintdef(oid) INTO STRICT definition FROM pg_constraint
            WHERE conrelid='news_verdicts'::regclass AND conname='news_verdicts_current_judgment_check';
          IF position('news_semantic_program_v10' in definition) = 0 THEN
            RAISE EXCEPTION 'unexpected_news_judgment_constraint';
          END IF;
          definition := replace(definition,
            '''news_semantic_program_v10''::text]))',
            '''news_semantic_program_v10''::text, ''news_semantic_program_v11''::text]))');
          definition := replace(definition,
            '''news_semantic_program_v10''::text)',
            '''news_semantic_program_v10''::text AND program_version <> ''news_semantic_program_v11''::text)');
          ALTER TABLE news_verdicts DROP CONSTRAINT news_verdicts_current_judgment_check;
          EXECUTE 'ALTER TABLE news_verdicts ADD CONSTRAINT news_verdicts_current_judgment_check ' || definition;
        END $migration$;
    """)
    op.execute("""
        DO $migration$
        DECLARE definition text;
        BEGIN
          SELECT pg_get_functiondef('public.news_current_told_trace_valid(jsonb)'::regprocedure)
            INTO STRICT definition;
          definition := replace(definition, 'news_jsonb_exact_keys(entry, ARRAY[',
            'news_jsonb_exact_keys(entry - ''assets'' - ''provenance_status'', ARRAY[');
          definition := replace(definition, 'AND news_jsonb_int64_valid(entry -> ''i'')',
            $validation$
            AND (NOT entry ? 'assets' OR (
              jsonb_typeof(entry -> 'assets') = 'array'
              AND jsonb_array_length(entry -> 'assets') <= 6
              AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(entry -> 'assets') asset
                WHERE jsonb_typeof(asset) <> 'object'
                   OR NOT news_jsonb_exact_keys(asset, ARRAY['symbol','market_type'])
                   OR COALESCE(asset->>'symbol', '') = ''
                   OR COALESCE(asset->>'market_type', '') NOT IN
                        ('crypto','equity','commodity','index','fx','pre_ipo','unknown'))
            ))
            AND (NOT entry ? 'provenance_status' OR entry->>'provenance_status' IN
                 ('delivery_bound','legacy_receipt_only'))
            AND news_jsonb_int64_valid(entry -> 'i')
            $validation$);
          EXECUTE definition;
        END $migration$;
    """)


def downgrade() -> None:
    raise RuntimeError("news_evidence_material_forward_only: restore a verified backup")
