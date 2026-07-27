"""Replace the legacy Macro bundle chain with the clock-driven decision system."""

from __future__ import annotations

from alembic import op

revision = "20260727_0200"
down_revision = "20260727_0199"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30min'")
    _drop_legacy_macro()
    _create_general_market_facts()
    _create_macro_facts()
    _create_acquisition_control_plane()
    _create_decision_read_models()
    _create_research_lane()
    _create_immutability_contract()


def downgrade() -> None:
    raise RuntimeError(
        "20260727_0200 is an irreversible Macro fact-model hard cut; apply a forward fix"
    )


def _drop_legacy_macro() -> None:
    op.execute("DROP TABLE IF EXISTS macro_research_publications CASCADE")
    op.execute("DROP TABLE IF EXISTS macro_research_runs CASCADE")
    op.execute("DROP FUNCTION IF EXISTS enforce_macro_research_run_lifecycle() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS reject_macro_research_publication_mutation() CASCADE")
    op.execute("DROP TABLE IF EXISTS macro_sync_windows CASCADE")
    op.execute("DROP TABLE IF EXISTS macro_sync_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS macro_sync_state CASCADE")
    op.execute("DROP TABLE IF EXISTS macro_observations CASCADE")
    op.execute(
        """
        DELETE FROM checkpoint_writes
        WHERE thread_id LIKE 'macro-research:%'
           OR thread_id LIKE 'macro_research:%'
        """
    )
    op.execute(
        """
        DELETE FROM checkpoint_blobs
        WHERE thread_id LIKE 'macro-research:%'
           OR thread_id LIKE 'macro_research:%'
        """
    )
    op.execute(
        """
        DELETE FROM checkpoints
        WHERE thread_id LIKE 'macro-research:%'
           OR thread_id LIKE 'macro_research:%'
        """
    )


