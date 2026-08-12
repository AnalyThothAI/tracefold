"""Persist deterministic facet facts with each materialized News Story.

Revision ID: 20260813_0256
Revises: 20260812_0255
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0256"
down_revision = "20260812_0255"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        ALTER TABLE news_stories
          ADD COLUMN facet_facts jsonb;

        UPDATE news_stories story
           SET facet_facts = jsonb_build_object(
             'source_ids',
             COALESCE(
               (
                 SELECT jsonb_agg(source_id ORDER BY source_id COLLATE "C")
                   FROM (
                     SELECT DISTINCT item.source_id
                       FROM news_story_members member
                       JOIN news_items item ON item.item_id = member.item_id
                      WHERE member.story_id = story.story_id
                   ) source_values
               ),
               '[]'::jsonb
             ),
             'reporting_origins',
             COALESCE(
               (
                 SELECT jsonb_agg(
                          reporting_origin
                          ORDER BY reporting_origin COLLATE "C"
                        )
                   FROM (
                     SELECT DISTINCT btrim(item.reporting_origin) AS reporting_origin
                       FROM news_story_members member
                       JOIN news_items item ON item.item_id = member.item_id
                      WHERE member.story_id = story.story_id
                        AND NULLIF(btrim(item.reporting_origin), '') IS NOT NULL
                   ) origin_values
               ),
               '[]'::jsonb
             )
           );

        ALTER TABLE news_stories
          ALTER COLUMN facet_facts SET NOT NULL,
          ADD CONSTRAINT news_stories_facet_facts_check CHECK (
            jsonb_typeof(facet_facts) = 'object'
            AND facet_facts ? 'source_ids'
            AND jsonb_typeof(facet_facts -> 'source_ids') = 'array'
            AND facet_facts ? 'reporting_origins'
            AND jsonb_typeof(facet_facts -> 'reporting_origins') = 'array'
          );

        UPDATE news_projection_summary
           SET input_fingerprint = NULL;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260813_0256 is an irreversible News Story facet-facts cut")
