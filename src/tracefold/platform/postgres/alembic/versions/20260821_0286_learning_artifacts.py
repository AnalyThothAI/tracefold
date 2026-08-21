"""Content-addressed learning artifacts, per-case observations, and strict model recordings.

Revision ID: 20260821_0286
Revises: 20260821_0285
"""

from __future__ import annotations

from alembic import op

revision = "20260821_0286"
down_revision = "20260821_0285"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE news_learning_artifacts (
          artifact_sha      text   PRIMARY KEY,
          kind              text   NOT NULL,
          parent_sha        text,
          payload           jsonb  NOT NULL,
          created_by        text   NOT NULL,
          created_at_ms     bigint NOT NULL,
          CONSTRAINT news_learning_artifact_sha CHECK (artifact_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_learning_artifact_parent_sha CHECK (
            parent_sha IS NULL OR parent_sha ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT news_learning_artifact_kind CHECK (kind IN (
            'candidate_registration', 'proposal', 'candidate', 'dataset', 'evaluation_report', 'release_evidence',
            'active_agent', 'shadow_observation', 'canary_observation', 'deployment_receipt', 'rollback_receipt'
          )),
          CONSTRAINT news_learning_artifact_payload_object CHECK (jsonb_typeof(payload) = 'object'),
          CONSTRAINT news_learning_artifact_payload_size CHECK (pg_column_size(payload) <= 1048576)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_news_learning_artifacts_kind_created ON news_learning_artifacts (kind, created_at_ms DESC)"
    )

    op.execute(
        """
        CREATE TABLE news_learning_cases (
          run_sha              text   NOT NULL,
          case_id              text   NOT NULL,
          dataset_sha          text   NOT NULL,
          dataset_role         text   NOT NULL,
          evaluation_stage     text   NOT NULL,
          subject_kind         text   NOT NULL,
          event_id             text,
          evidence_version     integer,
          external_snapshot_id text,
          review_id            text,
          opened_at_ms         bigint NOT NULL,
          evidence_sha256      text   NOT NULL,
          cluster_id           text   NOT NULL,
          stratum              text   NOT NULL,
          stable_observation   jsonb  NOT NULL,
          candidate_observation jsonb NOT NULL,
          comparison           jsonb  NOT NULL,
          created_at_ms        bigint NOT NULL,
          PRIMARY KEY (run_sha, case_id),
          CONSTRAINT news_learning_case_run_sha CHECK (run_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_learning_case_dataset_sha CHECK (dataset_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_learning_case_evidence_sha CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_learning_case_subject CHECK (subject_kind IN ('event', 'external_miss', 'pairwise')),
          CONSTRAINT news_learning_case_role CHECK (dataset_role IN ('development', 'validation')),
          CONSTRAINT news_learning_case_stage CHECK (
            evaluation_stage IN ('offline', 'holdout', 'shadow', 'canary')
          ),
          CONSTRAINT news_learning_case_stable_object CHECK (jsonb_typeof(stable_observation) = 'object'),
          CONSTRAINT news_learning_case_candidate_object CHECK (jsonb_typeof(candidate_observation) = 'object'),
          CONSTRAINT news_learning_case_comparison_object CHECK (jsonb_typeof(comparison) = 'object')
        )
        """
    )
    op.execute("CREATE INDEX ix_news_learning_cases_dataset ON news_learning_cases (dataset_sha, cluster_id)")
    op.execute(
        """
        CREATE VIEW news_review_pairwise_tasks_v1 WITH (security_barrier = true) AS
        SELECT run_sha, case_id, dataset_sha, dataset_role, evaluation_stage, subject_kind,
               event_id, evidence_version, external_snapshot_id, review_id,
               opened_at_ms, evidence_sha256, cluster_id, stratum,
               CASE WHEN comparison ->> 'pair_order' = 'candidate_A'
                    THEN candidate_observation ELSE stable_observation END AS output_a,
               CASE WHEN comparison ->> 'pair_order' = 'candidate_A'
                    THEN stable_observation ELSE candidate_observation END AS output_b,
               jsonb_build_object(
                 'blind_task_version', COALESCE(comparison ->> 'blind_task_version', 'news_blind_pairwise_v1'),
                 'outcome_revealed', false
               ) AS disclosure,
               created_at_ms
          FROM news_learning_cases
         WHERE evaluation_stage IN ('offline', 'holdout')
           AND COALESCE((comparison ->> 'review_eligible')::boolean, false)
           AND review_id IS NOT NULL
        """
    )

    op.execute(
        """
        CREATE TABLE news_model_recordings (
          recording_sha       text    PRIMARY KEY,
          run_sha             text    NOT NULL,
          case_id             text    NOT NULL,
          arm                 text    NOT NULL,
          trial               integer NOT NULL,
          request_sha256      text    NOT NULL,
          response_sha256     text,
          request             jsonb   NOT NULL,
          response            jsonb,
          provider            text    NOT NULL,
          model               text    NOT NULL,
          model_sha           text    NOT NULL,
          execution_contract_sha text NOT NULL,
          latency_ms          integer,
          input_tokens        integer,
          output_tokens       integer,
          finish_reason       text,
          error_code          text,
          created_at_ms       bigint  NOT NULL,
          CONSTRAINT news_model_recording_sha CHECK (recording_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_model_recording_run_sha CHECK (run_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_model_recording_request_sha CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_model_recording_response_sha CHECK (
            response_sha256 IS NULL OR response_sha256 ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT news_model_recording_arm CHECK (arm IN ('stable', 'candidate')),
          CONSTRAINT news_model_recording_trial CHECK (trial >= 1 AND trial <= 3),
          CONSTRAINT news_model_recording_request_object CHECK (jsonb_typeof(request) = 'object'),
          CONSTRAINT news_model_recording_response_object CHECK (
            response IS NULL OR jsonb_typeof(response) = 'object'
          ),
          CONSTRAINT news_model_recording_request_size CHECK (pg_column_size(request) <= 65536),
          CONSTRAINT news_model_recording_response_size CHECK (
            response IS NULL OR pg_column_size(response) <= 65536
          )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_news_model_recording_trial ON news_model_recordings (run_sha, case_id, arm, trial)"
    )
    op.execute("CREATE INDEX ix_news_model_recording_created ON news_model_recordings (created_at_ms DESC)")

    op.execute(
        """
        CREATE FUNCTION reject_news_learning_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'news_learning_append_only';
        END;
        $$
        """
    )
    for table in ("news_learning_artifacts", "news_learning_cases", "news_model_recordings"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_news_learning_mutation()"
        )

    # ReviewDesk needs the current stable cohort to make its default queue
    # homogeneous. Expose only that public identity; the narrow review role
    # must not gain access to candidate manifests, reports, or arm mappings.
    op.execute(
        """
        CREATE VIEW news_review_active_agent_v1 WITH (security_barrier = true) AS
        SELECT payload ->> 'stable_sha' AS stable_sha, created_at_ms
          FROM news_learning_artifacts
         WHERE kind = 'active_agent'
        """
    )

    op.execute("GRANT SELECT ON news_learning_artifacts, news_learning_cases, news_model_recordings TO tracefold_serve")
    op.execute("GRANT SELECT ON news_review_pairwise_tasks_v1 TO tracefold_serve, tracefold_review")
    op.execute("GRANT SELECT ON news_review_active_agent_v1 TO tracefold_serve, tracefold_workers, tracefold_review")
    op.execute(
        "REVOKE SELECT ON news_learning_artifacts, news_learning_cases, news_model_recordings FROM tracefold_review"
    )
    op.execute(
        "GRANT SELECT, INSERT ON news_learning_artifacts, news_learning_cases, news_model_recordings "
        "TO tracefold_workers"
    )
    op.execute(
        "REVOKE UPDATE, DELETE ON news_learning_artifacts, news_learning_cases, news_model_recordings "
        "FROM tracefold_workers, tracefold_review"
    )


def downgrade() -> None:
    raise RuntimeError("20260821_0286 is an irreversible learning-artifact contract")
