"""Publish the rich Top-50 Token Radar contract and retire Stocks Radar.

Revision ID: 20260810_0250
Revises: 20260810_0249
"""

from __future__ import annotations

from alembic import op

revision = "20260810_0250"
down_revision = "20260810_0249"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        DROP TABLE stock_attention_target_features;
        DROP TABLE stocks_radar_current_rows;
        DROP TABLE stocks_radar_publication_state;

        ALTER TABLE token_radar_current
          DROP CONSTRAINT token_radar_current_schema_check;

        UPDATE token_radar_current
           SET schema_version = 'token_radar_snapshot_v2',
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
                 'schema_version', 'token_radar_snapshot_v2',
                 'evidence_as_of_ms', 0,
                 'eligible_total', 0,
                 'items', jsonb_build_array()
               ),
               created_at_ms = 0,
               updated_at_ms = 0
         WHERE singleton_key = true;

        ALTER TABLE token_radar_current
          ADD CONSTRAINT token_radar_current_schema_check CHECK (
            schema_version = 'token_radar_snapshot_v2'
            AND served_payload ->> 'schema_version' = schema_version
            AND jsonb_typeof(served_payload) = 'object'
            AND jsonb_typeof(served_payload -> 'items') = 'array'
            AND jsonb_array_length(served_payload -> 'items') <= 50
            AND (served_payload ->> 'evidence_as_of_ms')::bigint >= 0
            AND (served_payload ->> 'eligible_total')::integer >= 0
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260810_0250 is an irreversible product hard cut")
