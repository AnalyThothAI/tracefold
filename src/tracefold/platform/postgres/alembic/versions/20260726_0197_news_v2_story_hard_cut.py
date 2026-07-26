"""Hard-cut legacy News and create the RSS -> Story -> analysis model."""

from __future__ import annotations

from alembic import op

revision = "20260726_0197"
down_revision = "20260724_0196"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '30min'")
    op.execute(
        """
        DROP TABLE IF EXISTS
          news_token_mentions,
          news_fact_candidates,
          news_item_entities,
          news_page_rows,
          news_projection_dirty_targets,
          news_item_observation_edges,
          news_items,
          news_provider_items,
          news_fetch_runs,
          news_sources
        CASCADE
        """
    )
    op.execute(
        """
        CREATE TABLE news_sources (
          source_id text PRIMARY KEY,
          name text NOT NULL CHECK (btrim(name) <> ''),
          feed_url text NOT NULL CHECK (btrim(feed_url) <> ''),
          source_domain text NOT NULL CHECK (btrim(source_domain) <> ''),
          source_role text NOT NULL
            CHECK (source_role IN ('original_publisher', 'trusted_aggregator')),
          trust_tier text NOT NULL
            CHECK (trust_tier IN ('authoritative', 'trusted', 'standard', 'low')),
          source_chain_id text NOT NULL CHECK (btrim(source_chain_id) <> ''),
          coverage_tags jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(coverage_tags) = 'array'),
          default_language text NOT NULL CHECK (btrim(default_language) <> ''),
          enabled boolean NOT NULL DEFAULT true,
          refresh_interval_seconds integer NOT NULL CHECK (refresh_interval_seconds > 0),
          etag text,
          last_modified text,
          last_fetch_started_at_ms bigint,
          last_fetch_finished_at_ms bigint,
          last_success_at_ms bigint,
          last_http_status integer,
          consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
          last_error text,
          next_fetch_at_ms bigint NOT NULL DEFAULT 0 CHECK (next_fetch_at_ms >= 0),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_sources_due
          ON news_sources(next_fetch_at_ms, source_id)
          WHERE enabled
        """
    )
    op.execute(
        """
        CREATE TABLE news_articles (
          article_id text PRIMARY KEY,
          source_id text NOT NULL REFERENCES news_sources(source_id) ON DELETE RESTRICT,
          identity_version text NOT NULL CHECK (btrim(identity_version) <> ''),
          identity_method text NOT NULL
            CHECK (identity_method IN ('canonical_url', 'source_guid', 'title_time_bucket')),
          identity_key text NOT NULL CHECK (btrim(identity_key) <> ''),
          source_guid text,
          canonical_url text,
          title text NOT NULL CHECK (btrim(title) <> ''),
          snippet text NOT NULL DEFAULT '',
          published_at_ms bigint NOT NULL CHECK (published_at_ms >= 0),
          first_seen_at_ms bigint NOT NULL CHECK (first_seen_at_ms >= 0),
          last_seen_at_ms bigint NOT NULL CHECK (last_seen_at_ms >= first_seen_at_ms),
          language text NOT NULL CHECK (btrim(language) <> ''),
          origin_url text,
          origin_domain text,
          origin_name text,
          provenance_status text NOT NULL
            CHECK (provenance_status IN ('verified', 'attributed', 'unknown')),
          content_hash text NOT NULL CHECK (btrim(content_hash) <> ''),
          source_entry jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(source_entry) = 'object'),
          UNIQUE (source_id, identity_version, identity_key)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_articles_source_published
          ON news_articles(source_id, published_at_ms DESC, article_id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_articles_language_published
          ON news_articles(language, published_at_ms DESC, article_id DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE news_stories (
          story_id text PRIMARY KEY,
          anchor_article_id text NOT NULL REFERENCES news_articles(article_id) ON DELETE RESTRICT,
          primary_article_id text NOT NULL REFERENCES news_articles(article_id) ON DELETE RESTRICT,
          language text NOT NULL CHECK (btrim(language) <> ''),
          title text NOT NULL CHECK (btrim(title) <> ''),
          snippet text NOT NULL DEFAULT '',
          first_seen_at_ms bigint NOT NULL CHECK (first_seen_at_ms >= 0),
          last_seen_at_ms bigint NOT NULL CHECK (last_seen_at_ms >= first_seen_at_ms),
          source_count integer NOT NULL CHECK (source_count >= 1),
          article_count integer NOT NULL CHECK (article_count >= 1),
          trusted_source_count integer NOT NULL CHECK (trusted_source_count >= 0),
          independent_origin_count integer NOT NULL CHECK (independent_origin_count >= 0),
          verification_status text NOT NULL
            CHECK (verification_status IN ('corroborated', 'trusted', 'attributed', 'unverified')),
          phase text NOT NULL
            CHECK (phase IN ('breaking', 'developing', 'sustained', 'fading')),
          lifecycle_version text NOT NULL CHECK (btrim(lifecycle_version) <> ''),
          importance_score integer NOT NULL CHECK (importance_score BETWEEN 0 AND 100),
          importance_version text NOT NULL CHECK (btrim(importance_version) <> ''),
          importance_factors jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(importance_factors) = 'object'),
          identity_version text NOT NULL CHECK (btrim(identity_version) <> ''),
          evidence_set_hash text NOT NULL CHECK (btrim(evidence_set_hash) <> ''),
          next_state_refresh_at_ms bigint NOT NULL CHECK (next_state_refresh_at_ms >= 0),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_stories_feed
          ON news_stories(last_seen_at_ms DESC, story_id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_stories_identity_candidates
          ON news_stories(language, last_seen_at_ms DESC, story_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_stories_state_refresh
          ON news_stories(next_state_refresh_at_ms, story_id)
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_articles (
          story_id text NOT NULL REFERENCES news_stories(story_id) ON DELETE CASCADE,
          article_id text NOT NULL REFERENCES news_articles(article_id) ON DELETE RESTRICT,
          match_method text NOT NULL CHECK (btrim(match_method) <> ''),
          match_score double precision NOT NULL CHECK (match_score BETWEEN 0 AND 1),
          identity_version text NOT NULL CHECK (btrim(identity_version) <> ''),
          admitted_at_ms bigint NOT NULL CHECK (admitted_at_ms >= 0),
          match_reason jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(match_reason) = 'object'),
          PRIMARY KEY (story_id, article_id),
          UNIQUE (article_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_story_articles_story_admitted
          ON news_story_articles(story_id, admitted_at_ms, article_id)
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_analyses (
          analysis_id text PRIMARY KEY,
          story_id text NOT NULL REFERENCES news_stories(story_id) ON DELETE CASCADE,
          evidence_set_hash text NOT NULL CHECK (btrim(evidence_set_hash) <> ''),
          model text NOT NULL CHECK (btrim(model) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          workflow_version text NOT NULL CHECK (btrim(workflow_version) <> ''),
          schema_version text NOT NULL CHECK (btrim(schema_version) <> ''),
          what_happened text NOT NULL CHECK (btrim(what_happened) <> ''),
          why_it_matters text NOT NULL CHECK (btrim(why_it_matters) <> ''),
          political_impact text NOT NULL CHECK (btrim(political_impact) <> ''),
          economic_market_impact text NOT NULL CHECK (btrim(economic_market_impact) <> ''),
          confirmed_facts jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(confirmed_facts) = 'array'),
          disagreements_unknowns jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(disagreements_unknowns) = 'array'),
          next_checkpoint text NOT NULL CHECK (btrim(next_checkpoint) <> ''),
          evidence_references jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(evidence_references) = 'array'),
          receipt jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(receipt) = 'object'),
          published_at_ms bigint NOT NULL CHECK (published_at_ms >= 0),
          UNIQUE (
            story_id, evidence_set_hash, model, prompt_version, workflow_version, schema_version
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_story_analyses_current
          ON news_story_analyses(story_id, evidence_set_hash, published_at_ms DESC, analysis_id DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_analysis_attempts (
          analysis_key text PRIMARY KEY,
          story_id text NOT NULL REFERENCES news_stories(story_id) ON DELETE CASCADE,
          evidence_set_hash text NOT NULL CHECK (btrim(evidence_set_hash) <> ''),
          model text NOT NULL CHECK (btrim(model) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          workflow_version text NOT NULL CHECK (btrim(workflow_version) <> ''),
          schema_version text NOT NULL CHECK (btrim(schema_version) <> ''),
          status text NOT NULL CHECK (status IN ('running', 'failed', 'available')),
          attempt_count integer NOT NULL CHECK (attempt_count >= 1),
          lease_expires_at_ms bigint NOT NULL DEFAULT 0 CHECK (lease_expires_at_ms >= 0),
          next_attempt_at_ms bigint NOT NULL DEFAULT 0 CHECK (next_attempt_at_ms >= 0),
          last_error text,
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_story_analysis_attempts_claim
          ON news_story_analysis_attempts(
            story_id, evidence_set_hash, model, prompt_version, workflow_version, schema_version
          )
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260726_0197 is an irreversible News v2 hard cut; legacy News data and schema are not restored"
    )
