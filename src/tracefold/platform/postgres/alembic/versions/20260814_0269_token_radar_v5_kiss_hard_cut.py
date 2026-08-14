"""Hard-cut Token Radar to the minimal v5 current singleton.

Revision ID: 20260814_0269
Revises: 20260813_0268
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0269"
down_revision = "20260813_0268"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        ALTER TABLE token_radar_current
          DROP CONSTRAINT token_radar_current_schema_check,
          DROP CONSTRAINT token_radar_current_fingerprint_check,
          DROP CONSTRAINT token_radar_current_status_check,
          DROP CONSTRAINT token_radar_current_error_check,
          DROP CONSTRAINT token_radar_current_counts_check,
          DROP CONSTRAINT token_radar_current_clocks_check,
          DROP CONSTRAINT token_radar_current_state_changed_at_ms_check,
          ADD COLUMN snapshot_fingerprint text;

        UPDATE token_radar_current
           SET served_payload = jsonb_build_object(
                 'schema_version', 'token_radar_snapshot_v5',
                 'social_evidence_as_of_ms', 0,
                 'eligible_total', 0,
                 'items', jsonb_build_array()
               ),
               snapshot_fingerprint =
                 'sha256:5ea0cbe27b8434069c6d9186408f5a372c5290b0c7f4d0f24d68f483df0bd8a8',
               updated_at_ms = 0
         WHERE singleton_key = true;

        ALTER TABLE token_radar_current
          DROP COLUMN schema_version,
          DROP COLUMN ruleset_version,
          DROP COLUMN ruleset_fingerprint,
          DROP COLUMN input_fingerprint,
          DROP COLUMN state_fingerprint,
          DROP COLUMN evidence_as_of_ms,
          DROP COLUMN evaluation_at_ms,
          DROP COLUMN input_rows,
          DROP COLUMN input_bytes,
          DROP COLUMN latest_attempt_status,
          DROP COLUMN latest_error_code,
          DROP COLUMN failure_count,
          DROP COLUMN created_at_ms,
          DROP COLUMN state_changed_at_ms,
          ALTER COLUMN snapshot_fingerprint SET NOT NULL,
          ADD CONSTRAINT token_radar_current_snapshot_fingerprint_check CHECK (
            snapshot_fingerprint ~ '^sha256:[0-9a-f]{64}$'
          ),
          ADD CONSTRAINT token_radar_current_schema_check CHECK (
            jsonb_typeof(served_payload) = 'object'
            AND served_payload ?& ARRAY[
              'schema_version',
              'social_evidence_as_of_ms',
              'eligible_total',
              'items'
            ]
            AND served_payload - ARRAY[
              'schema_version',
              'social_evidence_as_of_ms',
              'eligible_total',
              'items'
            ] = '{}'::jsonb
            AND served_payload ->> 'schema_version' = 'token_radar_snapshot_v5'
            AND jsonb_typeof(served_payload -> 'social_evidence_as_of_ms') = 'number'
            AND (served_payload ->> 'social_evidence_as_of_ms')::bigint >= 0
            AND jsonb_typeof(served_payload -> 'eligible_total') = 'number'
            AND (served_payload ->> 'eligible_total')::integer >= 0
            AND jsonb_typeof(served_payload -> 'items') = 'array'
            AND jsonb_array_length(served_payload -> 'items') <= 50
            AND jsonb_array_length(served_payload -> 'items') = LEAST(
              (served_payload ->> 'eligible_total')::integer,
              50
            )
            AND octet_length(served_payload::text) <= 98304
          ),
          ADD CONSTRAINT token_radar_current_updated_at_ms_check CHECK (
            updated_at_ms >= 0
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260814_0269 is an irreversible Token Radar v5 hard cut")
