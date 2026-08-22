"""Start the expert-quality Program evidence epoch without rewriting history.

The first two deployed DSPy baselines remain immutable audit history. The
expert semantic baseline and deterministic normalization contract have a new
content-addressed baseline, so evidence eligibility restarts at ``program_v3``.

Revision ID: 20260822_0294
Revises: 20260822_0293
"""

from __future__ import annotations

from alembic import op

revision = "20260822_0294"
down_revision = "20260822_0293"
branch_labels = None
depends_on = None

_PROGRAM_V2_SHA = "ad8720a8f70c9210440f7c9eaeaad182b261359dcbe77a0fb7c6d2063815da3c"
_PROGRAM_V3_SHA = "49643db931211aee7f1d4f5b7124345d45e18132b10628b85843c55e05dff8d5"


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
           WHERE epoch_id = 'program_v2';
          IF prior_sha <> '{_PROGRAM_V2_SHA}' THEN
            RAISE EXCEPTION 'news_learning_program_v2_baseline_mismatch';
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
            'program_v3', deployed_at_ms,
            'https://github.com/AnalyThothAI/tracefold/issues/132',
            'tracefold.news.semantic_program.factory_v1',
            'news_semantic_program_artifact_v1',
            'news_semantic_program_v1',
            '{_PROGRAM_V3_SHA}',
            'audit_only',
            'expert_quality_baseline_and_semantic_normalization',
            deployed_at_ms
          );
        END
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260822_0294 is an irreversible append-only Program evidence epoch")
