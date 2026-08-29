"""Start the single-instruction Program evidence epoch (#306).

Issue #306 collapses the prompt layering into one text per Predictor and then removes the DSPy transport
from under it. Both halves move Program bytes: the sealed QualityKernel, the nine ordered RulePack headers
and the authority seal are gone from the rendered instruction, and the self-owned chat transport composes
the field envelope itself instead of delegating to DSPy's JSON adapter.

The issue leaves the scheduling of those two byte changes open — one identity migration or two — and this
is the decision: **one**. They land together, so the re-baseline is paid once. `factory_v7` becomes
`factory_v8`, the code-owned stable root moves to the seed texts, and evidence accrued under `program_v7`
becomes immutable audit history.

The cost is again nil in practice, and for the third epoch running the same reason: the v7 epoch closed
with zero accepted candidates, zero canary activations and a stable artifact whose two advisory
instructions were both the empty string — that is, with a learning plane that had never contributed a byte
to a reader-visible prompt. That absence is exactly what #306 exists to repair.

Revision ID: 20260828_0318
Revises: 20260828_0317
"""

from __future__ import annotations

from alembic import op

revision = "20260828_0318"
down_revision = "20260828_0317"
branch_labels = None
depends_on = None

# What migration 0303 recorded when it opened `program_v7`, not the runtime root at the time of this
# migration: #193 re-issued the Program root inside the v7 epoch without opening a new one, so the epoch
# row still names the sha the epoch was opened with. Asserting the runtime value here would raise on a
# freshly migrated database, which is the guard working — the two are genuinely different facts.
_PROGRAM_V7_EPOCH_BASELINE_SHA = "7a460f8d3812c64c6ee38158871eb9f060811e5ffe87f399f7bc2e506b4e28ad"
_PROGRAM_V8_SHA = "c9bd53421b8c5c41c183cda5ef69150f241d467fee7699a6c087e2f71b27f3e9"


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
           WHERE epoch_id = 'program_v7';
          IF prior_sha <> '{_PROGRAM_V7_EPOCH_BASELINE_SHA}' THEN
            RAISE EXCEPTION 'news_learning_program_v7_baseline_mismatch';
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
            'program_v8', deployed_at_ms,
            'https://github.com/AnalyThothAI/tracefold/issues/306',
            'tracefold.news.program.factory_v8',
            'news_program_strategy_artifact_v1',
            'news_semantic_program_v5',
            '{_PROGRAM_V8_SHA}',
            'audit_only',
            'single_instruction_seed_and_self_owned_transport_identity_migration',
            deployed_at_ms
          );

          UPDATE news_canary_activations
             SET state = 'tripped',
                 revision = revision + 1,
                 trip_reason = 'program_v8_hard_cut',
                 tripped_at_ms = deployed_at_ms
           WHERE state IN ('armed', 'active');
        END
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260828_0318 is an irreversible append-only Program evidence epoch")