def _create_general_market_facts() -> None:
    op.execute(
        """
        CREATE TABLE market_instruments (
          instrument_id TEXT PRIMARY KEY CHECK (btrim(instrument_id) <> ''),
          symbol TEXT NOT NULL CHECK (btrim(symbol) <> ''),
          name TEXT NOT NULL CHECK (btrim(name) <> ''),
          asset_class TEXT NOT NULL
            CHECK (asset_class IN (
              'equity', 'rates', 'credit', 'fx', 'commodity', 'crypto', 'volatility'
            )),
          instrument_type TEXT NOT NULL
            CHECK (instrument_type IN (
              'index', 'etf', 'spot', 'future', 'rate', 'spread'
            )),
          venue TEXT NOT NULL CHECK (btrim(venue) <> ''),
          currency TEXT NOT NULL CHECK (btrim(currency) <> ''),
          price_unit TEXT NOT NULL CHECK (btrim(price_unit) <> ''),
          source_metadata_json JSONB NOT NULL DEFAULT '{}'
            CHECK (jsonb_typeof(source_metadata_json) = 'object'),
          created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE market_observations (
          observation_id TEXT PRIMARY KEY CHECK (btrim(observation_id) <> ''),
          instrument_id TEXT NOT NULL
            REFERENCES market_instruments(instrument_id) ON DELETE RESTRICT,
          dataset_id TEXT NOT NULL CHECK (btrim(dataset_id) <> ''),
          source_id TEXT NOT NULL CHECK (btrim(source_id) <> ''),
          field_name TEXT NOT NULL CHECK (btrim(field_name) <> ''),
          value_numeric DOUBLE PRECISION NOT NULL
            CHECK (
              value_numeric <> 'NaN'::double precision
              AND value_numeric <> 'Infinity'::double precision
              AND value_numeric <> '-Infinity'::double precision
            ),
          unit TEXT NOT NULL CHECK (btrim(unit) <> ''),
          observed_at_ms BIGINT NOT NULL CHECK (observed_at_ms >= 0),
          published_at_ms BIGINT CHECK (published_at_ms IS NULL OR published_at_ms >= 0),
          received_at_ms BIGINT NOT NULL CHECK (received_at_ms >= 0),
          trust_tier TEXT NOT NULL
            CHECK (trust_tier IN ('official', 'exchange', 'untrusted_proxy')),
          source_url TEXT NOT NULL CHECK (btrim(source_url) <> ''),
          fact_hash TEXT NOT NULL CHECK (btrim(fact_hash) <> ''),
          raw_data_json JSONB NOT NULL CHECK (jsonb_typeof(raw_data_json) = 'object'),
          CONSTRAINT market_observations_clock_order CHECK (
            published_at_ms IS NULL OR published_at_ms <= received_at_ms
          )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_market_observations_natural_fact
          ON market_observations(
            dataset_id, instrument_id, field_name, observed_at_ms, fact_hash
          )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_market_observations_latest
          ON market_observations(instrument_id, field_name, observed_at_ms DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE market_settlements (
          settlement_id TEXT PRIMARY KEY CHECK (btrim(settlement_id) <> ''),
          instrument_id TEXT NOT NULL
            REFERENCES market_instruments(instrument_id) ON DELETE RESTRICT,
          dataset_id TEXT NOT NULL CHECK (btrim(dataset_id) <> ''),
          source_id TEXT NOT NULL CHECK (btrim(source_id) <> ''),
          trade_date DATE NOT NULL,
          contract_code TEXT NOT NULL CHECK (btrim(contract_code) <> ''),
          settlement_price DOUBLE PRECISION NOT NULL CHECK (
            settlement_price <> 'NaN'::double precision
            AND settlement_price <> 'Infinity'::double precision
            AND settlement_price <> '-Infinity'::double precision
          ),
          open_interest DOUBLE PRECISION CHECK (
            open_interest IS NULL OR (
              open_interest <> 'NaN'::double precision
              AND open_interest <> 'Infinity'::double precision
              AND open_interest <> '-Infinity'::double precision
              AND open_interest >= 0
            )
          ),
          volume DOUBLE PRECISION CHECK (
            volume IS NULL OR (
              volume <> 'NaN'::double precision
              AND volume <> 'Infinity'::double precision
              AND volume <> '-Infinity'::double precision
              AND volume >= 0
            )
          ),
          unit TEXT NOT NULL CHECK (btrim(unit) <> ''),
          published_at_ms BIGINT CHECK (published_at_ms IS NULL OR published_at_ms >= 0),
          received_at_ms BIGINT NOT NULL CHECK (
            received_at_ms >= 0
            AND (published_at_ms IS NULL OR received_at_ms >= published_at_ms)
          ),
          source_url TEXT NOT NULL CHECK (btrim(source_url) <> ''),
          fact_hash TEXT NOT NULL CHECK (btrim(fact_hash) <> ''),
          raw_data_json JSONB NOT NULL CHECK (jsonb_typeof(raw_data_json) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_market_settlements_natural_fact
          ON market_settlements(dataset_id, instrument_id, trade_date, contract_code, fact_hash)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_market_settlements_latest
          ON market_settlements(instrument_id, trade_date DESC, contract_code)
        """
    )
    op.execute(
        """
        CREATE TABLE market_position_facts (
          position_fact_id TEXT PRIMARY KEY CHECK (btrim(position_fact_id) <> ''),
          dataset_id TEXT NOT NULL CHECK (btrim(dataset_id) <> ''),
          contract_code TEXT NOT NULL CHECK (btrim(contract_code) <> ''),
          contract_name TEXT NOT NULL CHECK (btrim(contract_name) <> ''),
          report_date DATE NOT NULL,
          open_interest DOUBLE PRECISION NOT NULL CHECK (
            open_interest >= 0
            AND open_interest <> 'NaN'::double precision
            AND open_interest <> 'Infinity'::double precision
          ),
          leveraged_long DOUBLE PRECISION NOT NULL CHECK (
            leveraged_long >= 0
            AND leveraged_long <> 'NaN'::double precision
            AND leveraged_long <> 'Infinity'::double precision
          ),
          leveraged_short DOUBLE PRECISION NOT NULL CHECK (
            leveraged_short >= 0
            AND leveraged_short <> 'NaN'::double precision
            AND leveraged_short <> 'Infinity'::double precision
          ),
          leveraged_net_pct_oi DOUBLE PRECISION NOT NULL CHECK (
            leveraged_net_pct_oi <> 'NaN'::double precision
            AND leveraged_net_pct_oi <> 'Infinity'::double precision
            AND leveraged_net_pct_oi <> '-Infinity'::double precision
          ),
          asset_manager_net_pct_oi DOUBLE PRECISION NOT NULL CHECK (
            asset_manager_net_pct_oi <> 'NaN'::double precision
            AND asset_manager_net_pct_oi <> 'Infinity'::double precision
            AND asset_manager_net_pct_oi <> '-Infinity'::double precision
          ),
          dealer_net_pct_oi DOUBLE PRECISION NOT NULL CHECK (
            dealer_net_pct_oi <> 'NaN'::double precision
            AND dealer_net_pct_oi <> 'Infinity'::double precision
            AND dealer_net_pct_oi <> '-Infinity'::double precision
          ),
          published_at_ms BIGINT CHECK (published_at_ms IS NULL OR published_at_ms >= 0),
          received_at_ms BIGINT NOT NULL CHECK (
            received_at_ms >= 0
            AND (published_at_ms IS NULL OR received_at_ms >= published_at_ms)
          ),
          source_url TEXT NOT NULL CHECK (btrim(source_url) <> ''),
          fact_hash TEXT NOT NULL CHECK (btrim(fact_hash) <> ''),
          raw_data_json JSONB NOT NULL CHECK (jsonb_typeof(raw_data_json) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_market_position_facts_natural_fact
          ON market_position_facts(dataset_id, contract_code, report_date, fact_hash)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_market_position_facts_latest
          ON market_position_facts(dataset_id, contract_code, report_date DESC)
        """
    )


