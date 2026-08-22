"""Start the candidate-conditioned ToldContext Program evidence epoch.

The factory-v3 Program changes what each Predictor is allowed to read: the
told context is selected against the candidate instead of by recency, and
``ReaderCard`` no longer receives it at all.  A verdict produced under the old
input contract cannot be compared with one produced under the new one, so
program v1-v4 evidence remains immutable audit history and release evidence
reaccrues under ``program_v5``.

Revision ID: 20260822_0298
Revises: 20260822_0297
"""

from __future__ import annotations

from alembic import op

revision = "20260822_0298"
down_revision = "20260822_0297"
branch_labels = None
depends_on = None

_PROGRAM_V4_SHA = "5bc03f976b10f22ce788f93a8d91202e2b0e9899172cb91e33beeca89b93cb48"
_PROGRAM_V5_SHA = "c62e0d69bf6c1901b3e8a1a716ca153acaf92793421d5af2701030c0477cac3b"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        DECLARE
          prior_start_ms bigint;
          deployed_at_ms bigint;
          prior_sha text;
        BEGIN
          SELECT starts_at_ms, baseline_program_sha256
            INTO STRICT prior_start_ms, prior_sha
            FROM news_learning_epochs
           WHERE epoch_id = 'program_v4';
          IF prior_sha <> '{_PROGRAM_V4_SHA}' THEN
            RAISE EXCEPTION 'news_learning_program_v4_baseline_mismatch';
          END IF;

          deployed_at_ms := greatest(
            floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint,
            prior_start_ms + 1
          );
          INSERT INTO news_learning_epochs (
            epoch_id, starts_at_ms, source_issue, program_factory_id, artifact_schema_version,
            baseline_program_version, baseline_program_sha256, prior_evidence_disposition,
            reset_reason, created_at_ms
          ) VALUES (
            'program_v5', deployed_at_ms,
            'https://github.com/AnalyThothAI/tracefold/issues/138',
            'tracefold.news.semantic_program.factory_v3',
            'news_semantic_program_artifact_v2',
            'news_semantic_program_v3',
            '{_PROGRAM_V5_SHA}',
            'audit_only',
            'candidate_conditioned_told_context_and_predictor_input_partition',
            deployed_at_ms
          );

          UPDATE news_canary_activations
             SET state = 'tripped',
                 revision = revision + 1,
                 trip_reason = 'program_v5_hard_cut',
                 tripped_at_ms = deployed_at_ms
           WHERE state IN ('armed', 'active');
        END
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260822_0298 is an irreversible append-only Program evidence epoch")
