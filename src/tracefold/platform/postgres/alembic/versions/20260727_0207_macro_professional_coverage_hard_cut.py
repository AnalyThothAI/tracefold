"""Hard-cut Macro coverage, Fed evidence, and module-specific read contracts."""

from __future__ import annotations

from alembic import op

revision = "20260727_0207"
down_revision = "20260727_0206"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30min'")
    _hard_cut_module_current()
    _archive_v1_publication_lane()
    _create_v2_publication_lane()
    _extend_document_types()
    _create_fed_evidence_tables()
    _retire_generic_document_target()


def downgrade() -> None:
    raise RuntimeError("20260727_0207 is an irreversible Macro contract hard cut; apply a forward fix")


def _hard_cut_module_current() -> None:
    op.execute("DELETE FROM macro_module_current")
    op.execute("ALTER TABLE macro_module_current DROP COLUMN readiness")
    op.execute(
        """
        ALTER TABLE macro_module_current
          ADD COLUMN data_health_state TEXT NOT NULL
          CHECK (data_health_state IN (
            'current', 'delayed', 'stale', 'invalid', 'backfilling', 'unavailable'
          ))
        """
    )
    op.execute(
        """
        ALTER TABLE macro_module_current
          ADD CONSTRAINT macro_module_current_typed_schema_check
          CHECK (
            payload_json ->> 'schema_version' = CASE module_id
              WHEN 'rates_fed' THEN 'macro_rates_fed_v2'
              WHEN 'economy_inflation' THEN 'macro_economy_inflation_v2'
              WHEN 'liquidity_funding' THEN 'macro_liquidity_funding_v2'
              WHEN 'credit' THEN 'macro_credit_v2'
              WHEN 'volatility' THEN 'macro_volatility_v2'
              WHEN 'cross_asset' THEN 'macro_cross_asset_v2'
            END
          )
        """
    )


def _archive_v1_publication_lane() -> None:
    for current_name, archive_name in (
        ("macro_research_publications", "macro_research_publications_v1_archive"),
        ("macro_research_runs", "macro_research_runs_v1_archive"),
        ("macro_event_updates", "macro_event_updates_v1_archive"),
        ("macro_daily_judgments", "macro_daily_judgments_v1_archive"),
        ("macro_evidence_packs", "macro_evidence_packs_v1_archive"),
    ):
        op.execute(f"ALTER TABLE {current_name} RENAME TO {archive_name}")


