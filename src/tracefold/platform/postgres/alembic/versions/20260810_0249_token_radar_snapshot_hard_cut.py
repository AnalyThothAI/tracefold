"""Replace legacy Radar projections with one compact current singleton.

Revision ID: 20260810_0249
Revises: 20260810_0248
"""

from __future__ import annotations

from alembic import op

revision = "20260810_0249"
down_revision = "20260810_0248"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        DELETE FROM queue_terminal_events
         WHERE owner_key = 'radar_projection'
            OR source_table IN (
              'token_radar_current_rows',
              'token_radar_publication_state',
              'token_radar_target_first_seen',
              'token_radar_target_features',
              'radar_source_edges',
              'radar_projection_frontiers'
            );

        ALTER TABLE queue_terminal_events
          DROP CONSTRAINT queue_terminal_events_owner_key_check,
          ADD CONSTRAINT queue_terminal_events_owner_key_check CHECK (
            owner_key IN (
              'event_anchor_backfill', 'resolution_refresh',
              'asset_profile_refresh', 'token_image_mirror',
              'profile_projection', 'macro_projection',
              'news_brief', 'macro_document_analysis'
            )
          );

        DROP TRIGGER token_intents_persisted_at_immutable ON token_intents;
        DROP TRIGGER token_intent_resolutions_persisted_at_immutable
          ON token_intent_resolutions;
        DROP TRIGGER market_ticks_persisted_at_immutable ON market_ticks;
        DROP TRIGGER registry_assets_persisted_at_immutable ON registry_assets;
        DROP TRIGGER cex_tokens_persisted_at_immutable ON cex_tokens;
        DROP TRIGGER price_feeds_persisted_at_immutable ON price_feeds;

        ALTER TABLE token_intents
          DROP CONSTRAINT token_intents_persisted_at_ms_check,
          DROP COLUMN persisted_at_ms;
        ALTER TABLE token_intent_resolutions
          DROP CONSTRAINT token_intent_resolutions_persisted_at_ms_check,
          DROP COLUMN persisted_at_ms;
        ALTER TABLE market_ticks
          DROP CONSTRAINT market_ticks_persisted_at_ms_check,
          DROP COLUMN persisted_at_ms;
        ALTER TABLE registry_assets
          DROP CONSTRAINT registry_assets_persisted_at_ms_check,
          DROP COLUMN persisted_at_ms;
        ALTER TABLE cex_tokens
          DROP CONSTRAINT cex_tokens_persisted_at_ms_check,
          DROP COLUMN persisted_at_ms;
        ALTER TABLE price_feeds
          DROP CONSTRAINT price_feeds_persisted_at_ms_check,
          DROP COLUMN persisted_at_ms;

        DROP FUNCTION enforce_fact_persisted_at_ms();

        DROP TABLE token_radar_current_rows;
        DROP TABLE token_radar_publication_state;
        DROP TABLE token_radar_target_first_seen;
        DROP TABLE token_radar_target_features;
        DROP TABLE radar_source_edges;
        DROP TABLE radar_projection_frontiers;

        CREATE TABLE token_radar_current (
          singleton_key boolean PRIMARY KEY DEFAULT true,
          schema_version text NOT NULL,
          ruleset_version text,
          ruleset_fingerprint text,
          input_fingerprint text,
          state_fingerprint text,
          evidence_as_of_ms bigint NOT NULL DEFAULT 0,
          evaluation_at_ms bigint NOT NULL DEFAULT 0,
          input_rows integer NOT NULL DEFAULT 0,
          input_bytes bigint NOT NULL DEFAULT 0,
          latest_attempt_status text NOT NULL DEFAULT 'never',
          latest_error_code text,
          failure_count integer NOT NULL DEFAULT 0,
          served_payload jsonb NOT NULL,
          created_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT token_radar_current_singleton_check CHECK (singleton_key),
          CONSTRAINT token_radar_current_schema_check CHECK (
            schema_version = 'token_radar_snapshot_v1'
            AND served_payload ->> 'schema_version' = schema_version
            AND jsonb_typeof(served_payload) = 'object'
            AND jsonb_typeof(served_payload -> 'items') = 'array'
            AND jsonb_array_length(served_payload -> 'items') <= 8
            AND (served_payload ->> 'evidence_as_of_ms')::bigint >= 0
            AND (served_payload ->> 'eligible_total')::integer >= 0
          ),
          CONSTRAINT token_radar_current_fingerprint_check CHECK (
            (
              ruleset_version IS NULL
              AND ruleset_fingerprint IS NULL
              AND input_fingerprint IS NULL
              AND state_fingerprint IS NULL
            )
            OR (
              NULLIF(btrim(ruleset_version), '') IS NOT NULL
              AND ruleset_fingerprint ~ '^sha256:[0-9a-f]{64}$'
              AND input_fingerprint ~ '^sha256:[0-9a-f]{64}$'
              AND state_fingerprint ~ '^sha256:[0-9a-f]{64}$'
            )
          ),
          CONSTRAINT token_radar_current_status_check CHECK (
            latest_attempt_status IN ('never', 'ready', 'failed')
          ),
          CONSTRAINT token_radar_current_error_check CHECK (
            (latest_attempt_status = 'failed' AND NULLIF(btrim(latest_error_code), '') IS NOT NULL)
            OR (latest_attempt_status <> 'failed' AND latest_error_code IS NULL)
          ),
          CONSTRAINT token_radar_current_counts_check CHECK (
            evidence_as_of_ms >= 0
            AND evaluation_at_ms >= 0
            AND input_rows BETWEEN 0 AND 10000
            AND input_bytes BETWEEN 0 AND 8388608
            AND failure_count >= 0
          ),
          CONSTRAINT token_radar_current_clocks_check CHECK (
            created_at_ms >= 0 AND updated_at_ms >= created_at_ms
          )
        );

        INSERT INTO token_radar_current(
          singleton_key, schema_version, served_payload,
          created_at_ms, updated_at_ms
        )
        VALUES (
          true,
          'token_radar_snapshot_v1',
          jsonb_build_object(
            'schema_version', 'token_radar_snapshot_v1',
            'evidence_as_of_ms', 0,
            'eligible_total', 0,
            'items', jsonb_build_array()
          ),
          0,
          0
        );

        ALTER TABLE token_radar_current OWNER TO tracefold_owner;
        GRANT SELECT ON token_radar_current TO tracefold_serve;
        GRANT SELECT, UPDATE ON token_radar_current TO tracefold_workers;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260810_0249 is an irreversible Token Radar hard cut")
