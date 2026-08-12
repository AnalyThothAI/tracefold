"""Hard-cut Token Radar current state to the four-hour v4 contract.

Revision ID: 20260812_0255
Revises: 20260811_0254
"""

from __future__ import annotations

from alembic import op

revision = "20260812_0255"
down_revision = "20260811_0254"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        DROP INDEX idx_events_token_radar_source_time;
        CREATE INDEX idx_events_token_radar_source_time
          ON events (
            timestamp_ms,
            event_id,
            md5(NULLIF(btrim(regexp_replace(
              translate(
                COALESCE(text_clean, search_text, text, ''),
                'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                'abcdefghijklmnopqrstuvwxyz'
              ),
              E'[ \t\n\r\f]+', ' ', 'g'
            )), ''))
          )
          INCLUDE (received_at_ms, created_at_ms, action, author_handle)
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

        CREATE INDEX idx_token_intent_resolutions_token_radar_material
          ON token_intent_resolutions (
            event_id, intent_id, decision_time_ms, created_at_ms, resolution_id
          )
          INCLUDE (resolution_status, target_type, target_id);

        ALTER TABLE token_radar_current
          DROP CONSTRAINT token_radar_current_schema_check,
          DROP CONSTRAINT token_radar_current_counts_check;

        UPDATE token_radar_current
           SET schema_version = 'token_radar_snapshot_v4',
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
                 'schema_version', 'token_radar_snapshot_v4',
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
            schema_version = 'token_radar_snapshot_v4'
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
          ),
          ADD CONSTRAINT token_radar_current_counts_check CHECK (
            evidence_as_of_ms >= 0
            AND evaluation_at_ms >= 0
            AND input_rows BETWEEN 0 AND 20000
            AND input_bytes BETWEEN 0 AND 16777216
            AND failure_count >= 0
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260812_0255 is an irreversible Token Radar v4 hard cut")
