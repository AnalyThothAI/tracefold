"""Start the D-generation Program evidence epoch without rewriting history.

The factory-v2 Program and trusted optimizer ownership boundary are a hard
cut.  Program v1-v3 evidence remains immutable audit history, every open
canary is terminalized, and release evidence reaccrues under ``program_v4``.

Revision ID: 20260822_0295
Revises: 20260822_0294
"""

from __future__ import annotations

from alembic import op

revision = "20260822_0295"
down_revision = "20260822_0294"
branch_labels = None
depends_on = None

_PROGRAM_V3_SHA = "49643db931211aee7f1d4f5b7124345d45e18132b10628b85843c55e05dff8d5"
_PROGRAM_V4_SHA = "5bc03f976b10f22ce788f93a8d91202e2b0e9899172cb91e33beeca89b93cb48"


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
           WHERE epoch_id = 'program_v3';
          IF prior_sha <> '{_PROGRAM_V3_SHA}' THEN
            RAISE EXCEPTION 'news_learning_program_v3_baseline_mismatch';
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
            'program_v4', deployed_at_ms,
            'https://github.com/AnalyThothAI/tracefold/issues/134',
            'tracefold.news.semantic_program.factory_v2',
            'news_semantic_program_artifact_v2',
            'news_semantic_program_v2',
            '{_PROGRAM_V4_SHA}',
            'audit_only',
            'd_generation_factory_and_optimizer_ownership_hard_cut',
            deployed_at_ms
          );

          UPDATE news_canary_activations
             SET state = 'tripped',
                 revision = revision + 1,
                 trip_reason = 'program_v4_hard_cut',
                 tripped_at_ms = deployed_at_ms
           WHERE state IN ('armed', 'active');
        END
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260822_0295 is an irreversible append-only Program evidence epoch")
