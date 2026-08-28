"""Start the endpoint-capable-envelope Program evidence epoch (#310).

#306 Phase 3's self-owned transport sent the `json_schema` response_format to every endpoint. DeepSeek
rejects that format outright (HTTP 400 "This response_format type is unavailable now"), which killed the
whole fallback route and the metric judge on the day the transport shipped: a primary output failure had
nowhere to go, and the first 1.7 hours of the v8 cohort degraded 35 of 118 verdicts. #310 makes the
structured-output constraint follow the endpoint — `json_schema` where supported, `json_object` with the
same schema inlined into the system message where not — decided per model at composition time, never as a
per-call fallback.

That moves prompt bytes on the fallback route, which is code-owned envelope behavior: `factory_v8` becomes
`factory_v9`, the artifact bytes move with the literal, and the stable root is re-issued over the unchanged
seed texts. Evidence accrued under `program_v8` becomes immutable audit history. The cost is the smallest
of any epoch so far: v8 lived under two hours of clean traffic, closed with zero accepted reviews, zero
frozen datasets and zero candidates, and a third of its verdicts carry the very degradation this migration
repairs.

Revision ID: 20260828_0319
Revises: 20260828_0318
"""

from __future__ import annotations

from alembic import op

revision = "20260828_0319"
down_revision = "20260828_0318"
branch_labels = None
depends_on = None

# What migration 0318 recorded when it opened `program_v8`: the epoch row names the sha the epoch was
# opened with, and no re-issue happened inside v8, so the runtime root and the epoch baseline agree here.
_PROGRAM_V8_EPOCH_BASELINE_SHA = "c9bd53421b8c5c41c183cda5ef69150f241d467fee7699a6c087e2f71b27f3e9"
_PROGRAM_V9_SHA = "23bb047c1ca2e2caef2b713154f7d0fe5eabe98bfdaddb4417aa7a889982b754"


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
           WHERE epoch_id = 'program_v8';
          IF prior_sha <> '{_PROGRAM_V8_EPOCH_BASELINE_SHA}' THEN
            RAISE EXCEPTION 'news_learning_program_v8_baseline_mismatch';
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
            'program_v9', deployed_at_ms,
            'https://github.com/AnalyThothAI/tracefold/issues/310',
            'tracefold.news.program.factory_v9',
            'news_program_strategy_artifact_v1',
            'news_semantic_program_v5',
            '{_PROGRAM_V9_SHA}',
            'audit_only',
            'endpoint_capable_structured_output_envelope_identity_migration',
            deployed_at_ms
          );

          UPDATE news_canary_activations
             SET state = 'tripped',
                 revision = revision + 1,
                 trip_reason = 'program_v9_hard_cut',
                 tripped_at_ms = deployed_at_ms
           WHERE state IN ('armed', 'active');
        END
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260828_0319 is an irreversible append-only Program evidence epoch")
