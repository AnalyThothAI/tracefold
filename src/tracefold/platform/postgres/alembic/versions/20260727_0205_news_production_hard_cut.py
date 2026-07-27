"""Hard-cut News to the production WorldMonitor kernel."""

from __future__ import annotations

from alembic import op

revision = "20260727_0205"
down_revision = "20260727_0204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '30min'")
    op.execute(
        """
        DROP TABLE news_brief_current;
        DROP TABLE news_brief_publications;
        DROP TABLE news_ai_classification_cache;
        DROP TABLE news_story_aliases;
        DROP TABLE news_story_members;
        DROP TABLE news_stories;
        DROP TABLE news_items;
        DROP TABLE news_feed_observations;
        DROP TABLE news_source_fetches;
        DROP TABLE news_sources;

        CREATE TABLE news_sources (
          source_id text PRIMARY KEY,
          name text NOT NULL CHECK (btrim(name) <> ''),
          feed_url text NOT NULL CHECK (feed_url ~ '^https?://'),
          tier smallint NOT NULL CHECK (tier BETWEEN 1 AND 4),
          lang text NOT NULL CHECK (btrim(lang) <> ''),
          enabled boolean NOT NULL DEFAULT true,
          refresh_interval_seconds integer NOT NULL
            CHECK (refresh_interval_seconds >= 1),
          etag text,
          last_modified text,
          last_fetch_started_at_ms bigint,
          last_fetch_finished_at_ms bigint,
          last_success_at_ms bigint,
          last_http_status integer,
          consecutive_failures integer NOT NULL DEFAULT 0
            CHECK (consecutive_failures >= 0),
          last_error text,
          next_fetch_at_ms bigint NOT NULL CHECK (next_fetch_at_ms >= 0),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        );
        CREATE INDEX ix_news_sources_due
          ON news_sources(next_fetch_at_ms, source_id) WHERE enabled;

        CREATE TABLE news_source_memberships (
          source_id text NOT NULL
            REFERENCES news_sources(source_id) ON DELETE RESTRICT,
          membership text NOT NULL CHECK (btrim(membership) <> ''),
          PRIMARY KEY (source_id, membership)
        );
        CREATE INDEX ix_news_source_memberships_membership
          ON news_source_memberships(membership, source_id);

        CREATE TABLE news_source_fetches (
          fetch_id text PRIMARY KEY,
          source_id text NOT NULL
            REFERENCES news_sources(source_id) ON DELETE RESTRICT,
          started_at_ms bigint NOT NULL CHECK (started_at_ms >= 0),
          finished_at_ms bigint NOT NULL
            CHECK (finished_at_ms >= started_at_ms),
          status text NOT NULL
            CHECK (status IN ('success', 'not_modified', 'failed')),
          fetch_path text CHECK (fetch_path IN ('direct', 'relay')),
          direct_error_code text,
          http_status integer,
          entries_seen integer NOT NULL DEFAULT 0 CHECK (entries_seen >= 0),
          observations_inserted integer NOT NULL DEFAULT 0
            CHECK (observations_inserted >= 0),
          items_inserted integer NOT NULL DEFAULT 0
            CHECK (items_inserted >= 0),
          items_updated integer NOT NULL DEFAULT 0 CHECK (items_updated >= 0),
          rejection_counts jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(rejection_counts) = 'object'),
          error_code text,
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          CHECK (status = 'failed' OR fetch_path IS NOT NULL)
        );
        CREATE INDEX ix_news_source_fetches_source_time
          ON news_source_fetches(source_id, finished_at_ms DESC, fetch_id DESC);
        CREATE INDEX ix_news_source_fetches_retention
          ON news_source_fetches(created_at_ms);

        CREATE TABLE news_feed_observations (
          observation_id text PRIMARY KEY,
          fetch_id text NOT NULL
            REFERENCES news_source_fetches(fetch_id) ON DELETE CASCADE,
          source_id text NOT NULL
            REFERENCES news_sources(source_id) ON DELETE RESTRICT,
          source_item_key text NOT NULL CHECK (btrim(source_item_key) <> ''),
          observed_at_ms bigint NOT NULL CHECK (observed_at_ms >= 0),
          title text,
          url text,
          published_at_ms bigint,
          raw jsonb NOT NULL CHECK (jsonb_typeof(raw) = 'object'),
          admitted boolean NOT NULL,
          rejection_reason text,
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          UNIQUE (fetch_id, source_item_key)
        );
        CREATE INDEX ix_news_feed_observations_source_item
          ON news_feed_observations(
            source_id, source_item_key, observed_at_ms DESC
          );

        CREATE TABLE news_items (
          item_id text PRIMARY KEY,
          source_id text NOT NULL
            REFERENCES news_sources(source_id) ON DELETE RESTRICT,
          source_item_key text NOT NULL CHECK (btrim(source_item_key) <> ''),
          canonical_url text NOT NULL CHECK (canonical_url ~ '^https?://'),
          reporting_origin text NOT NULL CHECK (btrim(reporting_origin) <> ''),
          title text NOT NULL CHECK (btrim(title) <> ''),
          normalized_title text NOT NULL CHECK (btrim(normalized_title) <> ''),
          description text NOT NULL DEFAULT '',
          lang text NOT NULL CHECK (btrim(lang) <> ''),
          published_at_ms bigint NOT NULL CHECK (published_at_ms >= 0),
          first_observed_at_ms bigint NOT NULL
            CHECK (first_observed_at_ms >= 0),
          last_observed_at_ms bigint NOT NULL
            CHECK (last_observed_at_ms >= first_observed_at_ms),
          content_fingerprint text NOT NULL
            CHECK (btrim(content_fingerprint) <> ''),
          level text NOT NULL
            CHECK (level IN ('critical', 'high', 'medium', 'low', 'info')),
          category text NOT NULL CHECK (category IN (
            'conflict','protest','disaster','diplomatic','economic','terrorism',
            'cyber','health','environmental','military','crime','infrastructure',
            'tech','general'
          )),
          classification_source text NOT NULL CHECK (
            classification_source IN ('keyword', 'keyword-historical-downgrade')
          ),
          classification_confidence double precision NOT NULL
            CHECK (classification_confidence BETWEEN 0 AND 1),
          importance_score integer NOT NULL DEFAULT 0
            CHECK (importance_score >= 0),
          importance_factors jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(importance_factors) = 'object'),
          brief_excluded boolean NOT NULL DEFAULT false,
          active boolean NOT NULL DEFAULT true,
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0),
          UNIQUE (source_id, source_item_key)
        );
        CREATE INDEX ix_news_items_active_time
          ON news_items(published_at_ms DESC, item_id) WHERE active;
        CREATE INDEX ix_news_items_source_time
          ON news_items(source_id, published_at_ms DESC, item_id) WHERE active;

        CREATE TABLE news_stories (
          story_id text PRIMARY KEY,
          canonical_key text NOT NULL CHECK (btrim(canonical_key) <> ''),
          canonical_title text NOT NULL CHECK (btrim(canonical_title) <> ''),
          representative_item_id text NOT NULL
            REFERENCES news_items(item_id) ON DELETE RESTRICT,
          representative_source_id text NOT NULL
            REFERENCES news_sources(source_id) ON DELETE RESTRICT,
          representative_title text NOT NULL
            CHECK (btrim(representative_title) <> ''),
          representative_url text NOT NULL
            CHECK (representative_url ~ '^https?://'),
          representative_description text NOT NULL DEFAULT '',
          scoring_item_id text NOT NULL
            REFERENCES news_items(item_id) ON DELETE RESTRICT,
          level text NOT NULL
            CHECK (level IN ('critical', 'high', 'medium', 'low', 'info')),
          category text NOT NULL CHECK (category IN (
            'conflict','protest','disaster','diplomatic','economic','terrorism',
            'cyber','health','environmental','military','crime','infrastructure',
            'tech','general'
          )),
          importance_score integer NOT NULL CHECK (importance_score >= 0),
          importance_factors jsonb NOT NULL
            CHECK (jsonb_typeof(importance_factors) = 'object'),
          item_count integer NOT NULL CHECK (item_count >= 1),
          source_count integer NOT NULL CHECK (source_count >= 1),
          first_published_at_ms bigint NOT NULL
            CHECK (first_published_at_ms >= 0),
          last_published_at_ms bigint NOT NULL
            CHECK (last_published_at_ms >= first_published_at_ms),
          active boolean NOT NULL DEFAULT true,
          state_fingerprint text NOT NULL
            CHECK (btrim(state_fingerprint) <> ''),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        );
        CREATE INDEX ix_news_stories_importance_feed
          ON news_stories(
            importance_score DESC, last_published_at_ms DESC, story_id
          ) WHERE active;
        CREATE INDEX ix_news_stories_latest_feed
          ON news_stories(
            last_published_at_ms DESC, importance_score DESC, story_id
          ) WHERE active;
        CREATE INDEX ix_news_stories_category
          ON news_stories(
            category, importance_score DESC, last_published_at_ms DESC, story_id
          ) WHERE active;

        CREATE TABLE news_story_members (
          story_id text NOT NULL
            REFERENCES news_stories(story_id) ON DELETE RESTRICT,
          item_id text NOT NULL
            REFERENCES news_items(item_id) ON DELETE RESTRICT,
          current boolean NOT NULL DEFAULT true,
          first_joined_at_ms bigint NOT NULL CHECK (first_joined_at_ms >= 0),
          last_confirmed_at_ms bigint NOT NULL
            CHECK (last_confirmed_at_ms >= first_joined_at_ms),
          PRIMARY KEY (story_id, item_id)
        );
        CREATE UNIQUE INDEX ux_news_story_members_current_item
          ON news_story_members(item_id) WHERE current;
        CREATE INDEX ix_news_story_members_current_story
          ON news_story_members(story_id, item_id) WHERE current;

        CREATE TABLE news_story_aliases (
          alias_key text PRIMARY KEY,
          story_id text NOT NULL
            REFERENCES news_stories(story_id) ON DELETE RESTRICT,
          expires_at_ms bigint NOT NULL CHECK (expires_at_ms >= 0),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0)
        );
        CREATE INDEX ix_news_story_aliases_expiry
          ON news_story_aliases(expires_at_ms);

        CREATE TABLE news_brief_runs (
          run_id text PRIMARY KEY,
          fingerprint text NOT NULL UNIQUE CHECK (btrim(fingerprint) <> ''),
          status text NOT NULL CHECK (
            status IN ('running', 'ready', 'insufficient_material', 'failed')
          ),
          attempt_count integer NOT NULL CHECK (attempt_count >= 0),
          candidate_story_count integer NOT NULL
            CHECK (candidate_story_count >= 0),
          candidate_source_count integer NOT NULL
            CHECK (candidate_source_count >= 0),
          lease_owner text,
          lease_expires_at_ms bigint,
          heartbeat_at_ms bigint,
          last_error text,
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= created_at_ms),
          completed_at_ms bigint,
          CHECK (
            (status = 'running' AND lease_owner IS NOT NULL
              AND lease_expires_at_ms IS NOT NULL
              AND heartbeat_at_ms IS NOT NULL)
            OR
            (status <> 'running' AND lease_owner IS NULL
              AND lease_expires_at_ms IS NULL)
          )
        );
        CREATE INDEX ix_news_brief_runs_status
          ON news_brief_runs(status, lease_expires_at_ms, updated_at_ms);

        CREATE TABLE news_brief_publications (
          publication_id text PRIMARY KEY,
          fingerprint text NOT NULL UNIQUE CHECK (btrim(fingerprint) <> ''),
          evidence_cutoff_at_ms bigint NOT NULL
            CHECK (evidence_cutoff_at_ms >= 0),
          published_at_ms bigint NOT NULL CHECK (published_at_ms >= 0),
          provider text NOT NULL CHECK (btrim(provider) <> ''),
          model text NOT NULL CHECK (btrim(model) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          workflow_version text NOT NULL CHECK (btrim(workflow_version) <> ''),
          schema_version text NOT NULL CHECK (btrim(schema_version) <> ''),
          locale text NOT NULL CHECK (btrim(locale) <> ''),
          selected_story_ids jsonb NOT NULL
            CHECK (jsonb_typeof(selected_story_ids) = 'array'),
          lead text NOT NULL CHECK (btrim(lead) <> ''),
          lines jsonb NOT NULL CHECK (jsonb_typeof(lines) = 'array'),
          sources jsonb NOT NULL CHECK (jsonb_typeof(sources) = 'array'),
          validation jsonb NOT NULL CHECK (jsonb_typeof(validation) = 'object'),
          raw_response text NOT NULL,
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0)
        );
        CREATE INDEX ix_news_brief_publications_time
          ON news_brief_publications(published_at_ms DESC, publication_id DESC);

        CREATE TABLE news_brief_current (
          singleton_key boolean PRIMARY KEY DEFAULT true CHECK (singleton_key),
          publication_id text
            REFERENCES news_brief_publications(publication_id) ON DELETE RESTRICT,
          target_fingerprint text,
          latest_run_id text
            REFERENCES news_brief_runs(run_id) ON DELETE SET NULL,
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        );
        INSERT INTO news_brief_current(singleton_key, updated_at_ms)
        VALUES (true, 0);
        """
    )


def downgrade() -> None:
    raise RuntimeError("news_production_hard_cut_is_irreversible")