def _create_macro_facts() -> None:
    op.execute(
        """
        CREATE TABLE macro_series_facts (
          fact_id TEXT PRIMARY KEY CHECK (btrim(fact_id) <> ''),
          dataset_id TEXT NOT NULL CHECK (btrim(dataset_id) <> ''),
          series_id TEXT NOT NULL CHECK (btrim(series_id) <> ''),
          reference_date DATE NOT NULL,
          vintage_date DATE NOT NULL,
          value_numeric DOUBLE PRECISION CHECK (
            value_numeric IS NULL OR (
              value_numeric <> 'NaN'::double precision
              AND value_numeric <> 'Infinity'::double precision
              AND value_numeric <> '-Infinity'::double precision
            )
          ),
          value_text TEXT,
          unit TEXT NOT NULL CHECK (btrim(unit) <> ''),
          published_at_ms BIGINT CHECK (published_at_ms IS NULL OR published_at_ms >= 0),
          received_at_ms BIGINT NOT NULL CHECK (
            received_at_ms >= 0
            AND (published_at_ms IS NULL OR received_at_ms >= published_at_ms)
          ),
          source_url TEXT NOT NULL CHECK (btrim(source_url) <> ''),
          fact_hash TEXT NOT NULL CHECK (btrim(fact_hash) <> ''),
          raw_data_json JSONB NOT NULL CHECK (jsonb_typeof(raw_data_json) = 'object'),
          CONSTRAINT macro_series_facts_value_shape CHECK (
            (value_numeric IS NOT NULL AND value_text IS NULL)
            OR (value_numeric IS NULL AND btrim(COALESCE(value_text, '')) <> '')
          )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_macro_series_facts_natural_fact
          ON macro_series_facts(
            dataset_id, series_id, reference_date, fact_hash
          )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_series_facts_latest
          ON macro_series_facts(dataset_id, series_id, reference_date DESC, vintage_date DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE macro_release_facts (
          release_fact_id TEXT PRIMARY KEY CHECK (btrim(release_fact_id) <> ''),
          dataset_id TEXT NOT NULL CHECK (btrim(dataset_id) <> ''),
          release_id TEXT NOT NULL CHECK (btrim(release_id) <> ''),
          series_id TEXT NOT NULL CHECK (btrim(series_id) <> ''),
          reference_period TEXT NOT NULL CHECK (btrim(reference_period) <> ''),
          scheduled_at_ms BIGINT CHECK (scheduled_at_ms IS NULL OR scheduled_at_ms >= 0),
          published_at_ms BIGINT NOT NULL CHECK (published_at_ms >= 0),
          received_at_ms BIGINT NOT NULL CHECK (received_at_ms >= published_at_ms),
          actual_value DOUBLE PRECISION CHECK (
            actual_value IS NULL OR (
              actual_value <> 'NaN'::double precision
              AND actual_value <> 'Infinity'::double precision
              AND actual_value <> '-Infinity'::double precision
            )
          ),
          prior_value DOUBLE PRECISION CHECK (
            prior_value IS NULL OR (
              prior_value <> 'NaN'::double precision
              AND prior_value <> 'Infinity'::double precision
              AND prior_value <> '-Infinity'::double precision
            )
          ),
          revised_prior_value DOUBLE PRECISION CHECK (
            revised_prior_value IS NULL OR (
              revised_prior_value <> 'NaN'::double precision
              AND revised_prior_value <> 'Infinity'::double precision
              AND revised_prior_value <> '-Infinity'::double precision
            )
          ),
          estimate_value DOUBLE PRECISION CHECK (
            estimate_value IS NULL OR (
              estimate_value <> 'NaN'::double precision
              AND estimate_value <> 'Infinity'::double precision
              AND estimate_value <> '-Infinity'::double precision
            )
          ),
          unit TEXT NOT NULL CHECK (btrim(unit) <> ''),
          importance_tier SMALLINT NOT NULL CHECK (importance_tier BETWEEN 1 AND 3),
          source_url TEXT NOT NULL CHECK (btrim(source_url) <> ''),
          fact_hash TEXT NOT NULL CHECK (btrim(fact_hash) <> ''),
          raw_data_json JSONB NOT NULL CHECK (jsonb_typeof(raw_data_json) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_macro_release_facts_natural_fact
          ON macro_release_facts(dataset_id, release_id, reference_period, fact_hash)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_release_facts_latest
          ON macro_release_facts(dataset_id, published_at_ms DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE macro_documents (
          document_id TEXT PRIMARY KEY CHECK (btrim(document_id) <> ''),
          dataset_id TEXT NOT NULL CHECK (btrim(dataset_id) <> ''),
          document_type TEXT NOT NULL
            CHECK (document_type IN (
              'statement', 'minutes', 'sep', 'speech', 'auction', 'survey', 'calendar'
            )),
          title TEXT NOT NULL CHECK (btrim(title) <> ''),
          effective_date DATE NOT NULL,
          published_at_ms BIGINT NOT NULL CHECK (published_at_ms >= 0),
          received_at_ms BIGINT NOT NULL CHECK (received_at_ms >= published_at_ms),
          source_url TEXT NOT NULL CHECK (btrim(source_url) <> ''),
          content_text TEXT NOT NULL CHECK (btrim(content_text) <> ''),
          fact_hash TEXT NOT NULL CHECK (btrim(fact_hash) <> ''),
          metadata_json JSONB NOT NULL CHECK (jsonb_typeof(metadata_json) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_documents_latest
          ON macro_documents(document_type, published_at_ms DESC)
        """
    )


