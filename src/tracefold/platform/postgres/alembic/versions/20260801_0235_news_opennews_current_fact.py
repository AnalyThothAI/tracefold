"""Keep OpenNews current facts; remove News acquisition audit history.

Revision ID: 20260801_0235
Revises: 20260801_0234
"""

from __future__ import annotations

from alembic import op

revision = "20260801_0235"
down_revision = "20260801_0234"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE news_items
          ADD COLUMN provider_record_id text,
          ADD COLUMN provider_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD CONSTRAINT news_items_provider_record_id_check
            CHECK (
              provider_record_id IS NULL
              OR btrim(provider_record_id) <> ''
            ),
          ADD CONSTRAINT news_items_provider_metadata_check
            CHECK (jsonb_typeof(provider_metadata) = 'object');

        UPDATE news_sources
           SET enabled = (source_kind = 'opennews')
         WHERE enabled IS DISTINCT FROM (source_kind = 'opennews');

        DELETE FROM news_brief_selection_current;
        DELETE FROM news_story_facet_counts;
        DELETE FROM news_source_facet_counts;
        DELETE FROM news_story_members;
        DELETE FROM news_stories;

        DELETE FROM news_items item
        USING news_feed_observations winner
         WHERE item.winning_observation_id = winner.observation_id
           AND item.source_id = 'news-opennews'
           AND lower(btrim(COALESCE(winner.raw ->> 'newsType', ''))) = 'translation';

        UPDATE news_items item
           SET provider_record_id = COALESCE(
                 NULLIF(btrim(winner.raw ->> 'id'), ''),
                 winner.source_item_key
               )
          FROM news_feed_observations winner
         WHERE item.winning_observation_id = winner.observation_id
           AND item.source_id = 'news-opennews';

        WITH normalized AS MATERIALIZED (
          SELECT COALESCE(
                   NULLIF(btrim(observation.raw ->> 'id'), ''),
                   observation.source_item_key
                 ) AS provider_record_id,
                 CASE
                   WHEN jsonb_typeof(observation.raw -> 'score') = 'number'
                     THEN observation.raw -> 'score'
                   WHEN jsonb_typeof(observation.raw #> '{aiRating,score}') = 'number'
                     THEN observation.raw #> '{aiRating,score}'
                   ELSE NULL
                 END AS score,
                 NULLIF(
                   left(btrim(observation.raw ->> 'source'), 128),
                   ''
                 ) AS source,
                 COALESCE(
                   NULLIF(left(btrim(observation.raw ->> 'signal'), 32), ''),
                   NULLIF(left(btrim(observation.raw #>> '{aiRating,signal}'), 32), '')
                 ) AS signal,
                 COALESCE(
                   NULLIF(left(btrim(observation.raw ->> 'grade'), 32), ''),
                   NULLIF(left(btrim(observation.raw #>> '{aiRating,grade}'), 32), '')
                 ) AS grade,
                 CASE
                   WHEN jsonb_typeof(observation.raw -> 'coins') = 'array'
                    AND jsonb_array_length(observation.raw -> 'coins') > 0
                     THEN observation.raw -> 'coins'
                   ELSE NULL
                 END AS coins,
                 observation.observed_at_ms,
                 observation.created_at_ms,
                 observation.observation_id
            FROM news_feed_observations observation
           WHERE observation.source_id = 'news-opennews'
        ),
        providers AS (
          SELECT DISTINCT provider_record_id
            FROM normalized
        ),
        latest_score AS (
          SELECT DISTINCT ON (provider_record_id) provider_record_id, score
            FROM normalized
           WHERE score IS NOT NULL
           ORDER BY provider_record_id, observed_at_ms DESC,
                    created_at_ms DESC, observation_id DESC
        ),
        latest_signal AS (
          SELECT DISTINCT ON (provider_record_id) provider_record_id, signal
            FROM normalized
           WHERE signal IS NOT NULL
           ORDER BY provider_record_id, observed_at_ms DESC,
                    created_at_ms DESC, observation_id DESC
        ),
        latest_source AS (
          SELECT DISTINCT ON (provider_record_id) provider_record_id, source
            FROM normalized
           WHERE source IS NOT NULL
           ORDER BY provider_record_id, observed_at_ms DESC,
                    created_at_ms DESC, observation_id DESC
        ),
        latest_grade AS (
          SELECT DISTINCT ON (provider_record_id) provider_record_id, grade
            FROM normalized
           WHERE grade IS NOT NULL
           ORDER BY provider_record_id, observed_at_ms DESC,
                    created_at_ms DESC, observation_id DESC
        ),
        latest_coins AS (
          SELECT DISTINCT ON (provider_record_id) provider_record_id, coins
            FROM normalized
           WHERE coins IS NOT NULL
           ORDER BY provider_record_id, observed_at_ms DESC,
                    created_at_ms DESC, observation_id DESC
        ),
        metadata AS (
          SELECT providers.provider_record_id,
                 latest_score.score,
                 latest_source.source,
                 latest_signal.signal,
                 latest_grade.grade,
                 latest_coins.coins
            FROM providers
            LEFT JOIN latest_score USING (provider_record_id)
            LEFT JOIN latest_source USING (provider_record_id)
            LEFT JOIN latest_signal USING (provider_record_id)
            LEFT JOIN latest_grade USING (provider_record_id)
            LEFT JOIN latest_coins USING (provider_record_id)
        )
        UPDATE news_items item
           SET provider_metadata = jsonb_strip_nulls(
                 jsonb_build_object(
                   'score', metadata.score,
                   'source', metadata.source,
                   'signal', metadata.signal,
                   'grade', metadata.grade,
                   'coins', CASE
                     WHEN metadata.coins IS NULL THEN NULL
                     ELSE (
                       SELECT COALESCE(
                         jsonb_agg(
                           jsonb_strip_nulls(
                             jsonb_build_object(
                               'symbol', NULLIF(left(btrim(coin ->> 'symbol'), 32), ''),
                               'market_type', NULLIF(left(btrim(coin ->> 'market_type'), 32), ''),
                               'match', NULLIF(left(btrim(coin ->> 'match'), 64), ''),
                               'score', CASE
                                 WHEN jsonb_typeof(coin -> 'score') = 'number'
                                   THEN coin -> 'score'
                                 ELSE NULL
                               END,
                               'signal', NULLIF(left(coin ->> 'signal', 32), ''),
                               'grade', NULLIF(left(coin ->> 'grade', 32), '')
                             )
                           )
                           ORDER BY ordinal
                         ),
                         '[]'::jsonb
                       )
                         FROM jsonb_array_elements(metadata.coins)
                              WITH ORDINALITY AS entry(coin, ordinal)
                        WHERE jsonb_typeof(coin) = 'object'
                          AND ordinal <= 32
                          AND (
                            NULLIF(btrim(coin ->> 'symbol'), '') IS NOT NULL
                            OR NULLIF(btrim(coin ->> 'market_type'), '') IS NOT NULL
                          )
                     )
                   END
                 )
               )
          FROM metadata
         WHERE item.source_id = 'news-opennews'
           AND item.provider_record_id = metadata.provider_record_id;

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

        ALTER TABLE news_items
          DROP COLUMN winning_observation_id;

        CREATE UNIQUE INDEX ux_news_items_provider_record
          ON news_items(source_id, provider_record_id)
          WHERE provider_record_id IS NOT NULL;

        DROP TABLE news_feed_observations;
        DROP TABLE news_source_fetches;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260801_0235 is an irreversible News current-fact hard cut")
