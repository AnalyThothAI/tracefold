"""Hard-cut News into the professional Article -> Event Story -> Brief pipeline."""

from __future__ import annotations

from alembic import op

revision = "20260726_0198"
down_revision = "20260726_0197"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '30min'")
    op.execute(
        """
        CREATE TEMP TABLE preserved_news_sources ON COMMIT DROP AS
        SELECT
          source_id,
          name,
          feed_url,
          source_domain,
          source_role,
          trust_tier,
          source_chain_id,
          coverage_tags,
          default_language,
          enabled,
          refresh_interval_seconds,
          etag,
          last_modified,
          last_fetch_started_at_ms,
          last_fetch_finished_at_ms,
          last_success_at_ms,
          last_http_status,
          consecutive_failures,
          last_error,
          next_fetch_at_ms,
          created_at_ms,
          updated_at_ms
        FROM news_sources
        """
    )
    op.execute(
        """
        DROP TABLE IF EXISTS
          news_story_analysis_current,
          news_story_analysis_publications,
          news_brief_current,
          news_brief_publications,
          news_ai_attempts,
          news_story_analysis_requests,
          news_brief_selection_snapshots,
          news_narrative_grouping_snapshots,
          news_story_material_events,
          news_story_identity_decisions,
          news_story_profiles,
          news_story_memberships,
          news_article_identity_features,
          news_article_content_snapshots,
          news_article_revisions,
          news_articles,
          news_feed_observations,
          news_fetch_receipts,
          news_story_projection_checkpoints,
          news_story_analysis_attempts,
          news_story_analyses,
          news_story_articles,
          news_stories,
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
            CHECK (
              source_role IN (
                'original_publisher',
                'wire_service',
                'official_authority',
                'trusted_aggregator'
              )
            ),
          trust_tier text NOT NULL
            CHECK (trust_tier IN ('authoritative', 'trusted', 'standard', 'low')),
          source_chain_id text NOT NULL CHECK (btrim(source_chain_id) <> ''),
          publisher_organization_id text NOT NULL CHECK (btrim(publisher_organization_id) <> ''),
          parent_organization_id text,
          canonical_domains jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(canonical_domains) = 'array'),
          known_relationships jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(known_relationships) = 'array'),
          source_quality_factors jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(source_quality_factors) = 'object'),
          registry_version text NOT NULL CHECK (btrim(registry_version) <> ''),
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
        INSERT INTO news_sources (
          source_id,
          name,
          feed_url,
          source_domain,
          source_role,
          trust_tier,
          source_chain_id,
          publisher_organization_id,
          canonical_domains,
          source_quality_factors,
          registry_version,
          coverage_tags,
          default_language,
          enabled,
          refresh_interval_seconds,
          etag,
          last_modified,
          last_fetch_started_at_ms,
          last_fetch_finished_at_ms,
          last_success_at_ms,
          last_http_status,
          consecutive_failures,
          last_error,
          next_fetch_at_ms,
          created_at_ms,
          updated_at_ms
        )
        SELECT
          source_id,
          name,
          feed_url,
          source_domain,
          source_role,
          trust_tier,
          source_chain_id,
          source_chain_id,
          jsonb_build_array(source_domain),
          jsonb_build_object('trust_tier', trust_tier),
          'news_source_registry_v2',
          coverage_tags,
          default_language,
          enabled,
          refresh_interval_seconds,
          NULL,
          NULL,
          NULL,
          NULL,
          NULL,
          NULL,
          0,
          NULL,
          0,
          created_at_ms,
          updated_at_ms
        FROM preserved_news_sources
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
        CREATE TABLE news_fetch_receipts (
          fetch_receipt_id text PRIMARY KEY,
          source_id text NOT NULL REFERENCES news_sources(source_id) ON DELETE CASCADE,
          started_at_ms bigint NOT NULL CHECK (started_at_ms >= 0),
          finished_at_ms bigint NOT NULL CHECK (finished_at_ms >= started_at_ms),
          http_status integer,
          not_modified boolean NOT NULL DEFAULT false,
          entries_seen integer NOT NULL DEFAULT 0 CHECK (entries_seen >= 0),
          entries_admitted integer NOT NULL DEFAULT 0 CHECK (entries_admitted >= 0),
          duplicate_seen_count integer NOT NULL DEFAULT 0 CHECK (duplicate_seen_count >= 0),
          rejection_counts jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(rejection_counts) = 'object'),
          observation_ids jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(observation_ids) = 'array'),
          error_code text,
          error_detail text,
          etag text,
          last_modified text,
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          UNIQUE (source_id, started_at_ms)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_fetch_receipts_source_finished
          ON news_fetch_receipts(source_id, finished_at_ms DESC, fetch_receipt_id DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE news_feed_observations (
          observation_id text PRIMARY KEY,
          source_id text NOT NULL REFERENCES news_sources(source_id) ON DELETE RESTRICT,
          fetch_receipt_id text NOT NULL REFERENCES news_fetch_receipts(fetch_receipt_id) ON DELETE RESTRICT,
          source_entry_key text NOT NULL CHECK (btrim(source_entry_key) <> ''),
          observation_revision_hash text NOT NULL CHECK (btrim(observation_revision_hash) <> ''),
          source_guid text,
          raw_url text NOT NULL CHECK (btrim(raw_url) <> ''),
          normalized_url text NOT NULL CHECK (btrim(normalized_url) <> ''),
          title text NOT NULL CHECK (btrim(title) <> ''),
          summary text NOT NULL DEFAULT '',
          source_published_at_ms bigint NOT NULL CHECK (source_published_at_ms >= 0),
          observed_at_ms bigint NOT NULL CHECK (observed_at_ms >= 0),
          language text NOT NULL CHECK (btrim(language) <> ''),
          raw_entry jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(raw_entry) = 'object'),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          UNIQUE (source_id, source_entry_key, observation_revision_hash)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_feed_observations_order
          ON news_feed_observations(observed_at_ms, observation_id)
        """
    )
    op.execute(
        """
        CREATE TABLE news_articles (
          article_id text PRIMARY KEY,
          publisher_organization_id text NOT NULL CHECK (btrim(publisher_organization_id) <> ''),
          canonical_url text NOT NULL CHECK (btrim(canonical_url) <> ''),
          incarnation_key text NOT NULL CHECK (btrim(incarnation_key) <> ''),
          first_observation_id text NOT NULL
            REFERENCES news_feed_observations(observation_id) ON DELETE RESTRICT,
          first_seen_at_ms bigint NOT NULL CHECK (first_seen_at_ms >= 0),
          identity_version text NOT NULL CHECK (btrim(identity_version) <> ''),
          identity_status text NOT NULL
            CHECK (identity_status IN ('active', 'ended', 'revision_identity_ambiguous')),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0),
          UNIQUE (publisher_organization_id, canonical_url, incarnation_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_article_revisions (
          revision_id text PRIMARY KEY,
          article_id text NOT NULL REFERENCES news_articles(article_id) ON DELETE CASCADE,
          observation_id text NOT NULL
            REFERENCES news_feed_observations(observation_id) ON DELETE RESTRICT,
          revision_number integer NOT NULL CHECK (revision_number >= 1),
          title text NOT NULL CHECK (btrim(title) <> ''),
          snippet text NOT NULL DEFAULT '',
          source_published_at_ms bigint NOT NULL CHECK (source_published_at_ms >= 0),
          observed_at_ms bigint NOT NULL CHECK (observed_at_ms >= 0),
          language text NOT NULL CHECK (btrim(language) <> ''),
          content_hash text NOT NULL CHECK (btrim(content_hash) <> ''),
          material_change_kind text NOT NULL
            CHECK (
              material_change_kind IN (
                'initial',
                'title',
                'summary',
                'source_time',
                'content',
                'correction',
                'url_reuse'
              )
            ),
          is_current boolean NOT NULL DEFAULT true,
          raw_entry jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(raw_entry) = 'object'),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          UNIQUE (article_id, revision_number),
          UNIQUE (article_id, content_hash)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_news_article_revisions_current
          ON news_article_revisions(article_id)
          WHERE is_current
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_article_revisions_projection_order
          ON news_article_revisions(observed_at_ms, revision_id)
        """
    )
    op.execute(
        """
        CREATE TABLE news_article_content_snapshots (
          content_snapshot_id text PRIMARY KEY,
          article_id text NOT NULL REFERENCES news_articles(article_id) ON DELETE CASCADE,
          revision_id text NOT NULL REFERENCES news_article_revisions(revision_id) ON DELETE CASCADE,
          requested_at_ms bigint NOT NULL CHECK (requested_at_ms >= 0),
          fetched_at_ms bigint,
          status text NOT NULL
            CHECK (
              status IN (
                'pending',
                'available',
                'failed',
                'paywalled',
                'robots_denied',
                'unsupported_content',
                'truncated'
              )
            ),
          http_status integer,
          content_type text,
          extractor_version text NOT NULL CHECK (btrim(extractor_version) <> ''),
          content_hash text,
          extracted_text text,
          byte_count integer CHECK (byte_count IS NULL OR byte_count >= 0),
          failure_reason text,
          source_url text NOT NULL CHECK (btrim(source_url) <> ''),
          final_url text,
          attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
          lease_token text NOT NULL CHECK (btrim(lease_token) <> ''),
          lease_expires_at_ms bigint NOT NULL DEFAULT 0 CHECK (lease_expires_at_ms >= 0),
          next_attempt_at_ms bigint NOT NULL DEFAULT 0 CHECK (next_attempt_at_ms >= 0),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0),
          UNIQUE (revision_id, extractor_version, source_url)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_article_content_snapshots_claim
          ON news_article_content_snapshots(status, next_attempt_at_ms, lease_expires_at_ms)
        """
    )
    op.execute(
        """
        CREATE TABLE news_article_identity_features (
          revision_id text NOT NULL REFERENCES news_article_revisions(revision_id) ON DELETE CASCADE,
          identity_version text NOT NULL CHECK (btrim(identity_version) <> ''),
          article_id text NOT NULL REFERENCES news_articles(article_id) ON DELETE CASCADE,
          language text NOT NULL CHECK (btrim(language) <> ''),
          normalized_title text NOT NULL,
          normalized_lead text NOT NULL,
          content_fingerprint text NOT NULL CHECK (btrim(content_fingerprint) <> ''),
          lexical_signature text NOT NULL,
          event_key text NOT NULL,
          named_event_keys jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(named_event_keys) = 'array'),
          features jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(features) = 'object'),
          extraction_receipt jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(extraction_receipt) = 'object'),
          feature_hash text NOT NULL CHECK (btrim(feature_hash) <> ''),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          PRIMARY KEY (revision_id, identity_version)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_article_identity_features_candidates
          ON news_article_identity_features(identity_version, language, event_key, revision_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_article_identity_features_content
          ON news_article_identity_features(identity_version, content_fingerprint, revision_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_article_identity_features_title_trgm
          ON news_article_identity_features
          USING gin (normalized_title gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_article_identity_features_named_events
          ON news_article_identity_features
          USING gin (named_event_keys)
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_projection_checkpoints (
          identity_version text PRIMARY KEY,
          last_observed_at_ms bigint NOT NULL DEFAULT 0 CHECK (last_observed_at_ms >= 0),
          last_revision_id text NOT NULL DEFAULT '',
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_stories (
          story_id text PRIMARY KEY,
          seed_article_id text NOT NULL REFERENCES news_articles(article_id) ON DELETE RESTRICT,
          representative_revision_id text NOT NULL
            REFERENCES news_article_revisions(revision_id) ON DELETE RESTRICT,
          identity_version text NOT NULL CHECK (btrim(identity_version) <> ''),
          identity_status text NOT NULL CHECK (identity_status IN ('stable', 'ambiguous')),
          title text NOT NULL CHECK (btrim(title) <> ''),
          snippet text NOT NULL DEFAULT '',
          languages jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(languages) = 'array'),
          event_core jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(event_core) = 'object'),
          first_seen_at_ms bigint NOT NULL CHECK (first_seen_at_ms >= 0),
          last_material_evidence_at_ms bigint NOT NULL CHECK (last_material_evidence_at_ms >= 0),
          material_evolution_state text NOT NULL CHECK (btrim(material_evolution_state) <> ''),
          lifecycle text NOT NULL
            CHECK (lifecycle IN ('emerging', 'developing', 'stable', 'fading', 'dormant', 'reactivated')),
          breaking boolean NOT NULL DEFAULT false,
          lifecycle_version text NOT NULL CHECK (btrim(lifecycle_version) <> ''),
          impact_profile jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(impact_profile) = 'object'),
          priority_profile jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(priority_profile) = 'object'),
          impact_score integer NOT NULL CHECK (impact_score BETWEEN 0 AND 100),
          priority_score integer NOT NULL CHECK (priority_score BETWEEN 0 AND 100),
          scoring_version text NOT NULL CHECK (btrim(scoring_version) <> ''),
          evidence_posture text NOT NULL
            CHECK (
              evidence_posture IN (
                'single_origin_reported',
                'independently_corroborated',
                'primary_source_confirmed',
                'contested',
                'corrected',
                'withdrawn'
              )
            ),
          evidence_factors jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(evidence_factors) = 'object'),
          article_count integer NOT NULL CHECK (article_count >= 1),
          primary_member_count integer NOT NULL CHECK (primary_member_count >= 0),
          contextual_member_count integer NOT NULL CHECK (contextual_member_count >= 0),
          reporting_origin_count integer NOT NULL CHECK (reporting_origin_count >= 0),
          independent_origin_count integer NOT NULL CHECK (independent_origin_count >= 0),
          syndicated_article_count integer NOT NULL CHECK (syndicated_article_count >= 0),
          material_evidence_hash text NOT NULL CHECK (btrim(material_evidence_hash) <> ''),
          presentation_state_hash text NOT NULL CHECK (btrim(presentation_state_hash) <> ''),
          brief_eligible boolean NOT NULL DEFAULT false,
          brief_eligibility_reason jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(brief_eligibility_reason) = 'object'),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_stories_feed
          ON news_stories(priority_score DESC, last_material_evidence_at_ms DESC, story_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_stories_brief_eligible
          ON news_stories(priority_score DESC, impact_score DESC, story_id)
          WHERE brief_eligible
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_memberships (
          story_id text NOT NULL REFERENCES news_stories(story_id) ON DELETE CASCADE,
          article_id text NOT NULL REFERENCES news_articles(article_id) ON DELETE RESTRICT,
          revision_id text NOT NULL REFERENCES news_article_revisions(revision_id) ON DELETE RESTRICT,
          membership_kind text NOT NULL CHECK (membership_kind IN ('primary', 'contextual')),
          identity_version text NOT NULL CHECK (btrim(identity_version) <> ''),
          verdict text NOT NULL
            CHECK (
              verdict IN (
                'seed',
                'accept_strong',
                'accept_scored',
                'contextual'
              )
            ),
          match_method text NOT NULL CHECK (btrim(match_method) <> ''),
          match_score double precision NOT NULL CHECK (match_score BETWEEN 0 AND 1),
          runner_up_margin double precision NOT NULL CHECK (runner_up_margin BETWEEN 0 AND 1),
          match_reason jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(match_reason) = 'object'),
          content_form text NOT NULL
            CHECK (content_form IN ('report', 'analysis', 'opinion', 'live', 'static', 'unknown')),
          origin_relation text NOT NULL
            CHECK (
              origin_relation IN (
                'originating',
                'independent',
                'syndicated',
                'derived',
                'unresolved'
              )
            ),
          development_relation text NOT NULL
            CHECK (
              development_relation IN (
                'initial',
                'follow_up',
                'correction',
                'background',
                'retrospective'
              )
            ),
          epistemic_use text NOT NULL
            CHECK (epistemic_use IN ('fact_evidence', 'context', 'viewpoint', 'non_evidence')),
          reporting_origin_id text,
          origin_confidence double precision NOT NULL CHECK (origin_confidence BETWEEN 0 AND 1),
          semantics_reason jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(semantics_reason) = 'object'),
          admitted_at_ms bigint NOT NULL CHECK (admitted_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0),
          PRIMARY KEY (story_id, article_id, membership_kind)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_news_story_memberships_primary_article
          ON news_story_memberships(article_id)
          WHERE membership_kind = 'primary'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_story_memberships_story
          ON news_story_memberships(story_id, membership_kind, admitted_at_ms, article_id)
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_profiles (
          story_id text PRIMARY KEY REFERENCES news_stories(story_id) ON DELETE CASCADE,
          identity_version text NOT NULL CHECK (btrim(identity_version) <> ''),
          profile jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(profile) = 'object'),
          profile_hash text NOT NULL CHECK (btrim(profile_hash) <> ''),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_identity_decisions (
          decision_id text PRIMARY KEY,
          revision_id text NOT NULL REFERENCES news_article_revisions(revision_id) ON DELETE CASCADE,
          article_id text NOT NULL REFERENCES news_articles(article_id) ON DELETE CASCADE,
          identity_version text NOT NULL CHECK (btrim(identity_version) <> ''),
          selected_story_id text REFERENCES news_stories(story_id) ON DELETE SET NULL,
          verdict text NOT NULL
            CHECK (
              verdict IN (
                'accept_strong',
                'accept_scored',
                'reject_conflict',
                'ambiguous_new_story',
                'no_candidate_new_story',
                'revision_compatible',
                'revision_identity_ambiguous'
              )
            ),
          candidates jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(candidates) = 'array'),
          decision_reason jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(decision_reason) = 'object'),
          decided_at_ms bigint NOT NULL CHECK (decided_at_ms >= 0),
          UNIQUE (revision_id, identity_version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_material_events (
          material_event_id text PRIMARY KEY,
          story_id text NOT NULL REFERENCES news_stories(story_id) ON DELETE CASCADE,
          revision_id text REFERENCES news_article_revisions(revision_id) ON DELETE SET NULL,
          event_kind text NOT NULL
            CHECK (
              event_kind IN (
                'first_report',
                'new_independent_origin',
                'material_follow_up',
                'material_correction',
                'conflict_detected',
                'conflict_resolved',
                'retraction'
              )
            ),
          event_factors jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(event_factors) = 'object'),
          occurred_at_ms bigint NOT NULL CHECK (occurred_at_ms >= 0),
          UNIQUE (story_id, revision_id, event_kind)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_narrative_grouping_snapshots (
          grouping_snapshot_id text PRIMARY KEY,
          input_hash text NOT NULL CHECK (btrim(input_hash) <> ''),
          policy_version text NOT NULL CHECK (btrim(policy_version) <> ''),
          embedding_model text,
          fallback_used boolean NOT NULL DEFAULT false,
          groups jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(groups) = 'array'),
          receipt jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(receipt) = 'object'),
          cutoff_at_ms bigint NOT NULL CHECK (cutoff_at_ms >= 0),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          UNIQUE (input_hash, policy_version, embedding_model)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_brief_selection_snapshots (
          selection_snapshot_id text PRIMARY KEY,
          selection_fingerprint text NOT NULL UNIQUE CHECK (btrim(selection_fingerprint) <> ''),
          grouping_snapshot_id text NOT NULL
            REFERENCES news_narrative_grouping_snapshots(grouping_snapshot_id) ON DELETE RESTRICT,
          policy_version text NOT NULL CHECK (btrim(policy_version) <> ''),
          cutoff_at_ms bigint NOT NULL CHECK (cutoff_at_ms >= 0),
          selected_story_ids jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(selected_story_ids) = 'array'),
          decisions jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(decisions) = 'array'),
          critical boolean NOT NULL DEFAULT false,
          evidence_bundle_hash text NOT NULL CHECK (btrim(evidence_bundle_hash) <> ''),
          evidence_bundle jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(evidence_bundle) = 'object'),
          status text NOT NULL
            CHECK (status IN ('planned', 'debounced', 'publishable', 'published', 'superseded')),
          publish_after_ms bigint NOT NULL CHECK (publish_after_ms >= 0),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_brief_selection_publishable
          ON news_brief_selection_snapshots(status, publish_after_ms, created_at_ms, selection_snapshot_id)
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_analysis_requests (
          request_id text PRIMARY KEY,
          story_id text NOT NULL REFERENCES news_stories(story_id) ON DELETE CASCADE,
          material_evidence_hash text NOT NULL CHECK (btrim(material_evidence_hash) <> ''),
          request_kind text NOT NULL CHECK (request_kind IN ('automatic', 'on_demand')),
          reason jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(reason) = 'object'),
          status text NOT NULL CHECK (status IN ('pending', 'claimed', 'published', 'failed', 'insufficient')),
          requested_at_ms bigint NOT NULL CHECK (requested_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0),
          UNIQUE (story_id, material_evidence_hash, request_kind)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_ai_attempts (
          attempt_key text PRIMARY KEY,
          publication_kind text NOT NULL CHECK (publication_kind IN ('brief', 'story_analysis')),
          target_id text NOT NULL CHECK (btrim(target_id) <> ''),
          evidence_hash text NOT NULL CHECK (btrim(evidence_hash) <> ''),
          model text NOT NULL CHECK (btrim(model) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          workflow_version text NOT NULL CHECK (btrim(workflow_version) <> ''),
          schema_version text NOT NULL CHECK (btrim(schema_version) <> ''),
          locale text NOT NULL CHECK (btrim(locale) <> ''),
          status text NOT NULL CHECK (status IN ('running', 'failed', 'available', 'insufficient')),
          attempt_count integer NOT NULL CHECK (attempt_count >= 1),
          repair_count integer NOT NULL DEFAULT 0 CHECK (repair_count BETWEEN 0 AND 1),
          lease_token text NOT NULL CHECK (btrim(lease_token) <> ''),
          lease_expires_at_ms bigint NOT NULL DEFAULT 0 CHECK (lease_expires_at_ms >= 0),
          next_attempt_at_ms bigint NOT NULL DEFAULT 0 CHECK (next_attempt_at_ms >= 0),
          validation_errors jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(validation_errors) = 'array'),
          last_error text,
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0),
          UNIQUE (
            publication_kind,
            target_id,
            evidence_hash,
            model,
            prompt_version,
            workflow_version,
            schema_version,
            locale
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_ai_attempts_claim
          ON news_ai_attempts(status, next_attempt_at_ms, lease_expires_at_ms, updated_at_ms)
        """
    )
    op.execute(
        """
        CREATE TABLE news_brief_publications (
          publication_id text PRIMARY KEY,
          selection_snapshot_id text NOT NULL
            REFERENCES news_brief_selection_snapshots(selection_snapshot_id) ON DELETE RESTRICT,
          selection_fingerprint text NOT NULL CHECK (btrim(selection_fingerprint) <> ''),
          evidence_bundle_hash text NOT NULL CHECK (btrim(evidence_bundle_hash) <> ''),
          model text NOT NULL CHECK (btrim(model) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          workflow_version text NOT NULL CHECK (btrim(workflow_version) <> ''),
          schema_version text NOT NULL CHECK (btrim(schema_version) <> ''),
          locale text NOT NULL CHECK (btrim(locale) <> ''),
          payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
          evidence_references jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(evidence_references) = 'array'),
          receipt jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(receipt) = 'object'),
          published_at_ms bigint NOT NULL CHECK (published_at_ms >= 0),
          UNIQUE (
            selection_fingerprint,
            evidence_bundle_hash,
            model,
            prompt_version,
            workflow_version,
            schema_version,
            locale
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_brief_current (
          singleton_key boolean PRIMARY KEY DEFAULT true CHECK (singleton_key),
          publication_id text NOT NULL
            REFERENCES news_brief_publications(publication_id) ON DELETE CASCADE,
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_analysis_publications (
          publication_id text PRIMARY KEY,
          story_id text NOT NULL REFERENCES news_stories(story_id) ON DELETE CASCADE,
          material_evidence_hash text NOT NULL CHECK (btrim(material_evidence_hash) <> ''),
          model text NOT NULL CHECK (btrim(model) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          workflow_version text NOT NULL CHECK (btrim(workflow_version) <> ''),
          schema_version text NOT NULL CHECK (btrim(schema_version) <> ''),
          locale text NOT NULL CHECK (btrim(locale) <> ''),
          payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
          evidence_references jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(evidence_references) = 'array'),
          receipt jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(receipt) = 'object'),
          published_at_ms bigint NOT NULL CHECK (published_at_ms >= 0),
          UNIQUE (
            story_id,
            material_evidence_hash,
            model,
            prompt_version,
            workflow_version,
            schema_version,
            locale
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_story_analysis_current (
          story_id text PRIMARY KEY REFERENCES news_stories(story_id) ON DELETE CASCADE,
          publication_id text NOT NULL
            REFERENCES news_story_analysis_publications(publication_id) ON DELETE CASCADE,
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260726_0198 is an irreversible professional News hard cut; the superseded Story v1 schema is not restored"
    )
