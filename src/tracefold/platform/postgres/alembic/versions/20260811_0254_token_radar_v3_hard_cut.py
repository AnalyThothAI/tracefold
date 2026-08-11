"""Hard-cut Token Radar current state to the causal v3 contract.

Revision ID: 20260811_0254
Revises: 20260811_0253
"""

from __future__ import annotations

from alembic import op

revision = "20260811_0254"
down_revision = "20260811_0253"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        ALTER TABLE token_radar_current
          ADD COLUMN state_changed_at_ms bigint NOT NULL DEFAULT 0,
          ADD CONSTRAINT token_radar_current_state_changed_at_ms_check
            CHECK (state_changed_at_ms >= 0);

        CREATE INDEX idx_events_token_radar_source_time
          ON events (timestamp_ms, event_id)
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

        ALTER TABLE token_radar_current
          DROP CONSTRAINT token_radar_current_schema_check;

        UPDATE token_radar_current
           SET schema_version = 'token_radar_snapshot_v3',
               ruleset_version = NULL,
               ruleset_fingerprint = NULL,
               input_fingerprint = NULL,
               state_fingerprint = NULL,
               evidence_as_of_ms = 0,
               evaluation_at_ms = 0,
               input_rows = 0,
               input_bytes = 0,
               latest_attempt_status = 'never',
               latest_error_code = NULL,
               failure_count = 0,
               served_payload = jsonb_build_object(
                 'schema_version', 'token_radar_snapshot_v3',
                 'social_evidence_as_of_ms', 0,
                 'eligible_total', 0,
                 'items', jsonb_build_array()
               ),
               state_changed_at_ms = 0,
               created_at_ms = 0,
               updated_at_ms = 0
         WHERE singleton_key = true;

        ALTER TABLE token_radar_current
          ADD CONSTRAINT token_radar_current_schema_check CHECK (
            schema_version = 'token_radar_snapshot_v3'
            AND jsonb_typeof(served_payload) = 'object'
            AND served_payload ->> 'schema_version' = schema_version
            AND jsonb_typeof(served_payload -> 'social_evidence_as_of_ms') = 'number'
            AND (served_payload ->> 'social_evidence_as_of_ms')::bigint >= 0
            AND jsonb_typeof(served_payload -> 'eligible_total') = 'number'
            AND (served_payload ->> 'eligible_total')::integer >= 0
            AND jsonb_typeof(served_payload -> 'items') = 'array'
            AND jsonb_array_length(served_payload -> 'items') <= 50
            AND (served_payload ->> 'eligible_total')::integer
                >= jsonb_array_length(served_payload -> 'items')
            AND octet_length(served_payload::text) <= 131072
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260811_0254 is an irreversible Token Radar v3 hard cut")
