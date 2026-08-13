"""Keep Token Radar material replay on one narrow covering index.

Revision ID: 20260813_0261
Revises: 20260813_0260
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0261"
down_revision = "20260813_0260"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        DROP INDEX idx_events_token_radar_source_time;

        ALTER TABLE events
          ADD COLUMN token_radar_text_fingerprint text
          GENERATED ALWAYS AS (
            md5(NULLIF(btrim(regexp_replace(
              translate(
                COALESCE(text_clean, search_text, text, ''),
                'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                'abcdefghijklmnopqrstuvwxyz'
              ),
              E'[ \t\n\r\f]+', ' ', 'g'
            )), ''))
          ) STORED;

        CREATE INDEX idx_events_token_radar_source_time
          ON events (timestamp_ms, event_id)
          INCLUDE (
            token_radar_text_fingerprint,
            received_at_ms,
            created_at_ms,
            action,
            author_handle
          )
          WHERE source_provider = 'gmgn'
            AND source_transport = 'direct_ws'
            AND coverage = 'public_stream'
            AND channel IN (
              'twitter_monitor_basic',
              'twitter_monitor_token',
              'twitter_monitor_translation',
              'twitter_monitor_express'
            )
            AND action IN ('tweet', 'quote', 'reply', 'repost');
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260813_0261 is an irreversible Token Radar covering-read cut")
