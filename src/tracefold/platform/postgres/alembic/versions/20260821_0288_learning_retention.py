"""Bounded retention and release-chain pinning for News learning evidence.

Revision ID: 20260821_0288
Revises: 20260821_0287
"""

from __future__ import annotations

from alembic import op

revision = "20260821_0288"
down_revision = "20260821_0287"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE news_learning_retention_state (
          singleton                    boolean PRIMARY KEY DEFAULT true CHECK (singleton),
          last_run_at_ms               bigint,
          eligible_recordings          integer NOT NULL DEFAULT 0,
          eligible_cases               integer NOT NULL DEFAULT 0,
          eligible_artifacts           integer NOT NULL DEFAULT 0,
          deleted_recordings           integer NOT NULL DEFAULT 0,
          deleted_cases                integer NOT NULL DEFAULT 0,
          deleted_artifacts            integer NOT NULL DEFAULT 0,
          oldest_recording_age_ms      bigint,
          oldest_case_age_ms           bigint,
          oldest_artifact_age_ms       bigint,
          last_error_code              text,
          updated_at_ms                bigint NOT NULL
        )
        """
    )
    op.execute(
        "INSERT INTO news_learning_retention_state(singleton, updated_at_ms) "
        "VALUES (true, floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint)"
    )
    op.execute("CREATE INDEX ix_news_learning_cases_created ON news_learning_cases (created_at_ms, run_sha, case_id)")
    op.execute(
        "CREATE INDEX ix_news_learning_artifacts_created ON news_learning_artifacts (created_at_ms, artifact_sha)"
    )

    # Retention is the only authorised deletion path.  Workers still have no
    # DELETE privilege; the SECURITY DEFINER function is owned by the migration
    # role and exposes only a bounded batch argument.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_news_learning_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF current_setting('tracefold.learning_retention_purge', true) = 'on' THEN
            RETURN OLD;
          END IF;
          RAISE EXCEPTION 'news_learning_append_only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION purge_news_learning_retention(p_batch integer DEFAULT 500) RETURNS jsonb
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
          v_now_ms bigint := floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint;
          v_unreferenced_cutoff bigint;
          v_referenced_cutoff bigint;
          v_recordings integer := 0;
          v_cases integer := 0;
          v_artifacts integer := 0;
          v_eligible_recordings integer := 0;
          v_eligible_cases integer := 0;
          v_eligible_artifacts integer := 0;
          v_oldest_recording bigint;
          v_oldest_case bigint;
          v_oldest_artifact bigint;
        BEGIN
          IF p_batch < 1 OR p_batch > 1000 THEN
            RAISE EXCEPTION 'news_learning_retention_batch_invalid';
          END IF;
          v_unreferenced_cutoff := v_now_ms - 90::bigint * 86400000;
          v_referenced_cutoff := v_now_ms - 365::bigint * 86400000;
          PERFORM set_config('tracefold.learning_retention_purge', 'on', true);

          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha
              FROM news_canary_activations a
             WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (
                 SELECT stable_bundle_sha FROM pinned_runtime
               )
          ), pinned_runs AS (
            SELECT DISTINCT r.payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts r
              JOIN pinned_candidates p ON p.candidate_sha = r.payload ->> 'candidate_sha'
             WHERE r.kind = 'release_evidence' AND r.payload ? 'run_sha'
          ), referenced_runs AS (
            SELECT DISTINCT payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts
             WHERE kind IN ('evaluation_report', 'release_evidence') AND payload ? 'run_sha'
          ), doomed AS (
            SELECT r.recording_sha
              FROM news_model_recordings r
             WHERE NOT EXISTS (SELECT 1 FROM pinned_runs p WHERE p.run_sha = r.run_sha)
               AND (
                 (r.created_at_ms < v_unreferenced_cutoff
                  AND NOT EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = r.run_sha))
                 OR
                 (r.created_at_ms < v_referenced_cutoff
                  AND EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = r.run_sha))
               )
             ORDER BY r.created_at_ms, r.recording_sha
             LIMIT p_batch
          )
          DELETE FROM news_model_recordings r USING doomed d
           WHERE r.recording_sha = d.recording_sha;
          GET DIAGNOSTICS v_recordings = ROW_COUNT;

          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha
              FROM news_canary_activations a
             WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (
                 SELECT stable_bundle_sha FROM pinned_runtime
               )
          ), pinned_runs AS (
            SELECT DISTINCT r.payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts r
              JOIN pinned_candidates p ON p.candidate_sha = r.payload ->> 'candidate_sha'
             WHERE r.kind = 'release_evidence' AND r.payload ? 'run_sha'
          ), referenced_runs AS (
            SELECT DISTINCT payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts
             WHERE kind IN ('evaluation_report', 'release_evidence') AND payload ? 'run_sha'
          ), doomed AS (
            SELECT c.run_sha, c.case_id
              FROM news_learning_cases c
             WHERE NOT EXISTS (SELECT 1 FROM pinned_runs p WHERE p.run_sha = c.run_sha)
               AND (
                 (c.created_at_ms < v_unreferenced_cutoff
                  AND NOT EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = c.run_sha))
                 OR
                 (c.created_at_ms < v_referenced_cutoff
                  AND EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = c.run_sha))
               )
             ORDER BY c.created_at_ms, c.run_sha, c.case_id
             LIMIT p_batch
          )
          DELETE FROM news_learning_cases c USING doomed d
           WHERE c.run_sha = d.run_sha AND c.case_id = d.case_id;
          GET DIAGNOSTICS v_cases = ROW_COUNT;

          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha
              FROM news_canary_activations a
             WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (
                 SELECT stable_bundle_sha FROM pinned_runtime
               )
          ), pinned_release AS (
            SELECT artifact_sha, payload
              FROM news_learning_artifacts r
             WHERE r.kind = 'release_evidence'
               AND r.payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
          ), protected AS (
            SELECT artifact_sha
              FROM news_learning_artifacts
             WHERE kind IN ('active_agent', 'deployment_receipt', 'rollback_receipt')
            UNION SELECT artifact_sha FROM pinned_release
            UNION SELECT payload ->> 'report_sha' FROM pinned_release
            UNION SELECT payload #>> '{evidence,development_dataset_sha}'
              FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION SELECT payload #>> '{evidence,validation_dataset_sha}'
              FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION SELECT payload #>> '{evidence,observation_manifest_sha}'
              FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION
            SELECT artifact_sha FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION
            SELECT payload ->> 'proposal_sha' FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION
            SELECT payload #>> '{manifest,development_dataset_sha}' FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION
            SELECT payload #>> '{manifest,proposal_receipt,registration_receipt_sha}'
              FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION
            SELECT artifact_sha FROM news_learning_artifacts
             WHERE kind = 'dataset'
               AND payload ->> 'observation_ref' IN (SELECT candidate_sha FROM pinned_candidates)
          ), semantic_references AS (
            SELECT payload ->> 'proposal_sha' AS artifact_sha
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{manifest,development_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{manifest,proposal_receipt,registration_receipt_sha}'
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{evidence,development_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload #>> '{evidence,validation_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload #>> '{evidence,observation_manifest_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload ->> 'report_sha'
              FROM news_learning_artifacts WHERE kind = 'release_evidence'
            UNION SELECT DISTINCT dataset_sha FROM news_learning_cases
          ), doomed AS (
            SELECT a.artifact_sha
              FROM news_learning_artifacts a
             WHERE a.created_at_ms < v_referenced_cutoff
               AND a.kind NOT IN ('active_agent', 'deployment_receipt', 'rollback_receipt')
               AND NOT EXISTS (SELECT 1 FROM protected p WHERE p.artifact_sha = a.artifact_sha)
               AND NOT EXISTS (SELECT 1 FROM semantic_references r WHERE r.artifact_sha = a.artifact_sha)
               AND NOT (
                 a.kind = 'candidate' AND EXISTS (
                   SELECT 1 FROM news_learning_artifacts ref
                    WHERE ref.kind IN (
                      'evaluation_report', 'release_evidence', 'shadow_observation', 'canary_observation'
                    )
                      AND ref.payload ->> 'candidate_sha' = a.payload ->> 'candidate_sha'
                 )
               )
               AND NOT EXISTS (
                 SELECT 1 FROM news_learning_artifacts child WHERE child.parent_sha = a.artifact_sha
               )
             ORDER BY a.created_at_ms, a.artifact_sha
             LIMIT p_batch
          )
          DELETE FROM news_learning_artifacts a USING doomed d
           WHERE a.artifact_sha = d.artifact_sha;
          GET DIAGNOSTICS v_artifacts = ROW_COUNT;

          -- Remaining eligible counts are deliberately capped at batch + 1.
          -- Zero means drained; batch + 1 means "more work remains" without an
          -- unbounded count over a cold operational table.
          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha FROM news_canary_activations a WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (SELECT stable_bundle_sha FROM pinned_runtime)
          ), pinned_runs AS (
            SELECT DISTINCT r.payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts r
              JOIN pinned_candidates p ON p.candidate_sha = r.payload ->> 'candidate_sha'
             WHERE r.kind = 'release_evidence' AND r.payload ? 'run_sha'
          ), referenced_runs AS (
            SELECT DISTINCT payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts
             WHERE kind IN ('evaluation_report', 'release_evidence') AND payload ? 'run_sha'
          )
          SELECT count(*) INTO v_eligible_recordings
            FROM (
              SELECT 1 FROM news_model_recordings r
               WHERE NOT EXISTS (SELECT 1 FROM pinned_runs p WHERE p.run_sha = r.run_sha)
                 AND (
                   (r.created_at_ms < v_unreferenced_cutoff
                    AND NOT EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = r.run_sha))
                   OR
                   (r.created_at_ms < v_referenced_cutoff
                    AND EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = r.run_sha))
                 )
               LIMIT p_batch + 1
            ) remaining;

          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha FROM news_canary_activations a WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (SELECT stable_bundle_sha FROM pinned_runtime)
          ), pinned_runs AS (
            SELECT DISTINCT r.payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts r
              JOIN pinned_candidates p ON p.candidate_sha = r.payload ->> 'candidate_sha'
             WHERE r.kind = 'release_evidence' AND r.payload ? 'run_sha'
          ), referenced_runs AS (
            SELECT DISTINCT payload ->> 'run_sha' AS run_sha
              FROM news_learning_artifacts
             WHERE kind IN ('evaluation_report', 'release_evidence') AND payload ? 'run_sha'
          )
          SELECT count(*) INTO v_eligible_cases
            FROM (
              SELECT 1 FROM news_learning_cases c
               WHERE NOT EXISTS (SELECT 1 FROM pinned_runs p WHERE p.run_sha = c.run_sha)
                 AND (
                   (c.created_at_ms < v_unreferenced_cutoff
                    AND NOT EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = c.run_sha))
                   OR
                   (c.created_at_ms < v_referenced_cutoff
                    AND EXISTS (SELECT 1 FROM referenced_runs x WHERE x.run_sha = c.run_sha))
                 )
               LIMIT p_batch + 1
            ) remaining;

          WITH pinned_runtime AS (
            SELECT stable_bundle_sha, candidate_shas
              FROM (
                SELECT DISTINCT ON (stable_bundle_sha)
                       stable_bundle_sha, candidate_shas, registered_at_ms
                  FROM news_agent_runtime_manifests
                 ORDER BY stable_bundle_sha, registered_at_ms DESC
              ) distinct_stable
             ORDER BY registered_at_ms DESC
             LIMIT 2
          ), pinned_candidates AS (
            SELECT jsonb_array_elements_text(candidate_shas) AS candidate_sha FROM pinned_runtime
            UNION
            SELECT a.candidate_manifest_sha FROM news_canary_activations a WHERE a.state IN ('armed', 'active')
            UNION
            SELECT c.payload ->> 'candidate_sha'
              FROM news_learning_artifacts c
             WHERE c.kind = 'candidate'
               AND c.payload ->> 'candidate_bundle_sha' IN (SELECT stable_bundle_sha FROM pinned_runtime)
          ), pinned_release AS (
            SELECT artifact_sha, payload FROM news_learning_artifacts r
             WHERE r.kind = 'release_evidence'
               AND r.payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
          ), protected AS (
            SELECT artifact_sha FROM news_learning_artifacts
             WHERE kind IN ('active_agent', 'deployment_receipt', 'rollback_receipt')
            UNION SELECT artifact_sha FROM pinned_release
            UNION SELECT payload ->> 'report_sha' FROM pinned_release
            UNION SELECT payload #>> '{evidence,development_dataset_sha}' FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION SELECT payload #>> '{evidence,validation_dataset_sha}' FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION SELECT payload #>> '{evidence,observation_manifest_sha}' FROM news_learning_artifacts
             WHERE artifact_sha IN (SELECT payload ->> 'report_sha' FROM pinned_release)
            UNION SELECT artifact_sha FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION SELECT payload ->> 'proposal_sha' FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION SELECT payload #>> '{manifest,development_dataset_sha}' FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION SELECT payload #>> '{manifest,proposal_receipt,registration_receipt_sha}'
              FROM news_learning_artifacts
             WHERE kind = 'candidate'
               AND payload ->> 'candidate_sha' IN (SELECT candidate_sha FROM pinned_candidates)
            UNION SELECT artifact_sha FROM news_learning_artifacts
             WHERE kind = 'dataset'
               AND payload ->> 'observation_ref' IN (SELECT candidate_sha FROM pinned_candidates)
          ), semantic_references AS (
            SELECT payload ->> 'proposal_sha' AS artifact_sha
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{manifest,development_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{manifest,proposal_receipt,registration_receipt_sha}'
              FROM news_learning_artifacts WHERE kind = 'candidate'
            UNION SELECT payload #>> '{evidence,development_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload #>> '{evidence,validation_dataset_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload #>> '{evidence,observation_manifest_sha}'
              FROM news_learning_artifacts WHERE kind = 'evaluation_report'
            UNION SELECT payload ->> 'report_sha'
              FROM news_learning_artifacts WHERE kind = 'release_evidence'
            UNION SELECT DISTINCT dataset_sha FROM news_learning_cases
          )
          SELECT count(*) INTO v_eligible_artifacts
            FROM (
              SELECT 1 FROM news_learning_artifacts a
               WHERE a.created_at_ms < v_referenced_cutoff
                 AND a.kind NOT IN ('active_agent', 'deployment_receipt', 'rollback_receipt')
                 AND NOT EXISTS (SELECT 1 FROM protected p WHERE p.artifact_sha = a.artifact_sha)
                 AND NOT EXISTS (SELECT 1 FROM semantic_references r WHERE r.artifact_sha = a.artifact_sha)
                 AND NOT (
                   a.kind = 'candidate' AND EXISTS (
                     SELECT 1 FROM news_learning_artifacts ref
                      WHERE ref.kind IN (
                        'evaluation_report', 'release_evidence', 'shadow_observation', 'canary_observation'
                      )
                        AND ref.payload ->> 'candidate_sha' = a.payload ->> 'candidate_sha'
                   )
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM news_learning_artifacts child WHERE child.parent_sha = a.artifact_sha
                 )
               LIMIT p_batch + 1
            ) remaining;

          SELECT v_now_ms - created_at_ms INTO v_oldest_recording
            FROM news_model_recordings ORDER BY created_at_ms ASC LIMIT 1;
          SELECT v_now_ms - created_at_ms INTO v_oldest_case
            FROM news_learning_cases ORDER BY created_at_ms ASC LIMIT 1;
          SELECT v_now_ms - created_at_ms INTO v_oldest_artifact
            FROM news_learning_artifacts ORDER BY created_at_ms ASC LIMIT 1;
          UPDATE news_learning_retention_state
             SET last_run_at_ms = v_now_ms,
                 eligible_recordings = v_eligible_recordings,
                 eligible_cases = v_eligible_cases,
                 eligible_artifacts = v_eligible_artifacts,
                 deleted_recordings = v_recordings,
                 deleted_cases = v_cases,
                 deleted_artifacts = v_artifacts,
                 oldest_recording_age_ms = v_oldest_recording,
                 oldest_case_age_ms = v_oldest_case,
                 oldest_artifact_age_ms = v_oldest_artifact,
                 last_error_code = NULL,
                 updated_at_ms = v_now_ms
           WHERE singleton;
          RETURN jsonb_build_object(
            'measured_at_ms', v_now_ms,
            'eligible_recordings', v_eligible_recordings,
            'eligible_cases', v_eligible_cases,
            'eligible_artifacts', v_eligible_artifacts,
            'deleted_recordings', v_recordings,
            'deleted_cases', v_cases,
            'deleted_artifacts', v_artifacts,
            'oldest_recording_age_ms', v_oldest_recording,
            'oldest_case_age_ms', v_oldest_case,
            'oldest_artifact_age_ms', v_oldest_artifact
          );
        END;
        $$
        """
    )

    op.execute("REVOKE ALL ON FUNCTION purge_news_learning_retention(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION purge_news_learning_retention(integer) TO tracefold_workers")
    op.execute("GRANT SELECT ON news_learning_retention_state TO tracefold_serve, tracefold_workers")
    op.execute("GRANT UPDATE ON news_learning_retention_state TO tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("20260821_0288 is an irreversible learning-retention contract")
