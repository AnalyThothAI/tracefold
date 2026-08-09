"""Hard-cut News to the bounded public WorldMonitor current-state chain.

Revision ID: 20260809_0247
Revises: 20260807_0246
"""

from __future__ import annotations

from alembic import op

revision = "20260809_0247"
down_revision = "20260807_0246"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        DELETE FROM queue_terminal_events
         WHERE owner_key = 'news_brief'
            OR source_table = 'news_brief_runs';

        DELETE FROM news_push_deliveries
         WHERE source_payload ->> 'schema_version'
                 IS DISTINCT FROM 'news_story_push_v1'
            OR (
              delivery_payload IS NOT NULL
              AND delivery_payload ->> 'schema_version'
                    IS DISTINCT FROM 'news_feishu_delivery_v2'
            );

        DROP TABLE news_brief_current;
        DROP TABLE news_brief_publications;
        DROP TABLE news_brief_runs;

        DELETE FROM news_brief_selection_current;
        DELETE FROM news_story_members;
        DELETE FROM news_stories;

        DELETE FROM news_items
         WHERE source_id IN (
           SELECT source_id FROM news_sources WHERE source_kind = 'rss'
         );
        DELETE FROM news_sources WHERE source_kind = 'rss';

        DROP TABLE news_story_facet_counts;
        DROP TABLE news_source_facet_counts;

        ALTER TABLE news_sources
          DROP CONSTRAINT news_sources_gap_boundary_provider_record_id_check,
          DROP CONSTRAINT news_sources_gap_version_check,
          DROP COLUMN gap_unclosed,
          DROP COLUMN gap_boundary_provider_record_id,
          DROP COLUMN gap_version,
          ADD COLUMN feed_url text,
          ADD COLUMN refresh_interval_seconds integer,
          ADD COLUMN etag text,
          ADD COLUMN last_modified text,
          ADD COLUMN next_fetch_at_ms bigint,
          ADD COLUMN claim_token uuid,
          ADD COLUMN claim_lease_expires_at_ms bigint,
          ADD COLUMN last_outcome text,
          ADD COLUMN last_rejection_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN last_items_seen integer NOT NULL DEFAULT 0,
          ADD COLUMN last_items_accepted integer NOT NULL DEFAULT 0,
          ADD CONSTRAINT news_sources_wire_control_check CHECK (
            (
              source_kind = 'rss'
              AND feed_url ~ '^https://'
              AND refresh_interval_seconds >= 1
              AND next_fetch_at_ms >= 0
            )
            OR (
              source_kind = 'opennews'
              AND feed_url IS NULL
              AND refresh_interval_seconds IS NULL
              AND etag IS NULL
              AND last_modified IS NULL
              AND next_fetch_at_ms IS NULL
              AND claim_token IS NULL
              AND claim_lease_expires_at_ms IS NULL
            )
          ),
          ADD CONSTRAINT news_sources_claim_check CHECK (
            (claim_token IS NULL AND claim_lease_expires_at_ms IS NULL)
            OR (
              source_kind = 'rss'
              AND claim_token IS NOT NULL
              AND claim_lease_expires_at_ms IS NOT NULL
              AND claim_lease_expires_at_ms >= 0
            )
          ),
          ADD CONSTRAINT news_sources_last_outcome_check CHECK (
            last_outcome IS NULL OR btrim(last_outcome) <> ''
          ),
          ADD CONSTRAINT news_sources_rejection_counts_check CHECK (
            jsonb_typeof(last_rejection_counts) = 'object'
          ),
          ADD CONSTRAINT news_sources_item_counts_check CHECK (
            last_items_seen >= 0
            AND last_items_accepted >= 0
            AND last_items_accepted <= last_items_seen
          );

        CREATE INDEX ix_news_sources_due_claim
          ON news_sources(next_fetch_at_ms, source_id, claim_lease_expires_at_ms)
          WHERE enabled AND source_kind = 'rss';

        ALTER TABLE news_items
          ADD COLUMN source_position smallint,
          ALTER COLUMN level DROP NOT NULL,
          ALTER COLUMN category DROP NOT NULL,
          ALTER COLUMN classification_source DROP NOT NULL,
          ALTER COLUMN classification_confidence DROP NOT NULL,
          ADD CONSTRAINT news_items_source_position_check CHECK (
            source_position IS NULL
            OR source_position BETWEEN 0 AND 4
          );

        ALTER TABLE news_projection_summary
          DROP COLUMN unmaterialized_item_count;

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

        CREATE TABLE news_brief_current (
          singleton_key boolean PRIMARY KEY DEFAULT true,
          slot_at_ms bigint,
          slot_status text NOT NULL,
          next_due_at_ms bigint NOT NULL,
          completed_at_ms bigint,
          lease_owner text,
          lease_token text,
          lease_expires_at_ms bigint,
          attempt_count integer NOT NULL DEFAULT 0,
          failure_count integer NOT NULL DEFAULT 0,
          model_outcome text,
          pointer_action text NOT NULL DEFAULT 'none',
          last_error_code text,
          last_attempt_at_ms bigint,
          active_selection jsonb,
          served_payload jsonb,
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT news_brief_current_singleton_check CHECK (singleton_key),
          CONSTRAINT news_brief_current_slot_status_check
            CHECK (slot_status IN ('due', 'running', 'completed')),
          CONSTRAINT news_brief_current_slot_clock_check CHECK (
            (slot_at_ms IS NULL OR (slot_at_ms >= 0 AND slot_at_ms % 1800000 = 0))
            AND next_due_at_ms >= 0
            AND (
              slot_at_ms IS NULL
              OR next_due_at_ms = slot_at_ms + 1800000
            )
          ),
          CONSTRAINT news_brief_current_lease_check CHECK (
            (
              slot_status = 'running'
              AND slot_at_ms IS NOT NULL
              AND completed_at_ms IS NULL
              AND NULLIF(btrim(lease_owner), '') IS NOT NULL
              AND NULLIF(btrim(lease_token), '') IS NOT NULL
              AND lease_expires_at_ms IS NOT NULL
              AND lease_expires_at_ms >= 0
              AND jsonb_typeof(active_selection) = 'object'
            )
            OR (
              slot_status <> 'running'
              AND lease_owner IS NULL
              AND lease_token IS NULL
              AND lease_expires_at_ms IS NULL
            )
          ),
          CONSTRAINT news_brief_current_completion_check CHECK (
            (
              slot_status = 'due'
              AND completed_at_ms IS NULL
              AND (
                (slot_at_ms IS NULL AND active_selection IS NULL)
                OR (
                  slot_at_ms IS NOT NULL
                  AND (
                    active_selection IS NULL
                    OR jsonb_typeof(active_selection) = 'object'
                  )
                )
              )
            )
            OR slot_status = 'running'
            OR (
              slot_status = 'completed'
              AND slot_at_ms IS NOT NULL
              AND completed_at_ms IS NOT NULL
              AND completed_at_ms >= slot_at_ms
              AND jsonb_typeof(active_selection) = 'object'
            )
          ),
          CONSTRAINT news_brief_current_attempt_counts_check CHECK (
            attempt_count >= 0
            AND failure_count >= 0
            AND failure_count <= attempt_count
          ),
          CONSTRAINT news_brief_current_model_outcome_check CHECK (
            model_outcome IS NULL OR model_outcome IN ('ok', 'l2', 'none')
          ),
          CONSTRAINT news_brief_current_pointer_action_check CHECK (
            pointer_action IN (
              'advance_ok', 'advance_degraded', 'preserve_lkg', 'none'
            )
          ),
          CONSTRAINT news_brief_current_error_check CHECK (
            last_error_code IS NULL OR btrim(last_error_code) <> ''
          ),
          CONSTRAINT news_brief_current_json_check CHECK (
            (active_selection IS NULL OR jsonb_typeof(active_selection) = 'object')
            AND (served_payload IS NULL OR jsonb_typeof(served_payload) = 'object')
          ),
          CONSTRAINT news_brief_current_clocks_check CHECK (
            created_at_ms >= 0
            AND updated_at_ms >= created_at_ms
            AND (
              last_attempt_at_ms IS NULL
              OR (
                last_attempt_at_ms >= created_at_ms
                AND updated_at_ms >= last_attempt_at_ms
              )
            )
            AND (
              completed_at_ms IS NULL
              OR updated_at_ms >= completed_at_ms
            )
          )
        );

        INSERT INTO news_brief_current(
          singleton_key, slot_status, next_due_at_ms,
          attempt_count, failure_count, pointer_action,
          created_at_ms, updated_at_ms
        )
        VALUES (true, 'due', 0, 0, 0, 'none', 0, 0);

        ALTER TABLE news_brief_current OWNER TO tracefold_owner;
        GRANT SELECT ON news_brief_current TO tracefold_serve;
        GRANT SELECT, INSERT, UPDATE, DELETE ON news_brief_current
          TO tracefold_workers;

        ANALYZE news_sources;
        ANALYZE news_items;
        ANALYZE news_projection_summary;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260809_0247 is an irreversible public News hard cut")
