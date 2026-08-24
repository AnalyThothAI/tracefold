"""Start the Program/Learning split evidence epoch.

Issue #162 PR8-B moved the Program out of ``news/agents`` into its own package and split the learning
plane away from it.  The factory source closure is content-addressed by *logical file name*, so the move
re-issues the Program root even though no prompt, RulePack, policy, model route or call budget changed.

The behavior is deliberately identical; the identity is not, and identity is what release evidence is
keyed on.  Evidence accrued under ``program_v6`` therefore becomes immutable audit history and re-accrues
under ``program_v7``.  That cost is nil in practice: the v6 epoch closed with zero accepted
``news_review_v4`` reviews, zero canary activations and zero Trading cases, which is exactly why this
migration was scheduled now rather than after a corpus had been built and then invalidated.

Revision ID: 20260824_0303
Revises: 20260824_0302
"""

from __future__ import annotations

from alembic import op

revision = "20260824_0303"
down_revision = "20260824_0302"
branch_labels = None
depends_on = None

# What migration 0301 recorded when it opened `program_v6` — not the runtime root at the time of this
# migration. #173/#174 re-issued the Program root to `9334eae4…` *inside* the v6 epoch without opening a
# new one, so the epoch row still names the sha the epoch was opened with. Asserting the runtime value
# here raised `news_learning_program_v6_baseline_mismatch` on a freshly migrated database, which is the
# guard working: the two are genuinely different facts.
_PROGRAM_V6_EPOCH_BASELINE_SHA = "648e696df5a8f251085a0749795a8d9e9227d05fb7e976fd1b5b538a7b8e87e7"
_PROGRAM_V7_SHA = "7a460f8d3812c64c6ee38158871eb9f060811e5ffe87f399f7bc2e506b4e28ad"


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
           WHERE epoch_id = 'program_v6';
          IF prior_sha <> '{_PROGRAM_V6_EPOCH_BASELINE_SHA}' THEN
            RAISE EXCEPTION 'news_learning_program_v6_baseline_mismatch';
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
            'program_v7', deployed_at_ms,
            'https://github.com/AnalyThothAI/tracefold/issues/162',
            'tracefold.news.program.factory_v5',
            'news_semantic_program_artifact_v2',
            'news_semantic_program_v5',
            '{_PROGRAM_V7_SHA}',
            'audit_only',
            'program_learning_package_split_identity_migration',
            deployed_at_ms
          );

          UPDATE news_canary_activations
             SET state = 'tripped',
                 revision = revision + 1,
                 trip_reason = 'program_v7_hard_cut',
                 tripped_at_ms = deployed_at_ms
           WHERE state IN ('armed', 'active');
        END
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260824_0303 is an irreversible append-only Program evidence epoch")