def _create_acquisition_control_plane() -> None:
    op.execute(
        """
        CREATE TABLE macro_source_receipts (
          receipt_id TEXT PRIMARY KEY CHECK (btrim(receipt_id) <> ''),
          target_key TEXT NOT NULL CHECK (btrim(target_key) <> ''),
          dataset_id TEXT NOT NULL CHECK (btrim(dataset_id) <> ''),
          partition_key TEXT NOT NULL CHECK (btrim(partition_key) <> ''),
          started_at_ms BIGINT NOT NULL CHECK (started_at_ms >= 0),
          completed_at_ms BIGINT NOT NULL CHECK (completed_at_ms >= started_at_ms),
          status TEXT NOT NULL
            CHECK (status IN ('ok', 'not_modified', 'empty', 'failed', 'invalid')),
          http_status INTEGER,
          rows_seen INTEGER NOT NULL CHECK (rows_seen >= 0),
          rows_inserted INTEGER NOT NULL CHECK (rows_inserted >= 0),
          response_hash TEXT,
          error_code TEXT,
          error_message TEXT,
          diagnostics_json JSONB NOT NULL CHECK (jsonb_typeof(diagnostics_json) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_source_receipts_target
          ON macro_source_receipts(target_key, completed_at_ms DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE macro_acquisition_targets (
          target_key TEXT PRIMARY KEY CHECK (btrim(target_key) <> ''),
          dataset_id TEXT NOT NULL CHECK (btrim(dataset_id) <> ''),
          partition_key TEXT NOT NULL CHECK (btrim(partition_key) <> ''),
          clock_kind TEXT NOT NULL
            CHECK (clock_kind IN (
              'intraday_market', 'daily_settlement', 'scheduled_release',
              'official_state', 'official_document', 'backfill'
            )),
          cursor_json JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(cursor_json) = 'object'),
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN (
              'pending', 'claimed', 'current', 'delayed', 'stale', 'invalid',
              'unavailable', 'backfilling'
            )),
          next_due_at_ms BIGINT NOT NULL CHECK (next_due_at_ms >= 0),
          priority INTEGER NOT NULL DEFAULT 100,
          leased_until_ms BIGINT,
          lease_owner TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
          max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
          last_receipt_id TEXT REFERENCES macro_source_receipts(receipt_id) ON DELETE SET NULL,
          last_success_at_ms BIGINT,
          last_error_code TEXT,
          created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= created_at_ms),
          UNIQUE (dataset_id, partition_key),
          CONSTRAINT macro_acquisition_targets_lease_shape CHECK (
            (
              status = 'claimed'
              AND leased_until_ms IS NOT NULL
              AND btrim(COALESCE(lease_owner, '')) <> ''
            )
            OR (
              status <> 'claimed'
              AND leased_until_ms IS NULL
              AND lease_owner IS NULL
            )
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_acquisition_targets_due
          ON macro_acquisition_targets(
            clock_kind, priority, next_due_at_ms, target_key
          )
          WHERE status IN (
            'pending', 'current', 'delayed', 'stale', 'invalid', 'backfilling', 'claimed'
          )
        """
    )


