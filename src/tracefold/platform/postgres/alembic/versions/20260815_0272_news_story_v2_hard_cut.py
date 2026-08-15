"""Hard-cut News Story identity to one Jaccard/fact-coherent projection.

Revision ID: 20260815_0272
Revises: 20260815_0271
"""

from __future__ import annotations

from alembic import op

revision = "20260815_0272"
down_revision = "20260815_0271"
branch_labels = None
depends_on = None

_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS = (0x54524644, 0)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        f"""
        DO $migration$
        BEGIN
          IF NOT pg_try_advisory_xact_lock(
            {_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS[0]},
            {_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS[1]}
          ) THEN
            RAISE EXCEPTION 'news_story_v2_hard_cut_workers_active'
              USING ERRCODE = '55006';
          END IF;
        END
        $migration$;
        """
    )
    op.execute(
        """
        DELETE FROM news_brief_selection_current;
        DELETE FROM news_story_members;
        DELETE FROM news_stories;

        ALTER TABLE news_stories
          DROP COLUMN canonical_key,
          ADD COLUMN identity_evidence jsonb NOT NULL,
          ADD CONSTRAINT news_stories_identity_evidence_check CHECK (
            jsonb_typeof(identity_evidence) = 'object'
            AND octet_length(identity_evidence::text) <= 8192
            AND jsonb_typeof(identity_evidence -> 'strong_entity_keys') = 'array'
            AND jsonb_array_length(identity_evidence -> 'strong_entity_keys') <= 16
            AND jsonb_typeof(identity_evidence -> 'action_keys') = 'array'
            AND jsonb_array_length(identity_evidence -> 'action_keys') <= 16
            AND jsonb_typeof(identity_evidence -> 'numeric_keys') = 'array'
            AND jsonb_array_length(identity_evidence -> 'numeric_keys') <= 16
            AND jsonb_typeof(identity_evidence -> 'location_keys') = 'array'
            AND jsonb_array_length(identity_evidence -> 'location_keys') <= 16
            AND jsonb_typeof(identity_evidence -> 'membership_reasons') = 'object'
            AND jsonb_array_length(
              jsonb_path_query_array(identity_evidence -> 'membership_reasons', '$.keyvalue()')
            ) <= 32
            AND jsonb_typeof(identity_evidence -> 'rejection_reasons') = 'object'
            AND jsonb_array_length(
              jsonb_path_query_array(identity_evidence -> 'rejection_reasons', '$.keyvalue()')
            ) <= 32
            AND NULLIF(btrim(identity_evidence ->> 'identity_version'), '') IS NOT NULL
            AND NULLIF(btrim(identity_evidence ->> 'feature_version'), '') IS NOT NULL
            AND NULLIF(btrim(identity_evidence ->> 'jaccard_version'), '') IS NOT NULL
            AND NULLIF(btrim(identity_evidence ->> 'event_policy_version'), '') IS NOT NULL
            AND NULLIF(btrim(identity_evidence ->> 'clustering_version'), '') IS NOT NULL
            AND NULLIF(btrim(identity_evidence ->> 'anchor_item_id'), '') IS NOT NULL
          );

        UPDATE news_projection_summary
           SET active_item_count = 0,
               active_story_count = 0,
               invalid_owner_count = 0,
               invalid_story_aggregate_count = 0,
               newest_item_at_ms = NULL,
               newest_story_at_ms = NULL,
               last_material_change_at_ms = NULL,
               input_fingerprint = NULL,
               projection_version = NULL,
               last_attempt_at_ms = NULL,
               last_error = NULL,
               last_success_at_ms = NULL,
               updated_at_ms = 0
         WHERE singleton_key = 'current';

        UPDATE news_brief_current
           SET slot_at_ms = NULL,
               slot_status = 'due',
               next_due_at_ms = 0,
               completed_at_ms = NULL,
               lease_owner = NULL,
               lease_token = NULL,
               lease_expires_at_ms = NULL,
               attempt_count = 0,
               failure_count = 0,
               model_outcome = NULL,
               pointer_action = 'none',
               last_error_code = NULL,
               last_attempt_at_ms = NULL,
               active_selection = NULL,
               served_payload = NULL,
               created_at_ms = 0,
               updated_at_ms = 0
         WHERE singleton_key = true;

        ANALYZE news_stories;
        ANALYZE news_story_members;
        ANALYZE news_brief_selection_current;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260815_0272 is an irreversible News Story V2 hard cut")
