"""Hard-cut Macro into one sealed Thesis, independent review, delta, and replay."""

from __future__ import annotations

from alembic import op

revision = "20260728_0212"
down_revision = "20260728_0211"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS macro_event_updates_v1_archive CASCADE;
        DROP TABLE IF EXISTS macro_event_updates CASCADE;
        DROP TABLE IF EXISTS macro_research_publications_v1_archive CASCADE;
        DROP TABLE IF EXISTS macro_research_publications CASCADE;
        DROP TABLE IF EXISTS macro_research_runs_v1_archive CASCADE;
        DROP TABLE IF EXISTS macro_research_runs CASCADE;
        DROP TABLE IF EXISTS macro_daily_judgments_v1_archive CASCADE;
        DROP TABLE IF EXISTS macro_daily_judgments CASCADE;
        DROP TABLE IF EXISTS macro_judgment_status CASCADE;
        DROP TABLE IF EXISTS macro_evidence_packs_v1_archive CASCADE;
        DROP TABLE IF EXISTS macro_evidence_packs CASCADE;
        DROP FUNCTION IF EXISTS enforce_macro_research_run_lifecycle() CASCADE;
        """
    )
    op.execute(
        """
        DELETE FROM macro_source_receipts
        WHERE dataset_id = ANY (
          ARRAY[
            'cme.rates.futures.curves',
            'licensed.credit.trace_nav',
            'licensed.credit.ice_bofa_full_history'
          ]
        );
        DELETE FROM macro_acquisition_targets
        WHERE dataset_id = ANY (
          ARRAY[
            'cme.rates.futures.curves',
            'licensed.credit.trace_nav',
            'licensed.credit.ice_bofa_full_history'
          ]
        );
        DELETE FROM macro_documents
        WHERE dataset_id = ANY (
          ARRAY[
            'cme.rates.futures.curves',
            'licensed.credit.trace_nav',
            'licensed.credit.ice_bofa_full_history'
          ]
        );
        DELETE FROM macro_series_facts
        WHERE dataset_id = ANY (
          ARRAY[
            'cme.rates.futures.curves',
            'licensed.credit.trace_nav',
            'licensed.credit.ice_bofa_full_history'
          ]
        );
        DELETE FROM macro_release_facts
        WHERE dataset_id = ANY (
          ARRAY[
            'cme.rates.futures.curves',
            'licensed.credit.trace_nav',
            'licensed.credit.ice_bofa_full_history'
          ]
        );
        """
    )
    op.execute(
        """
        ALTER TABLE macro_release_facts
        DROP CONSTRAINT macro_release_facts_check;
        ALTER TABLE macro_release_facts
        DROP CONSTRAINT macro_release_facts_published_at_ms_check;
        ALTER TABLE macro_release_facts
        ALTER COLUMN published_at_ms DROP NOT NULL;
        ALTER TABLE macro_release_facts
        ADD CONSTRAINT macro_release_facts_clock_check
        CHECK (
          received_at_ms >= 0
          AND (published_at_ms IS NULL OR (
            published_at_ms >= 0
            AND received_at_ms >= published_at_ms
          ))
        );
        """
    )
    op.execute("DELETE FROM macro_module_current")
    op.execute(
        """
        ALTER TABLE macro_module_current
        DROP CONSTRAINT macro_module_current_data_health_state_check;
        ALTER TABLE macro_module_current
        DROP CONSTRAINT macro_module_current_typed_schema_check;
        ALTER TABLE macro_module_current
        RENAME COLUMN data_health_state TO current_health_state;
        ALTER TABLE macro_module_current
        ADD COLUMN history_depth_state text NOT NULL;
        ALTER TABLE macro_module_current
        ADD CONSTRAINT macro_module_current_health_check
        CHECK (current_health_state IN ('current', 'degraded', 'unavailable'));
        ALTER TABLE macro_module_current
        ADD CONSTRAINT macro_module_current_history_depth_check
        CHECK (history_depth_state IN ('complete', 'partial', 'insufficient', 'not_required'));
        ALTER TABLE macro_module_current
        ADD CONSTRAINT macro_module_current_typed_schema_check
        CHECK (
          payload_json ->> 'schema_version' = CASE module_id
            WHEN 'rates_fed' THEN 'macro_rates_fed_v4'
            WHEN 'economy_inflation' THEN 'macro_economy_inflation_v4'
            WHEN 'liquidity_funding' THEN 'macro_liquidity_funding_v4'
            WHEN 'credit' THEN 'macro_credit_v5'
            WHEN 'volatility' THEN 'macro_volatility_v4'
            WHEN 'cross_asset' THEN 'macro_cross_asset_v5'
            ELSE NULL
          END
        );
        """
    )
    op.execute(
        """
        CREATE TABLE macro_evidence_packs (
          evidence_pack_id text PRIMARY KEY,
          session_date date NOT NULL UNIQUE,
          cutoff_ms bigint NOT NULL CHECK (cutoff_ms >= 0),
          sealed_at_ms bigint NOT NULL CHECK (sealed_at_ms >= cutoff_ms),
          source_max_received_at_ms bigint NOT NULL
            CHECK (source_max_received_at_ms >= 0 AND source_max_received_at_ms <= cutoff_ms),
          schema_version text NOT NULL CHECK (schema_version = 'macro_evidence_pack_v3'),
          payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
          payload_hash text NOT NULL UNIQUE CHECK (btrim(payload_hash) <> '')
        );

        CREATE TABLE macro_thesis_runs (
          session_date date PRIMARY KEY,
          cutoff_ms bigint NOT NULL CHECK (cutoff_ms >= 0),
          evidence_pack_id text NOT NULL REFERENCES macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT,
          evidence_pack_hash text NOT NULL CHECK (btrim(evidence_pack_hash) <> ''),
          status text NOT NULL DEFAULT 'pending'
            CHECK (status IN (
              'pending', 'running', 'retryable', 'failed',
              'config_error', 'blocked', 'published'
            )),
          attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
          max_attempts integer NOT NULL CHECK (max_attempts > 0),
          due_at_ms bigint NOT NULL CHECK (due_at_ms >= cutoff_ms),
          leased_until_ms bigint,
          lease_owner text,
          publication_id text,
          last_error_code text,
          last_error_message text,
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= cutoff_ms),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= created_at_ms),
          CHECK (
            (status = 'running' AND leased_until_ms IS NOT NULL AND btrim(COALESCE(lease_owner, '')) <> '')
            OR
            (status <> 'running' AND leased_until_ms IS NULL AND lease_owner IS NULL)
          ),
          CHECK (
            (status = 'published' AND publication_id IS NOT NULL)
            OR
            (status <> 'published' AND publication_id IS NULL)
          )
        );

        CREATE TABLE macro_thesis_reviews (
          review_id text PRIMARY KEY,
          session_date date NOT NULL REFERENCES macro_thesis_runs(session_date) ON DELETE RESTRICT,
          review_sequence integer NOT NULL CHECK (review_sequence IN (1, 2)),
          draft_hash text NOT NULL CHECK (btrim(draft_hash) <> ''),
          disposition text NOT NULL CHECK (disposition IN ('pass', 'revise', 'block')),
          review_json jsonb NOT NULL CHECK (jsonb_typeof(review_json) = 'object'),
          invocation_id text NOT NULL UNIQUE CHECK (btrim(invocation_id) <> ''),
          model_name text NOT NULL CHECK (btrim(model_name) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          UNIQUE (session_date, review_sequence)
        );

        CREATE TABLE macro_thesis_publications (
          publication_id text PRIMARY KEY,
          session_date date NOT NULL UNIQUE REFERENCES macro_thesis_runs(session_date) ON DELETE RESTRICT,
          cutoff_ms bigint NOT NULL CHECK (cutoff_ms >= 0),
          evidence_pack_id text NOT NULL REFERENCES macro_evidence_packs(evidence_pack_id) ON DELETE RESTRICT,
          schema_version text NOT NULL CHECK (schema_version = 'macro_thesis_v1'),
          thesis_json jsonb NOT NULL CHECK (jsonb_typeof(thesis_json) = 'object'),
          thesis_hash text NOT NULL UNIQUE CHECK (btrim(thesis_hash) <> ''),
          reviewer_invocation_id text NOT NULL REFERENCES macro_thesis_reviews(invocation_id) ON DELETE RESTRICT,
          reviewer_draft_hash text NOT NULL CHECK (btrim(reviewer_draft_hash) <> ''),
          published_at_ms bigint NOT NULL CHECK (published_at_ms >= cutoff_ms)
        );

        ALTER TABLE macro_thesis_runs
        ADD CONSTRAINT macro_thesis_runs_publication_fk
        FOREIGN KEY (publication_id)
        REFERENCES macro_thesis_publications(publication_id)
        DEFERRABLE INITIALLY DEFERRED;

        CREATE TABLE macro_live_deltas (
          live_delta_id text PRIMARY KEY,
          publication_id text NOT NULL
            REFERENCES macro_thesis_publications(publication_id) ON DELETE RESTRICT,
          evaluated_at_ms bigint NOT NULL CHECK (evaluated_at_ms >= 0),
          module_fact_cutoff_ms bigint NOT NULL CHECK (module_fact_cutoff_ms >= 0),
          schema_version text NOT NULL CHECK (schema_version = 'macro_live_delta_v1'),
          status text NOT NULL
            CHECK (status IN (
              'confirming', 'weakening', 'invalidation_triggered',
              'unrelated', 'insufficient'
            )),
          payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
          input_hash text NOT NULL UNIQUE CHECK (btrim(input_hash) <> '')
        );

        CREATE TABLE macro_outcome_replays (
          replay_id text PRIMARY KEY,
          publication_id text NOT NULL
            REFERENCES macro_thesis_publications(publication_id) ON DELETE RESTRICT,
          evaluated_at_ms bigint NOT NULL CHECK (evaluated_at_ms >= 0),
          schema_version text NOT NULL CHECK (schema_version = 'macro_outcome_replay_v1'),
          payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
          input_hash text NOT NULL UNIQUE CHECK (btrim(input_hash) <> '')
        );

        CREATE INDEX idx_macro_thesis_runs_due
          ON macro_thesis_runs(status, due_at_ms, session_date)
          WHERE status IN ('pending', 'running', 'retryable');
        CREATE INDEX idx_macro_thesis_publications_latest
          ON macro_thesis_publications(session_date DESC);
        CREATE INDEX idx_macro_live_deltas_latest
          ON macro_live_deltas(publication_id, evaluated_at_ms DESC);
        CREATE INDEX idx_macro_outcome_replays_latest
          ON macro_outcome_replays(publication_id, evaluated_at_ms DESC);
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_macro_thesis_run_lifecycle()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'macro_thesis_run_delete_forbidden';
          END IF;
          IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'pending' OR NEW.attempt_count <> 0 THEN
              RAISE EXCEPTION 'macro_thesis_run_initial_state_invalid';
            END IF;
            RETURN NEW;
          END IF;
          IF (
            NEW.session_date,
            NEW.cutoff_ms,
            NEW.evidence_pack_id,
            NEW.evidence_pack_hash,
            NEW.max_attempts,
            NEW.created_at_ms
          ) IS DISTINCT FROM (
            OLD.session_date,
            OLD.cutoff_ms,
            OLD.evidence_pack_id,
            OLD.evidence_pack_hash,
            OLD.max_attempts,
            OLD.created_at_ms
          ) THEN
            RAISE EXCEPTION 'macro_thesis_run_frozen_fields_immutable';
          END IF;
          IF OLD.status IN ('failed', 'config_error', 'blocked', 'published') THEN
            RAISE EXCEPTION 'macro_thesis_run_terminal';
          END IF;
          IF NOT (
            (OLD.status IN ('pending', 'retryable') AND NEW.status = 'running')
            OR (OLD.status = 'running' AND NEW.status IN (
              'running', 'retryable', 'failed', 'config_error', 'blocked', 'published'
            ))
          ) THEN
            RAISE EXCEPTION 'macro_thesis_run_transition_invalid:%->%', OLD.status, NEW.status;
          END IF;
          IF NEW.attempt_count < OLD.attempt_count THEN
            RAISE EXCEPTION 'macro_thesis_run_attempt_count_decrease';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE TRIGGER macro_thesis_runs_lifecycle
        BEFORE INSERT OR UPDATE OR DELETE ON macro_thesis_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_macro_thesis_run_lifecycle();

        CREATE TRIGGER macro_evidence_packs_append_only
        BEFORE UPDATE OR DELETE ON macro_evidence_packs
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation();
        CREATE TRIGGER macro_thesis_reviews_append_only
        BEFORE UPDATE OR DELETE ON macro_thesis_reviews
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation();
        CREATE TRIGGER macro_thesis_publications_append_only
        BEFORE UPDATE OR DELETE ON macro_thesis_publications
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation();
        CREATE TRIGGER macro_live_deltas_append_only
        BEFORE UPDATE OR DELETE ON macro_live_deltas
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation();
        CREATE TRIGGER macro_outcome_replays_append_only
        BEFORE UPDATE OR DELETE ON macro_outcome_replays
        FOR EACH ROW EXECUTE FUNCTION reject_macro_fact_mutation();
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260728_0212 is an irreversible Macro Thesis contract hard cut")
