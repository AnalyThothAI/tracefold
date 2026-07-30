"""Persist the bounded News projection status summary."""

from __future__ import annotations

from alembic import op

revision = "20260730_0226"
down_revision = "20260730_0225"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE news_projection_summary (
          singleton_key text PRIMARY KEY
            CHECK (singleton_key = 'current'),
          active_item_count integer NOT NULL
            CHECK (active_item_count >= 0),
          active_story_count integer NOT NULL
            CHECK (active_story_count >= 0),
          unmaterialized_item_count integer NOT NULL
            CHECK (unmaterialized_item_count >= 0),
          invalid_owner_count integer NOT NULL
            CHECK (invalid_owner_count >= 0),
          invalid_story_aggregate_count integer NOT NULL
            CHECK (invalid_story_aggregate_count >= 0),
          newest_item_at_ms bigint,
          newest_story_at_ms bigint,
          last_material_change_at_ms bigint,
          updated_at_ms bigint NOT NULL
            CHECK (updated_at_ms >= 0)
        );

        INSERT INTO news_projection_summary (
          singleton_key,
          active_item_count,
          active_story_count,
          unmaterialized_item_count,
          invalid_owner_count,
          invalid_story_aggregate_count,
          newest_item_at_ms,
          newest_story_at_ms,
          last_material_change_at_ms,
          updated_at_ms
        )
        SELECT
          'current',
          (SELECT count(*) FROM news_items WHERE active),
          (SELECT count(*) FROM news_stories WHERE active),
          (
            SELECT count(*)
            FROM news_items item
            WHERE item.active
              AND NOT EXISTS (
                SELECT 1
                FROM news_story_members member
                WHERE member.item_id = item.item_id
                  AND member.current
              )
          ),
          (
            SELECT count(*)
            FROM (
              SELECT item.item_id
              FROM news_items item
              LEFT JOIN news_story_members member
                ON member.item_id = item.item_id
               AND member.current
              WHERE item.active
              GROUP BY item.item_id
              HAVING count(member.story_id) <> 1
            ) invalid_owner
          ),
          (
            SELECT count(*)
            FROM (
              SELECT story.story_id
              FROM news_stories story
              LEFT JOIN news_story_members member
                ON member.story_id = story.story_id
               AND member.current
              LEFT JOIN news_items item
                ON item.item_id = member.item_id
              WHERE story.active
              GROUP BY story.story_id
              HAVING story.item_count <> count(member.item_id)
                 OR story.source_count <> count(DISTINCT item.source_id)
                 OR story.first_published_at_ms <> min(item.published_at_ms)
                 OR story.last_published_at_ms <> max(item.published_at_ms)
                 OR NOT bool_or(member.item_id = story.representative_item_id)
                 OR NOT bool_or(member.item_id = story.scoring_item_id)
            ) invalid_story
          ),
          (SELECT max(published_at_ms) FROM news_items WHERE active),
          (SELECT max(last_published_at_ms) FROM news_stories WHERE active),
          (SELECT max(updated_at_ms) FROM news_stories WHERE active),
          (extract(epoch FROM clock_timestamp()) * 1000)::bigint;

        ALTER TABLE news_projection_summary OWNER TO tracefold_owner;
        GRANT SELECT ON news_projection_summary TO tracefold_serve;
        GRANT SELECT, INSERT, UPDATE, DELETE
          ON news_projection_summary TO tracefold_workers;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260730_0226 is an irreversible News summary hard cut")
