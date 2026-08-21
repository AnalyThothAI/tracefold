"""Start the corrected Program evidence epoch without rewriting program_v1.

The first deployed DSPy baseline remains immutable audit history. The corrected
semantic retry state machine and hardened ``restates`` contract have a new
content-addressed baseline, so evidence eligibility restarts at ``program_v2``.

Revision ID: 20260822_0293
Revises: 20260822_0292
"""

from __future__ import annotations

from alembic import op

revision = "20260822_0293"
down_revision = "20260822_0292"
branch_labels = None
depends_on = None

_PROGRAM_V1_SHA = "87c62ed2b3a89eadddbdc90bdab03405c11da30ee259da49e11b0bd973094119"
_PROGRAM_V2_SHA = "ad8720a8f70c9210440f7c9eaeaad182b261359dcbe77a0fb7c6d2063815da3c"


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
           WHERE epoch_id = 'program_v1';
          IF prior_sha <> '{_PROGRAM_V1_SHA}' THEN
            RAISE EXCEPTION 'news_learning_program_v1_baseline_mismatch';
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
            'program_v2', deployed_at_ms,
            'https://github.com/AnalyThothAI/tracefold/issues/129',
            'tracefold.news.semantic_program.factory_v1',
            'news_semantic_program_artifact_v1',
            'news_semantic_program_v1',
            '{_PROGRAM_V2_SHA}',
            'audit_only',
            'semantic_retry_and_restatement_contract_reidentity',
            deployed_at_ms
          );
        END
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260822_0293 is an irreversible append-only Program evidence epoch")
