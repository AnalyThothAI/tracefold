"""Collapse hourly News score fanout into stable bounded buckets.

Revision ID: 20260731_0231
Revises: 20260731_0230
"""

from __future__ import annotations

from alembic import op

revision = "20260731_0231"
down_revision = "20260731_0230"
branch_labels = None
depends_on = None

_NEWS_PROJECTION_VERSION = (
    "worldmonitor_story_identity_f73de5b7:"
    "worldmonitor_keyword_classifier_f73de5b7:"
    "worldmonitor_importance_f73de5b7_physical_source:"
    "incremental-v1"
)


def upgrade() -> None:
    op.execute(
        f"""
        DELETE FROM news_projection_frontiers
         WHERE bucket_id LIKE 'score:%';

        WITH current_clock AS (
          SELECT (
            extract(epoch FROM clock_timestamp()) * 1000
          )::bigint AS now_ms
        ),
        active_buckets AS (
          SELECT
            (
              get_byte(decode(md5(story_id), 'hex'), 0) % 64
            )::integer AS score_bucket,
            sum(item_count)::integer AS active_item_count
          FROM news_stories
          WHERE active
          GROUP BY score_bucket
        )
        INSERT INTO news_projection_frontiers(
          bucket_id, status, first_dirty_at_ms, deadline_at_ms,
          next_attempt_at_ms, attempt_count, transient_failure_count,
          active_item_count, input_fingerprint, projection_version,
          claimed_by, claimed_until_ms, last_error_code, updated_at_ms
        )
        SELECT
          'score-bucket:' || lpad(score_bucket::text, 2, '0'),
          'dirty',
          current_clock.now_ms,
          current_clock.now_ms + 60000,
          NULL,
          0,
          0,
          active_buckets.active_item_count,
          md5(
            'score-bucket:'
            || score_bucket::text
            || ':'
            || current_clock.now_ms::text
          ),
          '{_NEWS_PROJECTION_VERSION}',
          NULL,
          NULL,
          NULL,
          current_clock.now_ms
        FROM active_buckets
        CROSS JOIN current_clock
        ON CONFLICT(bucket_id) DO UPDATE SET
          status = 'dirty',
          first_dirty_at_ms = EXCLUDED.first_dirty_at_ms,
          deadline_at_ms = EXCLUDED.deadline_at_ms,
          next_attempt_at_ms = NULL,
          attempt_count = 0,
          transient_failure_count = 0,
          active_item_count = EXCLUDED.active_item_count,
          input_fingerprint = EXCLUDED.input_fingerprint,
          projection_version = EXCLUDED.projection_version,
          claimed_by = NULL,
          claimed_until_ms = NULL,
          last_error_code = NULL,
          updated_at_ms = EXCLUDED.updated_at_ms;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260731_0231 is an irreversible News score-bucket hard cut")
