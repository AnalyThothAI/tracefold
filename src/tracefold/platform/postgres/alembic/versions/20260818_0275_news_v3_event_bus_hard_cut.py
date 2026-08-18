"""News V3 event-bus hard cut: drop the Story/Brief/Push/Title schema, create the Event pipeline schema.

Revision ID: 20260818_0275
Revises: 20260818_0274
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0275"
down_revision = "20260818_0274"
branch_labels = None
depends_on = None

_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS = (0x54524644, 0)

_OLD_TABLES = (
    "news_push_deliveries",
    "news_push_state",
    "news_item_title_presentations",
    "news_brief_current",
    "news_brief_selection_current",
    "news_projection_summary",
    "news_story_members",
    "news_stories",
    "news_items",
    "news_sources",
    "news_opennews_incidents",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        f"""
        DO $migration$
        BEGIN
          IF NOT pg_try_advisory_xact_lock(
            {_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS[0]},
            {_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS[1]}
          ) THEN
            RAISE EXCEPTION 'news_v3_hard_cut_workers_active' USING ERRCODE = '55006';
          END IF;
        END
        $migration$;
        """
    )
    for table in _OLD_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    op.execute(
        r"""
        CREATE TABLE news_ingest_state (
          singleton_key text PRIMARY KEY DEFAULT 'opennews' CHECK (singleton_key = 'opennews'),
          connected boolean NOT NULL DEFAULT false,
          last_frame_at_ms bigint,
          last_publish_at_ms bigint,
          last_error_code text,
          configured_strategy_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
          provider_enabled_strategy_ids jsonb,
          strategy_warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
          broker_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
          updated_at_ms bigint NOT NULL
        );
        INSERT INTO news_ingest_state (singleton_key, updated_at_ms) VALUES ('opennews', 0);

        CREATE TABLE news_opennews_incidents (
          incident_id bigserial PRIMARY KEY,
          cause_class text NOT NULL CHECK (
            cause_class IN (
              'planned_shutdown', 'network_connect', 'authentication', 'provider_close', 'protocol_error',
              'idle_timeout', 'broker_backpressure', 'broker_unavailable', 'process_outage',
              'triage_circuit_open', 'unknown'
            )
          ),
          opened_at_ms bigint NOT NULL CHECK (opened_at_ms >= 0),
          closed_at_ms bigint CHECK (closed_at_ms IS NULL OR closed_at_ms >= opened_at_ms),
          planned boolean NOT NULL DEFAULT false,
          close_code integer,
          recovery_status text NOT NULL DEFAULT 'pending' CHECK (
            recovery_status IN ('pending', 'recovered', 'partial', 'unavailable', 'not_applicable')
          ),
          recovery_from_at_ms bigint,
          recovery_to_at_ms bigint,
          recovered_count integer NOT NULL DEFAULT 0,
          last_error_code text,
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL
        );
        CREATE INDEX ix_news_incidents_open ON news_opennews_incidents (closed_at_ms) WHERE closed_at_ms IS NULL;
        CREATE INDEX ix_news_incidents_recovery ON news_opennews_incidents (recovery_status, incident_id)
          WHERE recovery_status = 'pending';

        CREATE TABLE news_items (
          item_id text PRIMARY KEY,
          source_id text NOT NULL,
          source_item_key text NOT NULL,
          title text NOT NULL,
          raw_first_line text NOT NULL DEFAULT '',
          description text NOT NULL DEFAULT '',
          canonical_url text,
          reporting_origin text NOT NULL DEFAULT '',
          published_at_ms bigint NOT NULL,
          observed_at_ms bigint NOT NULL,
          provider_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
          provenance jsonb NOT NULL DEFAULT '[]'::jsonb,
          first_ingest_mode text NOT NULL CHECK (first_ingest_mode IN ('live', 'recovery')),
          trace_id text NOT NULL DEFAULT '',
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL,
          UNIQUE (source_id, source_item_key)
        );
        CREATE INDEX ix_news_items_published ON news_items (published_at_ms DESC);

        CREATE TABLE news_events (
          event_id text PRIMARY KEY,
          leader_item_id text NOT NULL REFERENCES news_items (item_id) ON DELETE CASCADE,
          family text NOT NULL,
          comparison_fingerprint text NOT NULL,
          comparison_title text NOT NULL,
          leader_title text NOT NULL,
          opened_at_ms bigint NOT NULL,
          last_member_at_ms bigint NOT NULL,
          expires_at_ms bigint NOT NULL,
          member_count integer NOT NULL DEFAULT 1,
          admission text NOT NULL,
          priority text NOT NULL DEFAULT 'normal' CHECK (priority IN ('high', 'normal')),
          provider_score_max double precision,
          engine_type text NOT NULL DEFAULT 'unknown',
          asset_class text NOT NULL DEFAULT 'none',
          grounded_assets jsonb NOT NULL DEFAULT '[]'::jsonb,
          watchlist_hits jsonb NOT NULL DEFAULT '[]'::jsonb,
          macro_lexicon boolean NOT NULL DEFAULT false,
          storyline_key text NOT NULL DEFAULT '',
          context_line text NOT NULL DEFAULT '',
          search_doc tsvector GENERATED ALWAYS AS (
            to_tsvector('simple', coalesce(context_line, '') || ' ' || coalesce(leader_title, ''))
          ) STORED,
          published_at_ms bigint,
          followup_of text,
          ingest_mode text NOT NULL CHECK (ingest_mode IN ('live', 'recovery')),
          trace_id text NOT NULL DEFAULT '',
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL
        );
        CREATE INDEX ix_news_events_opened ON news_events (opened_at_ms DESC);
        CREATE INDEX ix_news_events_admission ON news_events (admission, opened_at_ms DESC);
        CREATE INDEX ix_news_events_expires ON news_events (expires_at_ms);
        CREATE INDEX ix_news_events_storyline ON news_events (storyline_key, opened_at_ms DESC);
        CREATE INDEX ix_news_events_fingerprint ON news_events (family, comparison_fingerprint, opened_at_ms DESC);
        CREATE INDEX ix_news_events_search ON news_events USING gin (search_doc);
        CREATE INDEX ix_news_events_unpublished ON news_events (opened_at_ms)
          WHERE published_at_ms IS NULL AND admission = 'candidate';

        CREATE TABLE news_event_members (
          event_id text NOT NULL REFERENCES news_events (event_id) ON DELETE CASCADE,
          item_id text NOT NULL REFERENCES news_items (item_id) ON DELETE CASCADE,
          joined_at_ms bigint NOT NULL,
          match_kind text NOT NULL CHECK (match_kind IN ('leader', 'exact', 'near')),
          jaccard_estimate double precision,
          PRIMARY KEY (event_id, item_id)
        );
        CREATE INDEX ix_news_event_members_item ON news_event_members (item_id);

        CREATE TABLE news_event_bands (
          band_index smallint NOT NULL,
          band_key text NOT NULL,
          event_id text NOT NULL REFERENCES news_events (event_id) ON DELETE CASCADE,
          family text NOT NULL,
          expires_at_ms bigint NOT NULL,
          PRIMARY KEY (band_index, band_key, event_id)
        );
        CREATE INDEX ix_news_event_bands_lookup ON news_event_bands (band_index, band_key, family, expires_at_ms);
        CREATE INDEX ix_news_event_bands_expires ON news_event_bands (expires_at_ms);

        CREATE TABLE news_event_assets (
          symbol text NOT NULL,
          event_id text NOT NULL REFERENCES news_events (event_id) ON DELETE CASCADE,
          market_type text,
          opened_at_ms bigint NOT NULL,
          PRIMARY KEY (symbol, event_id)
        );
        CREATE INDEX ix_news_event_assets_symbol ON news_event_assets (symbol, opened_at_ms DESC);

        CREATE TABLE news_verdicts (
          event_id text NOT NULL REFERENCES news_events (event_id) ON DELETE CASCADE,
          stage text NOT NULL CHECK (stage IN ('triage', 'deep')),
          policy_version text NOT NULL,
          model_decision text,
          rule_baseline_decision text NOT NULL,
          final_decision text NOT NULL CHECK (final_decision IN ('push', 'escalate', 'drop', 'throttled', 'degraded')),
          override_rule text,
          throttled_by text,
          verdict jsonb NOT NULL DEFAULT '{}'::jsonb,
          model text,
          prompt_version text,
          degraded boolean NOT NULL DEFAULT false,
          error_code text,
          trace jsonb NOT NULL DEFAULT '{}'::jsonb,
          published_at_ms bigint,
          created_at_ms bigint NOT NULL,
          PRIMARY KEY (event_id, stage, policy_version)
        );
        CREATE INDEX ix_news_verdicts_stage_created ON news_verdicts (stage, created_at_ms DESC);
        CREATE INDEX ix_news_verdicts_final ON news_verdicts (final_decision, created_at_ms DESC);

        CREATE TABLE news_title_presentations (
          comparison_fingerprint text PRIMARY KEY,
          original_title text NOT NULL,
          display_title text NOT NULL,
          outcome text NOT NULL CHECK (outcome IN ('translated', 'not_needed', 'fallback')),
          provider text,
          fallback_code text,
          policy_version text NOT NULL,
          created_at_ms bigint NOT NULL
        );

        CREATE TABLE news_deliveries (
          event_id text NOT NULL REFERENCES news_events (event_id) ON DELETE CASCADE,
          kind text NOT NULL CHECK (kind IN ('first', 'followup')),
          state text NOT NULL CHECK (state IN ('sending', 'sent', 'terminal')),
          card jsonb NOT NULL DEFAULT '{}'::jsonb,
          receipt jsonb,
          error_code text,
          attempted_at_ms bigint NOT NULL,
          settled_at_ms bigint,
          created_at_ms bigint NOT NULL,
          PRIMARY KEY (event_id, kind)
        );
        CREATE INDEX ix_news_deliveries_state ON news_deliveries (state, attempted_at_ms DESC);
        CREATE INDEX ix_news_deliveries_sent ON news_deliveries (settled_at_ms DESC) WHERE state = 'sent';

        CREATE TABLE news_control_state (
          singleton_key text PRIMARY KEY DEFAULT 'current' CHECK (singleton_key = 'current'),
          paused boolean NOT NULL DEFAULT false,
          mutes jsonb NOT NULL DEFAULT '[]'::jsonb,
          updated_at_ms bigint NOT NULL
        );
        INSERT INTO news_control_state (singleton_key, updated_at_ms) VALUES ('current', 0);

        CREATE TABLE news_event_market_marks (
          event_id text NOT NULL REFERENCES news_events (event_id) ON DELETE CASCADE,
          mark text NOT NULL CHECK (mark IN ('t0', '5m', '30m', '4h')),
          symbol text NOT NULL,
          market_type text,
          price double precision,
          open_interest double precision,
          price_change_pct double precision,
          oi_change_pct double precision,
          captured_at_ms bigint NOT NULL,
          PRIMARY KEY (event_id, mark, symbol)
        );
        CREATE INDEX ix_news_marks_due ON news_event_market_marks (captured_at_ms);

        CREATE TABLE news_event_labels (
          event_id text NOT NULL REFERENCES news_events (event_id) ON DELETE CASCADE,
          label_version text NOT NULL,
          source text NOT NULL CHECK (source IN ('market', 'human', 'dual_model')),
          label jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at_ms bigint NOT NULL,
          PRIMARY KEY (event_id, label_version)
        );

        GRANT SELECT ON news_ingest_state, news_opennews_incidents, news_items, news_events, news_event_members,
          news_event_bands, news_event_assets, news_verdicts, news_title_presentations, news_deliveries,
          news_control_state, news_event_market_marks, news_event_labels TO tracefold_serve;
        GRANT SELECT, INSERT, UPDATE, DELETE ON news_ingest_state, news_opennews_incidents, news_items, news_events,
          news_event_members, news_event_bands, news_event_assets, news_verdicts, news_title_presentations,
          news_deliveries, news_control_state, news_event_market_marks, news_event_labels TO tracefold_workers;
        GRANT USAGE, SELECT ON SEQUENCE news_opennews_incidents_incident_id_seq TO tracefold_workers;
        """
    )


def downgrade() -> None:
    raise RuntimeError("news_v3_event_bus_hard_cut_is_irreversible")
