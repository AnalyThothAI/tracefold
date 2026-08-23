"""Hard-cut News to typed trade relevance and Program evidence epoch v6 (#160).

``priority`` had two incompatible meanings: AMQP scheduling and reader-facing
importance.  The only surviving meaning is transport order, named
``queue_priority``.  The semantic Program now owns a separately typed editorial
envelope; verdict and editorial bytes are persisted atomically and addressed as
one ``ScoredJudgment``.  Earlier Program/rubric/policy rows remain immutable
audit history and release evidence reaccrues from this migration.

Revision ID: 20260823_0300
Revises: 20260823_0299
"""

from __future__ import annotations

from alembic import op

revision = "20260823_0300"
down_revision = "20260823_0299"
branch_labels = None
depends_on = None

_PROGRAM_V5_SHA = "c62e0d69bf6c1901b3e8a1a716ca153acaf92793421d5af2701030c0477cac3b"
_PROGRAM_V6_SHA = "648e696df5a8f251085a0749795a8d9e9227d05fb7e976fd1b5b538a7b8e87e7"


def upgrade() -> None:
    # A view keeps its output column name across a base-column rename, while CREATE OR REPLACE refuses to
    # rename an existing output column.  Drop and recreate this read-only surface in the same transaction so
    # there is no compatibility alias named `priority`.
    op.execute("DROP VIEW news_review_task_source_v1")
    op.execute("ALTER TABLE news_events RENAME COLUMN priority TO queue_priority")
    op.execute(
        "ALTER TABLE news_events RENAME CONSTRAINT news_events_priority_check TO news_events_queue_priority_check"
    )

    # Historical verdicts are audit-only and intentionally stay NULL.  v10 rows must carry the complete
    # triplet; the application validates the nested content hashes before insert and every learning read.
    op.execute("ALTER TABLE news_verdicts ADD COLUMN editorial jsonb")
    op.execute("ALTER TABLE news_verdicts ADD COLUMN scored_judgment_sha256 text")
    op.execute("ALTER TABLE news_verdicts ADD COLUMN runtime_manifest_sha text")
    op.execute(
        """
        ALTER TABLE news_verdicts
        ADD CONSTRAINT news_verdicts_scored_judgment_triplet_check CHECK (
          (editorial IS NULL AND scored_judgment_sha256 IS NULL AND runtime_manifest_sha IS NULL)
          OR
          (editorial IS NOT NULL
           AND scored_judgment_sha256 IS NOT NULL
           AND runtime_manifest_sha IS NOT NULL
           AND jsonb_typeof(editorial) = 'object'
           AND scored_judgment_sha256 ~ '^[0-9a-f]{64}$'
           AND runtime_manifest_sha ~ '^[0-9a-f]{64}$')
        )
        """
    )
    op.execute(
        """
        ALTER TABLE news_verdicts
        ADD CONSTRAINT news_verdicts_v10_scored_judgment_required CHECK (
          policy_version <> 'news_triage_policy_v10'
          OR (editorial IS NOT NULL AND scored_judgment_sha256 IS NOT NULL AND runtime_manifest_sha IS NOT NULL)
        )
        """
    )

    # Preserve the established output order and append the new identities.  Telemetry remains excluded: its
    # arithmetic judgment has an editorial envelope for audit symmetry but is not model-learning evidence.
    op.execute(
        """
        CREATE VIEW news_review_task_source_v1 WITH (security_barrier = true) AS
        SELECT e.event_id,
               s.evidence_version,
               s.evidence_sha256,
               s.release_eligible AS evidence_release_eligible,
               s.snapshot AS evidence_snapshot,
               e.opened_at_ms,
               e.admission,
               e.queue_priority,
               e.storyline_key,
               e.ingest_mode,
               v.created_at_ms AS verdict_created_at_ms,
               v.evidence_version AS verdict_evidence_version,
               v.final_decision,
               v.degraded,
               v.error_code AS verdict_error_code,
               v.override_rule,
               v.throttled_by,
               v.verdict,
               v.trace,
               v.prompt_version,
               v.policy_version,
               v.model,
               d.state AS delivery_state,
               d.card AS delivery_card,
               d.settled_at_ms,
               d.error_code AS delivery_error_code,
               reaction.max_abs_return_1h_bps,
               v.program_version,
               v.program_sha256,
               v.editorial,
               v.scored_judgment_sha256,
               v.runtime_manifest_sha
          FROM news_events e
          LEFT JOIN LATERAL (
            SELECT x.* FROM news_verdicts x
             WHERE x.event_id = e.event_id AND x.stage = 'triage'
             ORDER BY x.created_at_ms DESC LIMIT 1
          ) v ON true
          JOIN LATERAL (
            SELECT x.* FROM news_event_evidence_snapshots x
             WHERE x.event_id = e.event_id
             ORDER BY x.evidence_version DESC LIMIT 1
          ) s ON true
          LEFT JOIN news_deliveries d ON d.event_id = e.event_id AND d.kind = 'first'
          LEFT JOIN LATERAL (
            SELECT max(abs(x.return_1h_bps)) AS max_abs_return_1h_bps
              FROM news_event_reactions x
             WHERE x.event_id = e.event_id
               AND x.metric_version = 'reaction_v1'
               AND x.is_primary
          ) reaction ON true
         WHERE v.program_version IS DISTINCT FROM 'news_oi_signal_v1'
        """
    )
    op.execute("GRANT SELECT ON news_review_task_source_v1 TO tracefold_serve, tracefold_workers")

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
           WHERE epoch_id = 'program_v5';
          IF prior_sha <> '{_PROGRAM_V5_SHA}' THEN
            RAISE EXCEPTION 'news_learning_program_v5_baseline_mismatch';
          END IF;

          deployed_at_ms := greatest(
            floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint,
            prior_start_ms + 1
          );
          -- Durable broker messages from the old generation may still arrive after the binary flips.  The
          -- v6 consumer rejects their v1 evidence contract; terminalize the unpublished derived outbox state
          -- so Janitor cannot republish them forever.  Material Items/snapshots remain intact audit history.
          UPDATE news_events
             SET published_at_ms = deployed_at_ms,
                 updated_at_ms = deployed_at_ms
           WHERE opened_at_ms < deployed_at_ms
             AND published_at_ms IS NULL
             AND admission IN ('candidate', 'listing_deterministic', 'telemetry_deterministic');

          -- Pre-v6 Events remain immutable audit evidence but stop participating in exact, artifact and
          -- MinHash matches. Otherwise the first post-cut Item can be absorbed by an old Event whose v1
          -- evidence the v6 consumer correctly refuses, silently losing the new fact.
          UPDATE news_events
             SET expires_at_ms = least(expires_at_ms, deployed_at_ms),
                 updated_at_ms = greatest(updated_at_ms, deployed_at_ms)
           WHERE opened_at_ms < deployed_at_ms;
          UPDATE news_event_bands b
             SET expires_at_ms = least(b.expires_at_ms, deployed_at_ms)
            FROM news_events e
           WHERE e.event_id = b.event_id
             AND e.opened_at_ms < deployed_at_ms;

          INSERT INTO news_learning_epochs (
            epoch_id, starts_at_ms, source_issue, program_factory_id, artifact_schema_version,
            baseline_program_version, baseline_program_sha256, prior_evidence_disposition,
            reset_reason, created_at_ms
          ) VALUES (
            'program_v6', deployed_at_ms,
            'https://github.com/AnalyThothAI/tracefold/issues/160',
            'tracefold.news.semantic_program.factory_v4',
            'news_semantic_program_artifact_v2',
            'news_semantic_program_v4',
            '{_PROGRAM_V6_SHA}',
            'audit_only',
            'trade_relevance_editorial_authority_hard_cut',
            deployed_at_ms
          );

          UPDATE news_canary_activations
             SET state = 'tripped',
                 revision = revision + 1,
                 trip_reason = 'program_v6_hard_cut',
                 tripped_at_ms = deployed_at_ms
           WHERE state IN ('armed', 'active');
        END
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260823_0300 is an irreversible typed-editorial Program epoch hard cut")
