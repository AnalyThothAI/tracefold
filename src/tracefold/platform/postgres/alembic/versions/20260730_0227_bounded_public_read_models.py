"""Persist bounded public News, Stocks Radar, and search read paths."""

from __future__ import annotations

from alembic import op

revision = "20260730_0227"
down_revision = "20260730_0226"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS btree_gin;

        CREATE INDEX idx_events_search_window_tsv
          ON events USING gin(received_at_ms, search_tsv);

        CREATE TABLE stock_attention_target_features (
          window_key text NOT NULL
            CHECK (window_key IN ('5m', '1h', '4h', '24h')),
          target_id text NOT NULL,
          symbol text NOT NULL,
          security_name text NOT NULL,
          exchange text NOT NULL,
          instrument_type text NOT NULL,
          mentions integer NOT NULL
            CHECK (mentions > 0),
          unique_authors integer NOT NULL
            CHECK (unique_authors >= 0),
          latest_seen_ms bigint NOT NULL
            CHECK (latest_seen_ms >= 0),
          latest_event_id text NOT NULL
            REFERENCES events(event_id) ON DELETE RESTRICT,
          latest_author_handle text,
          latest_text text NOT NULL,
          source_event_ids text[] NOT NULL,
          state_fingerprint text NOT NULL
            CHECK (btrim(state_fingerprint) <> ''),
          computed_at_ms bigint NOT NULL
            CHECK (computed_at_ms >= 0),
          PRIMARY KEY (window_key, target_id)
        );

        CREATE TABLE stocks_radar_current_rows (
          window_key text NOT NULL
            CHECK (window_key IN ('5m', '1h', '4h', '24h')),
          target_id text NOT NULL,
          rank integer NOT NULL
            CHECK (rank BETWEEN 1 AND 100),
          symbol text NOT NULL,
          security_name text NOT NULL,
          exchange text NOT NULL,
          instrument_type text NOT NULL,
          mentions integer NOT NULL
            CHECK (mentions > 0),
          unique_authors integer NOT NULL
            CHECK (unique_authors >= 0),
          latest_seen_ms bigint NOT NULL
            CHECK (latest_seen_ms >= 0),
          latest_event_id text NOT NULL
            REFERENCES events(event_id) ON DELETE RESTRICT,
          latest_author_handle text,
          latest_text text NOT NULL,
          source_event_ids text[] NOT NULL,
          state_fingerprint text NOT NULL
            CHECK (btrim(state_fingerprint) <> ''),
          computed_at_ms bigint NOT NULL
            CHECK (computed_at_ms >= 0),
          PRIMARY KEY (window_key, target_id),
          UNIQUE (window_key, rank)
        );

        CREATE TABLE stocks_radar_publication_state (
          window_key text PRIMARY KEY
            CHECK (window_key IN ('5m', '1h', '4h', '24h')),
          state_fingerprint text NOT NULL
            CHECK (btrim(state_fingerprint) <> ''),
          source_frontier_ms bigint NOT NULL
            CHECK (source_frontier_ms >= 0),
          published_at_ms bigint NOT NULL
            CHECK (published_at_ms >= 0)
        );

        CREATE TABLE news_story_facet_counts (
          facet_type text NOT NULL
            CHECK (facet_type IN ('category', 'level')),
          facet_value text NOT NULL
            CHECK (btrim(facet_value) <> ''),
          story_count integer NOT NULL
            CHECK (story_count > 0),
          updated_at_ms bigint NOT NULL
            CHECK (updated_at_ms >= 0),
          PRIMARY KEY (facet_type, facet_value)
        );

        CREATE TABLE news_source_facet_counts (
          source_id text PRIMARY KEY
            REFERENCES news_sources(source_id) ON DELETE CASCADE,
          story_count integer NOT NULL
            CHECK (story_count > 0),
          updated_at_ms bigint NOT NULL
            CHECK (updated_at_ms >= 0)
        );

        CREATE TABLE news_brief_selection_current (
          rank smallint PRIMARY KEY
            CHECK (rank BETWEEN 1 AND 8),
          story_id text NOT NULL UNIQUE
            REFERENCES news_stories(story_id) ON DELETE CASCADE,
          updated_at_ms bigint NOT NULL
            CHECK (updated_at_ms >= 0)
        );

        CREATE INDEX ix_news_stories_source_importance
          ON news_stories(
            representative_source_id,
            importance_score DESC,
            last_published_at_ms DESC,
            story_id
          )
          WHERE active;

        INSERT INTO news_story_facet_counts (
          facet_type, facet_value, story_count, updated_at_ms
        )
        SELECT
          facets.facet_type,
          facets.facet_value,
          facets.story_count,
          (extract(epoch FROM clock_timestamp()) * 1000)::bigint
        FROM (
          SELECT 'category'::text AS facet_type,
                 category AS facet_value,
                 count(*)::integer AS story_count
          FROM news_stories
          WHERE active
          GROUP BY category
          UNION ALL
          SELECT 'level'::text AS facet_type,
                 level AS facet_value,
                 count(*)::integer AS story_count
          FROM news_stories
          WHERE active
          GROUP BY level
        ) facets;

        INSERT INTO news_source_facet_counts (
          source_id, story_count, updated_at_ms
        )
        SELECT
          item.source_id,
          count(DISTINCT member.story_id)::integer,
          (extract(epoch FROM clock_timestamp()) * 1000)::bigint
        FROM news_story_members member
        JOIN news_stories story
          ON story.story_id = member.story_id
        JOIN news_items item
          ON item.item_id = member.item_id
        WHERE story.active
          AND member.current
        GROUP BY item.source_id;

        INSERT INTO news_brief_selection_current (
          rank, story_id, updated_at_ms
        )
        SELECT
          row_number() OVER (
            ORDER BY candidate.importance_score DESC,
                     candidate.last_published_at_ms DESC,
                     candidate.story_id
          )::smallint,
          candidate.story_id,
          (extract(epoch FROM clock_timestamp()) * 1000)::bigint
        FROM news_sources source
        CROSS JOIN LATERAL (
          SELECT story.story_id,
                 story.importance_score,
                 story.last_published_at_ms
          FROM news_stories story
          JOIN news_items item
            ON item.item_id = story.representative_item_id
          WHERE source.enabled
            AND story.active
            AND story.representative_source_id = source.source_id
            AND NOT item.brief_excluded
          ORDER BY story.importance_score DESC,
                   story.last_published_at_ms DESC,
                   story.story_id
          LIMIT 3
        ) candidate
        ORDER BY candidate.importance_score DESC,
                 candidate.last_published_at_ms DESC,
                 candidate.story_id
        LIMIT 8;

        ALTER TABLE news_story_facet_counts OWNER TO tracefold_owner;
        ALTER TABLE news_source_facet_counts OWNER TO tracefold_owner;
        ALTER TABLE news_brief_selection_current OWNER TO tracefold_owner;
        ALTER TABLE stock_attention_target_features OWNER TO tracefold_owner;
        ALTER TABLE stocks_radar_current_rows OWNER TO tracefold_owner;
        ALTER TABLE stocks_radar_publication_state OWNER TO tracefold_owner;

        GRANT SELECT ON
          news_story_facet_counts,
          news_source_facet_counts,
          news_brief_selection_current,
          stock_attention_target_features,
          stocks_radar_current_rows,
          stocks_radar_publication_state
        TO tracefold_serve;
        GRANT SELECT, INSERT, UPDATE, DELETE ON
          news_story_facet_counts,
          news_source_facet_counts,
          news_brief_selection_current,
          stock_attention_target_features,
          stocks_radar_current_rows,
          stocks_radar_publication_state
        TO tracefold_workers;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260730_0227 is an irreversible News read-model hard cut")