def _create_decision_read_models() -> None:
    op.execute(
        """
        CREATE TABLE macro_feature_series (
          feature_id TEXT NOT NULL CHECK (btrim(feature_id) <> ''),
          as_of_date DATE NOT NULL,
          formula_version TEXT NOT NULL CHECK (btrim(formula_version) <> ''),
          value_numeric DOUBLE PRECISION NOT NULL CHECK (
            value_numeric <> 'NaN'::double precision
            AND value_numeric <> 'Infinity'::double precision
            AND value_numeric <> '-Infinity'::double precision
          ),
          unit TEXT NOT NULL CHECK (btrim(unit) <> ''),
          inputs_json JSONB NOT NULL CHECK (jsonb_typeof(inputs_json) = 'array'),
          payload_hash TEXT NOT NULL CHECK (btrim(payload_hash) <> ''),
          computed_at_ms BIGINT NOT NULL CHECK (computed_at_ms >= 0),
          PRIMARY KEY (feature_id, as_of_date)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE macro_module_current (
          module_id TEXT PRIMARY KEY
            CHECK (module_id IN (
              'rates_fed', 'economy_inflation', 'liquidity_funding',
              'credit', 'volatility', 'cross_asset'
            )),
          readiness TEXT NOT NULL CHECK (readiness IN ('ready', 'degraded', 'blocked')),
          fact_cutoff_ms BIGINT NOT NULL CHECK (fact_cutoff_ms >= 0),
          payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
          payload_hash TEXT NOT NULL CHECK (btrim(payload_hash) <> ''),
          updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE macro_evidence_packs (
          evidence_pack_id TEXT PRIMARY KEY CHECK (btrim(evidence_pack_id) <> ''),
          session_date DATE NOT NULL,
          judgment_cutoff_ms BIGINT NOT NULL CHECK (judgment_cutoff_ms >= 0),
          latest_fact_at_ms BIGINT NOT NULL CHECK (latest_fact_at_ms >= 0),
          schema_version TEXT NOT NULL CHECK (btrim(schema_version) <> ''),
          compiler_version TEXT NOT NULL CHECK (btrim(compiler_version) <> ''),
          payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
          payload_hash TEXT NOT NULL UNIQUE CHECK (btrim(payload_hash) <> ''),
          created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= judgment_cutoff_ms),
          UNIQUE (session_date, judgment_cutoff_ms)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE macro_daily_judgments (
          session_date DATE PRIMARY KEY,
          evidence_pack_id TEXT NOT NULL
            REFERENCES macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT,
          judgment_cutoff_ms BIGINT NOT NULL CHECK (judgment_cutoff_ms >= 0),
          latest_fact_at_ms BIGINT NOT NULL CHECK (latest_fact_at_ms >= 0),
          judgment_json JSONB NOT NULL CHECK (jsonb_typeof(judgment_json) = 'object'),
          memo_text TEXT NOT NULL CHECK (btrim(memo_text) <> ''),
          schema_version TEXT NOT NULL CHECK (btrim(schema_version) <> ''),
          compiler_version TEXT NOT NULL CHECK (btrim(compiler_version) <> ''),
          payload_hash TEXT NOT NULL UNIQUE CHECK (btrim(payload_hash) <> ''),
          published_at_ms BIGINT NOT NULL CHECK (published_at_ms >= judgment_cutoff_ms)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE macro_event_updates (
          event_update_id TEXT PRIMARY KEY CHECK (btrim(event_update_id) <> ''),
          session_date DATE NOT NULL,
          evidence_pack_id TEXT NOT NULL
            REFERENCES macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT,
          trigger_release_fact_id TEXT NOT NULL
            REFERENCES macro_release_facts(release_fact_id) ON DELETE RESTRICT,
          update_json JSONB NOT NULL CHECK (jsonb_typeof(update_json) = 'object'),
          payload_hash TEXT NOT NULL UNIQUE CHECK (btrim(payload_hash) <> ''),
          published_at_ms BIGINT NOT NULL CHECK (published_at_ms >= 0)
        )
        """
    )


