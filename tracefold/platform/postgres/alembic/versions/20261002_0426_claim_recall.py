"""#791: persistent claim versions, fp16 facts and shared retrieval.

Migration evidence:
- category: additive News claim index and removal of superseded lexical retrieval.
- why_database_must_change: one durable versioned proposition index replaces the two retrieval paths.
- current_source_revision: 20261002_0425
- minimum_supported_source_revision: 20261002_0425
- lock_level_and_order: ACCESS EXCLUSIVE on news_event_assets; stop writers before migration.
- statement_timeout: 600s; lock_timeout: 5s.
- estimated_rows: new table empty; approximately 75k claims after bounded 30-day backfill.
- estimated_bytes: approximately 58 MB fp16 vectors plus text and indexes.
- rewrite_or_index_build: new GIN/B-tree indexes; downgrade rewrites generated asset columns.
- preflight_and_maintenance_boundary: verified backup and supported migrate-before-start workflow.
- archive_current_compatibility: adopted documents and frozen receipts are unchanged.
- role_and_grant_impact: none; no extension or superuser requirement.
- failure_state: transactional DDL rollback.
- roll_forward_or_verified_backup_restore: downgrade restores 0419 retrieval before the old image starts.
"""

from alembic import op

revision = "20261002_0426"
down_revision = "20261002_0425"
branch_labels = None
depends_on = None

_ADDRESS = r"^(?:0[xX][0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}|solana:.*)$"
_QUOTES = ("USDT", "USDC", "FDUSD", "TUSD", "BUSD", "USD")
_TAGGED = r"regexp_replace(symbol, '^[[:space:]$]+|[[:space:]]+$', '', 'g')"
_CODE = "regexp_replace(regexp_replace(upper(tagged), '^XYZ-', ''), '^[^:]*:', '')"
_PAIR_BASE = " ".join(
    f"WHEN right(code, {len(quote)}) = '{quote}' AND length(code) > {len(quote) + 1}"
    f" THEN left(code, length(code) - {len(quote)})"
    for quote in _QUOTES
)