def _create_v2_publication_lane() -> None:
    op.execute(
        """
        CREATE TABLE macro_evidence_packs (
          evidence_pack_id TEXT
            CONSTRAINT macro_evidence_packs_v2_pkey PRIMARY KEY
            CHECK (btrim(evidence_pack_id) <> ''),
          session_date DATE NOT NULL,
          judgment_cutoff_ms BIGINT NOT NULL CHECK (judgment_cutoff_ms >= 0),
          latest_fact_at_ms BIGINT NOT NULL CHECK (latest_fact_at_ms >= 0),
          schema_version TEXT NOT NULL
            CHECK (schema_version = 'macro_evidence_pack_v2'),
          compiler_version TEXT NOT NULL CHECK (btrim(compiler_version) <> ''),
          payload_json JSONB NOT NULL
            CHECK (
              jsonb_typeof(payload_json) = 'object'
              AND payload_json ->> 'schema_version' = 'macro_evidence_pack_v2'
            ),
          payload_hash TEXT NOT NULL
            CONSTRAINT macro_evidence_packs_v2_payload_hash_key UNIQUE
            CHECK (btrim(payload_hash) <> ''),
          created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= judgment_cutoff_ms),
          CONSTRAINT macro_evidence_packs_v2_session_cutoff_key
            UNIQUE (session_date, judgment_cutoff_ms)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE macro_daily_judgments (
          session_date DATE
            CONSTRAINT macro_daily_judgments_v2_pkey PRIMARY KEY,
          evidence_pack_id TEXT NOT NULL
            REFERENCES macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT,
          judgment_cutoff_ms BIGINT NOT NULL CHECK (judgment_cutoff_ms >= 0),
          latest_fact_at_ms BIGINT NOT NULL CHECK (latest_fact_at_ms >= 0),
          judgment_json JSONB NOT NULL
            CHECK (
              jsonb_typeof(judgment_json) = 'object'
              AND judgment_json ->> 'schema_version' = 'macro_daily_judgment_v2'
            ),
          memo_text TEXT NOT NULL CHECK (btrim(memo_text) <> ''),
          schema_version TEXT NOT NULL
            CHECK (schema_version = 'macro_daily_judgment_v2'),
          compiler_version TEXT NOT NULL CHECK (btrim(compiler_version) <> ''),
          payload_hash TEXT NOT NULL
            CONSTRAINT macro_daily_judgments_v2_payload_hash_key UNIQUE
            CHECK (btrim(payload_hash) <> ''),
          published_at_ms BIGINT NOT NULL CHECK (published_at_ms >= judgment_cutoff_ms)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE macro_event_updates (
          event_update_id TEXT
            CONSTRAINT macro_event_updates_v2_pkey PRIMARY KEY
            CHECK (btrim(event_update_id) <> ''),
          session_date DATE NOT NULL,
          evidence_pack_id TEXT NOT NULL
            REFERENCES macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT,
          trigger_release_fact_id TEXT NOT NULL
            REFERENCES macro_release_facts(release_fact_id) ON DELETE RESTRICT,
          update_json JSONB NOT NULL CHECK (jsonb_typeof(update_json) = 'object'),
          payload_hash TEXT NOT NULL
            CONSTRAINT macro_event_updates_v2_payload_hash_key UNIQUE
            CHECK (btrim(payload_hash) <> ''),
          published_at_ms BIGINT NOT NULL CHECK (published_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE macro_research_runs (
          session_date DATE
            CONSTRAINT macro_research_runs_v2_pkey PRIMARY KEY,
          market_cutoff_ms BIGINT NOT NULL CHECK (market_cutoff_ms >= 0),
          evidence_pack_id TEXT NOT NULL
            REFERENCES macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT,
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'running', 'retryable', 'failed', 'published')),
          sealed_at_ms BIGINT NOT NULL CHECK (sealed_at_ms >= market_cutoff_ms),
          attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
          max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
          due_at_ms BIGINT NOT NULL CHECK (due_at_ms >= 0),
          leased_until_ms BIGINT,
          lease_owner TEXT,
          reviewer_disposition TEXT
            CHECK (reviewer_disposition IS NULL OR reviewer_disposition IN ('pass', 'revise', 'block')),
          last_error_code TEXT,
          last_error_message TEXT,
          created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= created_at_ms),
          CONSTRAINT macro_research_runs_v2_session_cutoff_key
            UNIQUE (session_date, market_cutoff_ms),
          CONSTRAINT macro_research_runs_v2_lease_shape_check CHECK (
            (
              status = 'running'
              AND leased_until_ms IS NOT NULL
              AND btrim(COALESCE(lease_owner, '')) <> ''
            )
            OR (
              status <> 'running'
              AND leased_until_ms IS NULL
              AND lease_owner IS NULL
            )
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_research_runs_v2_due
          ON macro_research_runs(status, due_at_ms, session_date)
          WHERE status IN ('pending', 'retryable', 'running')
        """
    )
    op.execute(
        """
        CREATE TRIGGER macro_research_runs_lifecycle
        BEFORE INSERT OR UPDATE OR DELETE ON macro_research_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_macro_research_run_lifecycle()
        """
    )
    op.execute(
        """
        CREATE TABLE macro_research_publications (
          session_date DATE
            CONSTRAINT macro_research_publications_v2_pkey PRIMARY KEY,
          market_cutoff_ms BIGINT NOT NULL CHECK (market_cutoff_ms >= 0),
          evidence_pack_id TEXT NOT NULL
            REFERENCES macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT,
          artifact_json JSONB NOT NULL CHECK (jsonb_typeof(artifact_json) = 'object'),
          report_markdown TEXT NOT NULL CHECK (btrim(report_markdown) <> ''),
          audit_json JSONB NOT NULL CHECK (jsonb_typeof(audit_json) = 'object'),
          reviewer_disposition TEXT NOT NULL
            CHECK (reviewer_disposition IN ('pass', 'revise', 'block')),
          model_name TEXT NOT NULL CHECK (btrim(model_name) <> ''),
          prompt_version TEXT NOT NULL CHECK (btrim(prompt_version) <> ''),
          workflow_version TEXT NOT NULL CHECK (btrim(workflow_version) <> ''),
          artifact_hash TEXT NOT NULL
            CONSTRAINT macro_research_publications_v2_artifact_hash_key UNIQUE
            CHECK (btrim(artifact_hash) <> ''),
          published_at_ms BIGINT NOT NULL CHECK (published_at_ms >= market_cutoff_ms),
          CONSTRAINT macro_research_publications_v2_run_fkey
            FOREIGN KEY (session_date, market_cutoff_ms)
            REFERENCES macro_research_runs(session_date, market_cutoff_ms)
            ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_research_publications_v2_latest
          ON macro_research_publications(session_date DESC)
        """
    )
    for table_name in (
        "macro_evidence_packs",
        "macro_daily_judgments",
        "macro_event_updates",
        "macro_research_publications",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_append_only
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation()
            """
        )


def _extend_document_types() -> None:
    op.execute(
        """
        ALTER TABLE macro_documents
          DROP CONSTRAINT IF EXISTS macro_documents_document_type_check
        """
    )
    op.execute(
        """
        ALTER TABLE macro_documents
          ADD CONSTRAINT macro_documents_document_type_check
          CHECK (document_type IN (
            'statement', 'implementation', 'minutes', 'sep', 'speech',
            'auction', 'survey', 'calendar'
          ))
        """
    )


def _create_fed_evidence_tables() -> None:
    op.execute(
        """
        CREATE TABLE macro_fed_official_role_facts (
          role_fact_id TEXT PRIMARY KEY CHECK (btrim(role_fact_id) <> ''),
          dataset_id TEXT NOT NULL
            CHECK (dataset_id = 'federal_reserve.fomc.roster'),
          official_id TEXT NOT NULL CHECK (btrim(official_id) <> ''),
          official_name TEXT NOT NULL CHECK (btrim(official_name) <> ''),
          role_title TEXT NOT NULL CHECK (btrim(role_title) <> ''),
          organization TEXT NOT NULL CHECK (btrim(organization) <> ''),
          effective_start DATE NOT NULL,
          effective_end DATE,
          fomc_participant BOOLEAN NOT NULL,
          fomc_voter BOOLEAN NOT NULL,
          source_url TEXT NOT NULL CHECK (btrim(source_url) <> ''),
          received_at_ms BIGINT NOT NULL CHECK (received_at_ms >= 0),
          fact_hash TEXT NOT NULL CHECK (btrim(fact_hash) <> ''),
          raw_data_json JSONB NOT NULL CHECK (jsonb_typeof(raw_data_json) = 'object'),
          CHECK (effective_end IS NULL OR effective_end >= effective_start),
          UNIQUE (official_id, role_title, effective_start, fact_hash)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_fed_official_role_effective
          ON macro_fed_official_role_facts(
            official_id, effective_start DESC, effective_end
          )
        """
    )
    op.execute(
        """
        CREATE TABLE macro_document_analyses (
          analysis_id TEXT PRIMARY KEY CHECK (btrim(analysis_id) <> ''),
          document_id TEXT NOT NULL
            REFERENCES macro_documents(document_id) ON DELETE RESTRICT,
          document_hash TEXT NOT NULL CHECK (btrim(document_hash) <> ''),
          official_id TEXT,
          policy_relevance TEXT NOT NULL
            CHECK (policy_relevance IN ('policy_signal', 'not_policy_signal', 'uncertain')),
          stance TEXT NOT NULL
            CHECK (stance IN ('hawkish', 'neutral', 'dovish', 'mixed', 'no_call')),
          confidence DOUBLE PRECISION
            CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
          analysis_json JSONB NOT NULL CHECK (jsonb_typeof(analysis_json) = 'object'),
          model_name TEXT NOT NULL CHECK (btrim(model_name) <> ''),
          prompt_version TEXT NOT NULL CHECK (btrim(prompt_version) <> ''),
          reviewer_disposition TEXT NOT NULL
            CHECK (reviewer_disposition IN ('pass', 'revise', 'block')),
          created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= 0),
          payload_hash TEXT NOT NULL UNIQUE CHECK (btrim(payload_hash) <> ''),
          UNIQUE (document_id, document_hash, model_name, prompt_version)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_document_analyses_document
          ON macro_document_analyses(document_id, created_at_ms DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE macro_document_analysis_jobs (
          analysis_job_id TEXT PRIMARY KEY CHECK (btrim(analysis_job_id) <> ''),
          document_id TEXT NOT NULL
            REFERENCES macro_documents(document_id) ON DELETE RESTRICT,
          document_hash TEXT NOT NULL CHECK (btrim(document_hash) <> ''),
          model_name TEXT NOT NULL CHECK (btrim(model_name) <> ''),
          prompt_version TEXT NOT NULL CHECK (btrim(prompt_version) <> ''),
          status TEXT NOT NULL
            CHECK (status IN ('pending', 'claimed', 'retryable', 'failed', 'completed')),
          next_due_at_ms BIGINT NOT NULL CHECK (next_due_at_ms >= 0),
          leased_until_ms BIGINT,
          lease_owner TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
          max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
          last_error_code TEXT,
          created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= 0),
          UNIQUE (document_id, document_hash, model_name, prompt_version),
          CHECK (
            (status = 'claimed' AND leased_until_ms IS NOT NULL AND btrim(lease_owner) <> '')
            OR
            (status <> 'claimed' AND leased_until_ms IS NULL AND lease_owner IS NULL)
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_document_analysis_jobs_due
          ON macro_document_analysis_jobs(status, next_due_at_ms, analysis_job_id)
        """
    )
    for table_name in ("macro_fed_official_role_facts", "macro_document_analyses"):
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_append_only
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation()
            """
        )


def _retire_generic_document_target() -> None:
    op.execute(
        """
        DELETE FROM macro_acquisition_targets
        WHERE dataset_id = 'federal_reserve.monetary_policy.documents'
        """
    )