def _create_research_lane() -> None:
    op.execute(
        """
        CREATE TABLE macro_research_runs (
          session_date DATE PRIMARY KEY,
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
          UNIQUE (session_date, market_cutoff_ms),
          CONSTRAINT macro_research_runs_lease_shape_check CHECK (
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
        CREATE INDEX idx_macro_research_runs_due
          ON macro_research_runs(status, due_at_ms, session_date)
          WHERE status IN ('pending', 'retryable', 'running')
        """
    )
    op.execute(
        """
        CREATE TABLE macro_research_publications (
          session_date DATE PRIMARY KEY,
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
          artifact_hash TEXT NOT NULL UNIQUE CHECK (btrim(artifact_hash) <> ''),
          published_at_ms BIGINT NOT NULL CHECK (published_at_ms >= market_cutoff_ms),
          FOREIGN KEY (session_date, market_cutoff_ms)
            REFERENCES macro_research_runs(session_date, market_cutoff_ms)
            ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_macro_research_publications_latest
          ON macro_research_publications(session_date DESC)
        """
    )


def _create_immutability_contract() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_macro_fact_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION '%_append_only', TG_TABLE_NAME;
        END
        $$
        """
    )
    for table_name in (
        "market_observations",
        "market_settlements",
        "market_position_facts",
        "macro_series_facts",
        "macro_release_facts",
        "macro_documents",
        "macro_source_receipts",
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
    op.execute(
        """
        CREATE FUNCTION enforce_macro_research_run_lifecycle()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'pending'
               OR NEW.attempt_count <> 0
               OR NEW.leased_until_ms IS NOT NULL
               OR NEW.lease_owner IS NOT NULL THEN
              RAISE EXCEPTION 'macro_research_run_initial_state_invalid';
            END IF;
            RETURN NEW;
          END IF;
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'macro_research_run_delete_forbidden';
          END IF;
          IF NEW.session_date IS DISTINCT FROM OLD.session_date
             OR NEW.market_cutoff_ms IS DISTINCT FROM OLD.market_cutoff_ms
             OR NEW.evidence_pack_id IS DISTINCT FROM OLD.evidence_pack_id
             OR NEW.sealed_at_ms IS DISTINCT FROM OLD.sealed_at_ms
             OR NEW.created_at_ms IS DISTINCT FROM OLD.created_at_ms THEN
            RAISE EXCEPTION 'macro_research_run_frozen_fields_immutable';
          END IF;
          IF OLD.status = 'failed' AND NEW.status = 'retryable' THEN
            RETURN NEW;
          END IF;
          IF OLD.status IN ('failed', 'published') THEN
            RAISE EXCEPTION 'macro_research_run_terminal';
          END IF;
          IF NOT (
            (OLD.status = 'pending' AND NEW.status = 'running')
            OR (OLD.status = 'retryable' AND NEW.status = 'running')
            OR (OLD.status = 'running' AND NEW.status IN (
              'running', 'retryable', 'failed', 'published'
            ))
          ) THEN
            RAISE EXCEPTION 'macro_research_run_transition_invalid:%->%', OLD.status, NEW.status;
          END IF;
          IF NEW.attempt_count < OLD.attempt_count THEN
            RAISE EXCEPTION 'macro_research_run_attempt_count_decrease';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER macro_research_runs_lifecycle
        BEFORE INSERT OR UPDATE OR DELETE ON macro_research_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_macro_research_run_lifecycle()
        """
    )