def _sql(statement: str) -> str:
    # The patterns contain `(?:` and `prefix:`; a backslash keeps SQLAlchemy from reading `:name` as a bind.
    return statement.replace(":", "\\:")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    op.execute("""
        CREATE TABLE public.news_claim_index (
          claim_ref text NOT NULL, text_sha256 text NOT NULL,
          event_id text NOT NULL REFERENCES public.news_events(event_id) ON DELETE CASCADE,
          first_available_at_ms bigint NOT NULL CHECK (first_available_at_ms>=0),
          embed_text text NOT NULL,
          lexical_text text NOT NULL,
          lexical tsvector GENERATED ALWAYS AS (to_tsvector('english',lexical_text)) STORED,
          numbers text[] NOT NULL DEFAULT '{}', structure_keys text[] NOT NULL DEFAULT '{}',
          embedder text, vector bytea,
          PRIMARY KEY (claim_ref,text_sha256),
          CHECK (vector IS NULL OR embedder IS NOT NULL)
        );
        CREATE INDEX news_claim_index_lexical ON public.news_claim_index USING gin(lexical);
        CREATE INDEX news_claim_index_source ON public.news_claim_index USING gin(structure_keys);
        CREATE INDEX news_claim_index_window ON public.news_claim_index(first_available_at_ms,event_id);
        CREATE INDEX news_claim_index_pending ON public.news_claim_index(first_available_at_ms)
          WHERE vector IS NULL;
        DROP INDEX public.ix_news_event_members_fact_trgm;
        ALTER TABLE public.news_event_assets DROP COLUMN retrieval_pair_base, DROP COLUMN retrieval_symbol;
        DROP FUNCTION public.news_asset_retrieval_pair_base(text,text);
        DROP FUNCTION public.news_asset_retrieval_symbol(text);
    """)

    # Permission generations fence sent sets and persisted claim links only.
    # Own head/work ownership is already checked under the Event lock.
    for trigger, table in (
        ("news_reader_analysis_insert", "news_analyses"),
        ("news_reader_analysis_update", "news_analyses"),
        ("news_reader_analysis_delete", "news_analyses"),
        ("news_reader_membership", "news_event_members"),
        ("news_reader_item_metadata", "news_items"),
        ("news_reader_event_kind", "news_events"),
    ):
        op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
    op.execute("""
        CREATE CONSTRAINT TRIGGER news_reader_analysis_insert AFTER INSERT ON public.news_analyses
          DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.adopted_at_ms IS NOT NULL AND
          jsonb_path_exists(NEW.document,'$.changes[*] ? (@.previous_ref != null)'))
          EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_analysis_update AFTER UPDATE ON public.news_analyses
          DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.adopted_at_ms IS NOT NULL AND
          NEW.document->'changes' IS DISTINCT FROM OLD.document->'changes' AND
          (jsonb_path_exists(NEW.document,'$.changes[*] ? (@.previous_ref != null)') OR
           jsonb_path_exists(OLD.document,'$.changes[*] ? (@.previous_ref != null)')))
          EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_analysis_delete AFTER DELETE ON public.news_analyses
          DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (OLD.adopted_at_ms IS NOT NULL AND
          jsonb_path_exists(OLD.document,'$.changes[*] ? (@.previous_ref != null)'))
          EXECUTE FUNCTION public.news_reader_advance();
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '600s'")
    # SQL-standard bodies bind their objects at creation, so restores and sessions with an empty search_path
    # compute the same values.
    op.execute(
        _sql(f"""
        CREATE FUNCTION public.news_asset_retrieval_symbol(symbol text) RETURNS text
          LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
        BEGIN ATOMIC
          SELECT CASE WHEN tagged ~ '{_ADDRESS}' THEN tagged ELSE {_CODE} END
            FROM (SELECT {_TAGGED} AS tagged) AS edge;
        END
        """)  # noqa: S608 - code-owned constants only
    )
    op.execute(
        _sql(f"""
        CREATE FUNCTION public.news_asset_retrieval_pair_base(symbol text, market_type text) RETURNS text
          LANGUAGE sql IMMUTABLE PARALLEL SAFE
        BEGIN ATOMIC
          SELECT CASE WHEN COALESCE(market_type, 'unknown') IN ('crypto', 'unknown') AND tagged !~ '{_ADDRESS}'
                      THEN CASE {_PAIR_BASE} END END
            FROM (SELECT tagged, {_CODE} AS code FROM (SELECT {_TAGGED} AS tagged) AS edge) AS normalized;
        END
        """)  # noqa: S608 - code-owned constants only
    )
    op.execute("""
        ALTER TABLE public.news_event_assets
          ADD COLUMN retrieval_symbol text
            GENERATED ALWAYS AS (public.news_asset_retrieval_symbol(symbol)) STORED,
          ADD COLUMN retrieval_pair_base text
            GENERATED ALWAYS AS (public.news_asset_retrieval_pair_base(symbol, market_type)) STORED
    """)
    op.execute(
        "CREATE INDEX ix_news_event_assets_retrieval_symbol ON public.news_event_assets (retrieval_symbol, event_id)"
    )
    op.execute(
        "CREATE INDEX ix_news_event_assets_retrieval_pair_base ON public.news_event_assets"
        " (retrieval_pair_base, event_id) WHERE retrieval_pair_base IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_news_event_members_fact_trgm ON public.news_event_members"
        " USING gin (fact_text public.gin_trgm_ops) WITH (fastupdate = off)"
    )
    for trigger in ("news_reader_analysis_insert", "news_reader_analysis_update", "news_reader_analysis_delete"):
        op.execute(f"DROP TRIGGER {trigger} ON public.news_analyses")
    op.execute("""
        CREATE CONSTRAINT TRIGGER news_reader_analysis_insert AFTER INSERT ON public.news_analyses
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (NEW.adopted_at_ms IS NOT NULL) EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_analysis_update AFTER UPDATE ON public.news_analyses
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (NEW.adopted_at_ms IS NOT NULL AND
            (OLD.adopted_at_ms IS DISTINCT FROM NEW.adopted_at_ms OR OLD.document IS DISTINCT FROM NEW.document))
          EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_analysis_delete AFTER DELETE ON public.news_analyses
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (OLD.adopted_at_ms IS NOT NULL) EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_membership AFTER INSERT OR UPDATE OR DELETE ON public.news_event_members
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_item_metadata AFTER UPDATE ON public.news_items
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (NEW.provider_metadata IS DISTINCT FROM OLD.provider_metadata)
          EXECUTE FUNCTION public.news_reader_advance();
        CREATE CONSTRAINT TRIGGER news_reader_event_kind AFTER UPDATE ON public.news_events
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW WHEN (NEW.event_kind IS DISTINCT FROM OLD.event_kind)
          EXECUTE FUNCTION public.news_reader_advance();
    """)
    op.execute("DROP TABLE public.news_claim_index")
