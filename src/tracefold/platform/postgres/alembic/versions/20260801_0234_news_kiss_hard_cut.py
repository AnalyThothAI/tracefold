"""NewsLiquid intake and WorldMonitor current-Story KISS hard cut.

Revision ID: 20260801_0234
Revises: 20260731_0233
"""

from __future__ import annotations

from alembic import op

revision = "20260801_0234"
down_revision = "20260731_0233"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE news_sources
          ADD COLUMN source_kind text NOT NULL DEFAULT 'rss',
          ADD COLUMN live_connected boolean NOT NULL DEFAULT false,
          ADD COLUMN last_live_at_ms bigint,
          ADD COLUMN last_recovery_at_ms bigint,
          ADD COLUMN gap_unclosed boolean NOT NULL DEFAULT false,
          ADD CONSTRAINT news_sources_source_kind_check
            CHECK (source_kind IN ('rss', 'opennews')),
          ADD CONSTRAINT news_sources_last_live_at_ms_check
            CHECK (last_live_at_ms IS NULL OR last_live_at_ms >= 0),
          ADD CONSTRAINT news_sources_last_recovery_at_ms_check
            CHECK (last_recovery_at_ms IS NULL OR last_recovery_at_ms >= 0);

        ALTER TABLE news_source_fetches
          DROP CONSTRAINT news_source_fetches_fetch_path_check;
        ALTER TABLE news_source_fetches
          ADD CONSTRAINT news_source_fetches_fetch_path_check
            CHECK (fetch_path IN ('direct', 'relay', 'opennews_rest'));

        ALTER TABLE news_feed_observations
          ADD COLUMN observation_kind text,
          ADD COLUMN payload_fingerprint text;

        UPDATE news_feed_observations
           SET observation_kind = 'report',
               payload_fingerprint = encode(
                 sha256(convert_to(raw::text, 'UTF8')),
                 'hex'
               );

        WITH ranked AS (
          SELECT ctid,
                 row_number() OVER (
                   PARTITION BY source_id, source_item_key,
                                observation_kind, payload_fingerprint
                   ORDER BY created_at_ms, observed_at_ms, observation_id
                 ) AS position
            FROM news_feed_observations
        )
        DELETE FROM news_feed_observations observation
        USING ranked
        WHERE observation.ctid = ranked.ctid
          AND ranked.position > 1;

        ALTER TABLE news_feed_observations
          DROP CONSTRAINT news_feed_observations_fetch_id_source_item_key_key,
          DROP CONSTRAINT news_feed_observations_fetch_id_fkey,
          ALTER COLUMN fetch_id DROP NOT NULL,
          ALTER COLUMN observation_kind SET NOT NULL,
          ALTER COLUMN payload_fingerprint SET NOT NULL,
          ADD CONSTRAINT news_feed_observations_fetch_id_fkey
            FOREIGN KEY (fetch_id) REFERENCES news_source_fetches(fetch_id)
            ON DELETE SET NULL,
          ADD CONSTRAINT news_feed_observations_kind_check
            CHECK (observation_kind IN ('report', 'translation', 'provider_annotation')),
          ADD CONSTRAINT news_feed_observations_payload_fingerprint_check
            CHECK (payload_fingerprint ~ '^[0-9a-f]{64}$'),
          ADD CONSTRAINT news_feed_observations_semantic_key
            UNIQUE (source_id, source_item_key, observation_kind, payload_fingerprint);

        UPDATE news_feed_observations
           SET observation_id = 'news_observation_' || substr(
             encode(
               sha256(
                 convert_to(
                   concat_ws(
                     chr(31),
                     'news_observation',
                     source_id,
                     source_item_key,
                     observation_kind,
                     payload_fingerprint
                   ),
                   'UTF8'
                 )
               ),
               'hex'
             ),
             1,
             32
           );

        ALTER TABLE news_items
          DROP CONSTRAINT news_items_canonical_url_check,
          ALTER COLUMN canonical_url DROP NOT NULL,
          ADD COLUMN winning_observation_id text,
          ADD CONSTRAINT news_items_canonical_url_check
            CHECK (canonical_url IS NULL OR canonical_url ~ '^https?://');

        DELETE FROM news_brief_selection_current;
        DELETE FROM news_story_facet_counts;
        DELETE FROM news_source_facet_counts;
        DELETE FROM news_story_members;
        DELETE FROM news_stories;

        DROP INDEX IF EXISTS ix_news_stories_category;
        DROP INDEX IF EXISTS ix_news_stories_importance_feed;
        DROP INDEX IF EXISTS ix_news_stories_latest_feed;
        DROP INDEX IF EXISTS ix_news_story_members_current_story;
        DROP INDEX IF EXISTS ux_news_story_members_current_item;

        ALTER TABLE news_stories
          DROP CONSTRAINT news_stories_representative_url_check,
          ALTER COLUMN representative_url DROP NOT NULL,
          DROP COLUMN active,
          ADD CONSTRAINT news_stories_representative_url_check
            CHECK (representative_url IS NULL OR representative_url ~ '^https?://');

        ALTER TABLE news_story_members
          DROP CONSTRAINT news_story_members_check,
          DROP CONSTRAINT news_story_members_first_joined_at_ms_check,
          DROP COLUMN current,
          DROP COLUMN first_joined_at_ms,
          DROP COLUMN last_confirmed_at_ms;

        ALTER TABLE news_projection_summary
          ADD COLUMN input_fingerprint text,
          ADD COLUMN projection_version text,
          ADD COLUMN last_attempt_at_ms bigint,
          ADD COLUMN last_error text,
          ADD CONSTRAINT news_projection_summary_input_fingerprint_check
            CHECK (input_fingerprint IS NULL OR input_fingerprint ~ '^[0-9a-f]{64}$'),
          ADD CONSTRAINT news_projection_summary_last_attempt_at_ms_check
            CHECK (last_attempt_at_ms IS NULL OR last_attempt_at_ms >= 0);

        CREATE INDEX ix_news_stories_category
          ON news_stories(category, importance_score DESC, last_published_at_ms DESC, story_id);
        CREATE INDEX ix_news_stories_importance_feed
          ON news_stories(importance_score DESC, last_published_at_ms DESC, story_id);
        CREATE INDEX ix_news_stories_latest_feed
          ON news_stories(last_published_at_ms DESC, importance_score DESC, story_id);
        CREATE INDEX ix_news_story_members_story
          ON news_story_members(story_id, item_id);
        CREATE UNIQUE INDEX ux_news_story_members_item
          ON news_story_members(item_id);

        DROP TABLE news_similarity_edges;
        DROP TABLE news_identity_features;
        DROP TABLE news_story_aliases;
        DROP TABLE news_story_input_state;
        DROP TABLE news_projection_frontiers;

        UPDATE news_projection_summary
           SET active_item_count = 0,
               active_story_count = 0,
               unmaterialized_item_count = 0,
               invalid_owner_count = 0,
               invalid_story_aggregate_count = 0,
               newest_item_at_ms = NULL,
               newest_story_at_ms = NULL,
               last_material_change_at_ms = NULL,
               input_fingerprint = NULL,
               projection_version = NULL,
               last_attempt_at_ms = NULL,
               last_error = NULL,
               updated_at_ms = 0
         WHERE singleton_key = 'current';
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260801_0234 is an irreversible News KISS hard cut")
