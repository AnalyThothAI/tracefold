"""Add typed worker frontiers, persisted live cursors, and runtime status."""

from __future__ import annotations

from alembic import op

revision = "20260730_0222"
down_revision = "20260730_0221"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE worker_runtime_status (
          unit_name text PRIMARY KEY,
          runtime_id uuid NOT NULL,
          runtime_version text NOT NULL,
          effective_status text NOT NULL,
          heartbeat_at_ms bigint NOT NULL,
          last_started_at_ms bigint,
          last_finished_at_ms bigint,
          last_result_json jsonb,
          last_error text,
          deadline_at_ms bigint,
          queue_depth bigint,
          oldest_due_at_ms bigint,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT worker_runtime_status_effective_status_check
            CHECK (effective_status IN (
              'disabled', 'unavailable', 'degraded', 'running', 'stopped', 'failed'
            )),
          CONSTRAINT worker_runtime_status_queue_depth_check
            CHECK (queue_depth IS NULL OR queue_depth >= 0)
        );

        CREATE INDEX idx_worker_runtime_status_heartbeat
          ON worker_runtime_status(heartbeat_at_ms DESC, unit_name);

        CREATE TABLE persisted_live_events (
          cursor bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          source_key text NOT NULL UNIQUE,
          event_kind text NOT NULL,
          target_type text,
          target_id text,
          payload_json jsonb NOT NULL,
          committed_at_ms bigint NOT NULL,
          CONSTRAINT persisted_live_events_kind_check
            CHECK (event_kind IN ('event', 'live_market_update')),
          CONSTRAINT persisted_live_events_target_pair_check
            CHECK ((target_type IS NULL) = (target_id IS NULL))
        );

        CREATE INDEX idx_persisted_live_events_cursor
          ON persisted_live_events(cursor);
        CREATE INDEX idx_persisted_live_events_target_cursor
          ON persisted_live_events(target_type, target_id, cursor)
          WHERE target_type IS NOT NULL;

        CREATE TABLE radar_projection_frontiers (
          target_type text NOT NULL,
          target_id text NOT NULL,
          window_key text NOT NULL,
          venue text NOT NULL,
          status text NOT NULL,
          first_dirty_at_ms bigint,
          deadline_at_ms bigint,
          next_attempt_at_ms bigint,
          attempt_count integer NOT NULL DEFAULT 0,
          transient_failure_count integer NOT NULL DEFAULT 0,
          input_fingerprint text,
          projection_version text NOT NULL,
          claimed_by uuid,
          claimed_until_ms bigint,
          last_error_code text,
          updated_at_ms bigint NOT NULL,
          PRIMARY KEY(target_type, target_id, window_key, venue),
          CONSTRAINT radar_projection_frontiers_status_check
            CHECK (status IN ('clean', 'dirty', 'running', 'retry_wait', 'quarantined')),
          CONSTRAINT radar_projection_frontiers_attempt_check
            CHECK (attempt_count >= 0 AND transient_failure_count >= 0)
        );

        CREATE INDEX idx_radar_projection_frontiers_due
          ON radar_projection_frontiers(
            deadline_at_ms, window_key, venue, target_type, target_id
          )
          WHERE status IN ('dirty', 'retry_wait');

        CREATE TABLE radar_source_edges (
          target_type text NOT NULL,
          target_id text NOT NULL,
          window_key text NOT NULL,
          venue text NOT NULL,
          source_kind text NOT NULL,
          source_id text NOT NULL,
          observed_at_ms bigint NOT NULL,
          expires_at_ms bigint NOT NULL,
          input_fingerprint text NOT NULL,
          payload_json jsonb NOT NULL,
          updated_at_ms bigint NOT NULL,
          PRIMARY KEY(
            target_type, target_id, window_key, venue, source_kind, source_id
          ),
          CONSTRAINT radar_source_edges_expiry_check
            CHECK (expires_at_ms >= observed_at_ms)
        );

        CREATE INDEX idx_radar_source_edges_expiry
          ON radar_source_edges(
            expires_at_ms, window_key, venue, target_type, target_id
          );

        CREATE TABLE macro_module_frontiers (
          module_id text PRIMARY KEY,
          status text NOT NULL,
          first_dirty_at_ms bigint,
          deadline_at_ms bigint,
          next_attempt_at_ms bigint,
          attempt_count integer NOT NULL DEFAULT 0,
          transient_failure_count integer NOT NULL DEFAULT 0,
          source_frontier_ms bigint,
          input_fingerprint text,
          projection_version text NOT NULL,
          claimed_by uuid,
          claimed_until_ms bigint,
          last_error_code text,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT macro_module_frontiers_status_check
            CHECK (status IN ('clean', 'dirty', 'running', 'retry_wait', 'quarantined')),
          CONSTRAINT macro_module_frontiers_attempt_check
            CHECK (attempt_count >= 0 AND transient_failure_count >= 0)
        );

        CREATE INDEX idx_macro_module_frontiers_due
          ON macro_module_frontiers(deadline_at_ms, module_id)
          WHERE status IN ('dirty', 'retry_wait');

        CREATE TABLE macro_dataset_projection_states (
          dataset_id text PRIMARY KEY,
          material_fingerprint text NOT NULL,
          acquisition_status text NOT NULL,
          source_frontier_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL
        );

        CREATE TABLE news_projection_frontiers (
          bucket_id text PRIMARY KEY,
          status text NOT NULL,
          first_dirty_at_ms bigint,
          deadline_at_ms bigint,
          next_attempt_at_ms bigint,
          attempt_count integer NOT NULL DEFAULT 0,
          transient_failure_count integer NOT NULL DEFAULT 0,
          active_item_count integer NOT NULL DEFAULT 0,
          input_fingerprint text,
          projection_version text NOT NULL,
          claimed_by uuid,
          claimed_until_ms bigint,
          last_error_code text,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT news_projection_frontiers_status_check
            CHECK (status IN ('clean', 'dirty', 'running', 'retry_wait', 'quarantined')),
          CONSTRAINT news_projection_frontiers_attempt_check
            CHECK (attempt_count >= 0 AND transient_failure_count >= 0),
          CONSTRAINT news_projection_frontiers_count_check CHECK (active_item_count >= 0)
        );

        CREATE INDEX idx_news_projection_frontiers_due
          ON news_projection_frontiers(deadline_at_ms, bucket_id)
          WHERE status IN ('dirty', 'retry_wait');

        CREATE TABLE news_identity_features (
          item_id text PRIMARY KEY REFERENCES news_items(item_id) ON DELETE CASCADE,
          normalized_title text NOT NULL,
          candidate_tokens text[] NOT NULL,
          feature_fingerprint text NOT NULL,
          published_at_ms bigint NOT NULL,
          expires_at_ms bigint NOT NULL,
          active boolean NOT NULL,
          updated_at_ms bigint NOT NULL,
          CONSTRAINT news_identity_features_expiry_check
            CHECK (expires_at_ms >= published_at_ms)
        );

        CREATE INDEX idx_news_identity_features_tokens
          ON news_identity_features USING gin(candidate_tokens);
        CREATE INDEX idx_news_identity_features_expiry
          ON news_identity_features(expires_at_ms, item_id)
          WHERE active;

        CREATE TABLE news_similarity_edges (
          left_item_id text NOT NULL
            REFERENCES news_items(item_id) ON DELETE CASCADE,
          right_item_id text NOT NULL
            REFERENCES news_items(item_id) ON DELETE CASCADE,
          similarity double precision NOT NULL,
          identity_version text NOT NULL,
          expires_at_ms bigint NOT NULL,
          updated_at_ms bigint NOT NULL,
          PRIMARY KEY(left_item_id, right_item_id),
          CONSTRAINT news_similarity_edges_order_check
            CHECK (left_item_id < right_item_id),
          CONSTRAINT news_similarity_edges_similarity_check
            CHECK (similarity >= 0.0 AND similarity <= 1.0)
        );

        CREATE INDEX idx_news_similarity_edges_left
          ON news_similarity_edges(left_item_id, expires_at_ms);
        CREATE INDEX idx_news_similarity_edges_right
          ON news_similarity_edges(right_item_id, expires_at_ms);

        CREATE TABLE token_profile_projection_frontiers (
          target_type text NOT NULL,
          target_id text NOT NULL,
          status text NOT NULL,
          first_dirty_at_ms bigint,
          deadline_at_ms bigint,
          next_attempt_at_ms bigint,
          attempt_count integer NOT NULL DEFAULT 0,
          transient_failure_count integer NOT NULL DEFAULT 0,
          input_fingerprint text,
          projection_version text NOT NULL,
          claimed_by uuid,
          claimed_until_ms bigint,
          last_error_code text,
          updated_at_ms bigint NOT NULL,
          PRIMARY KEY(target_type, target_id),
          CONSTRAINT token_profile_projection_frontiers_status_check
            CHECK (status IN ('clean', 'dirty', 'running', 'retry_wait', 'quarantined')),
          CONSTRAINT token_profile_projection_frontiers_attempt_check
            CHECK (attempt_count >= 0 AND transient_failure_count >= 0)
        );

        CREATE INDEX idx_token_profile_projection_frontiers_due
          ON token_profile_projection_frontiers(deadline_at_ms, target_type, target_id)
          WHERE status IN ('dirty', 'retry_wait');

        CREATE TABLE model_generation_frontiers (
          candidate_kind text NOT NULL,
          shard_key text NOT NULL,
          status text NOT NULL,
          first_dirty_at_ms bigint,
          deadline_at_ms bigint,
          next_attempt_at_ms bigint,
          attempt_count integer NOT NULL DEFAULT 0,
          transient_failure_count integer NOT NULL DEFAULT 0,
          input_fingerprint text,
          workflow_version text NOT NULL,
          claimed_by uuid,
          claimed_until_ms bigint,
          last_error_code text,
          updated_at_ms bigint NOT NULL,
          PRIMARY KEY(candidate_kind, shard_key),
          CONSTRAINT model_generation_frontiers_candidate_check
            CHECK (candidate_kind IN (
              'macro_thesis', 'news_brief', 'macro_document_analysis'
            )),
          CONSTRAINT model_generation_frontiers_status_check
            CHECK (status IN ('clean', 'dirty', 'running', 'retry_wait', 'quarantined')),
          CONSTRAINT model_generation_frontiers_attempt_check
            CHECK (attempt_count >= 0 AND transient_failure_count >= 0)
        );

        CREATE INDEX idx_model_generation_frontiers_due
          ON model_generation_frontiers(
            deadline_at_ms, candidate_kind, shard_key
          )
          WHERE status IN ('dirty', 'retry_wait');
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260730_0222 is an irreversible worker resource hard cut")
