"""Hard-cut News generation to the content-addressed DSPy Program epoch.

Prompt-era learning rows remain immutable audit history, but the code-owned
``program_v1`` epoch starts when this hard-cut migration is deployed and is the
earliest evidence eligible for new datasets. Verdicts name the Program that
generated them and recordings become Predictor/call/attempt facts rather than
one opaque row per final verdict.

Revision ID: 20260822_0292
Revises: 20260821_0291
"""

from __future__ import annotations

from alembic import op

revision = "20260822_0292"
down_revision = "20260821_0291"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE news_verdicts ADD COLUMN program_version text")
    op.execute("ALTER TABLE news_verdicts ADD COLUMN program_sha256 text")
    op.execute(
        "ALTER TABLE news_verdicts ADD CONSTRAINT news_verdicts_program_pair_check "
        "CHECK ((program_version IS NULL) = (program_sha256 IS NULL))"
    )
    op.execute(
        "ALTER TABLE news_verdicts ADD CONSTRAINT news_verdicts_program_version_check "
        "CHECK (program_version IS NULL OR btrim(program_version) <> '')"
    )
    op.execute(
        "ALTER TABLE news_verdicts ADD CONSTRAINT news_verdicts_program_sha_check "
        "CHECK (program_sha256 IS NULL OR program_sha256 ~ '^[0-9a-f]{64}$')"
    )

    # Preserve the old rows without pretending they came from a Predictor.
    # New code always supplies the real call path.
    # The table is append-only, so legacy identity is installed as a temporary
    # DDL default rather than mutating historical rows through UPDATE.
    op.execute("ALTER TABLE news_model_recordings ADD COLUMN predictor_name text NOT NULL DEFAULT 'legacy_prompt'")
    op.execute("ALTER TABLE news_model_recordings ADD COLUMN call_index integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE news_model_recordings ADD COLUMN attempt integer NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE news_model_recordings ADD COLUMN route text NOT NULL DEFAULT 'legacy'")
    op.execute("ALTER TABLE news_model_recordings ADD COLUMN cached_tokens integer")
    op.execute("ALTER TABLE news_model_recordings ADD COLUMN total_tokens integer")
    op.execute("ALTER TABLE news_model_recordings ADD COLUMN provider_cost_microusd bigint")
    for column in ("predictor_name", "call_index", "attempt", "route"):
        op.execute(f"ALTER TABLE news_model_recordings ALTER COLUMN {column} DROP DEFAULT")
    op.execute("ALTER TABLE news_model_recordings DROP CONSTRAINT IF EXISTS news_model_recording_call_index")
    op.execute(
        "ALTER TABLE news_model_recordings ADD CONSTRAINT news_model_recording_call_index CHECK (call_index >= 0)"
    )
    op.execute(
        "ALTER TABLE news_model_recordings ADD CONSTRAINT news_model_recording_attempt "
        "CHECK (attempt >= 1 AND attempt <= 2)"
    )
    op.execute(
        "ALTER TABLE news_model_recordings ADD CONSTRAINT news_model_recording_route "
        "CHECK (route IN ('primary', 'fallback', 'legacy'))"
    )
    op.execute(
        "ALTER TABLE news_model_recordings ADD CONSTRAINT news_model_recording_token_counts "
        "CHECK ((cached_tokens IS NULL OR cached_tokens >= 0) "
        "AND (total_tokens IS NULL OR total_tokens >= 0))"
    )
    op.execute(
        "ALTER TABLE news_model_recordings ADD CONSTRAINT news_model_recording_provider_cost "
        "CHECK (provider_cost_microusd IS NULL OR provider_cost_microusd >= 0)"
    )
    op.execute("DROP INDEX ux_news_model_recording_trial")
    op.execute(
        "CREATE UNIQUE INDEX ux_news_model_recording_call "
        "ON news_model_recordings "
        "(run_sha, case_id, arm, trial, predictor_name, call_index, attempt)"
    )

    op.execute("ALTER TABLE news_learning_artifacts DROP CONSTRAINT news_learning_artifact_kind")
    op.execute(
        """
        ALTER TABLE news_learning_artifacts
        ADD CONSTRAINT news_learning_artifact_kind CHECK (kind IN (
          'candidate_registration', 'proposal', 'candidate', 'dataset', 'evaluation_report', 'release_evidence',
          'active_agent', 'shadow_observation', 'canary_observation', 'deployment_receipt', 'rollback_receipt',
          'program_artifact', 'compile_receipt', 'epoch_reset'
        ))
        """
    )
    op.execute(
        """
        CREATE TABLE news_learning_epochs (
          epoch_id                   text   PRIMARY KEY,
          starts_at_ms               bigint NOT NULL,
          source_issue               text   NOT NULL,
          program_factory_id         text   NOT NULL,
          artifact_schema_version    text   NOT NULL,
          baseline_program_version   text   NOT NULL,
          baseline_program_sha256    text   NOT NULL,
          prior_evidence_disposition text   NOT NULL,
          reset_reason               text   NOT NULL,
          created_at_ms              bigint NOT NULL,
          CONSTRAINT news_learning_epoch_id CHECK (epoch_id ~ '^[a-z0-9_]+$'),
          CONSTRAINT news_learning_epoch_baseline_sha
            CHECK (baseline_program_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_learning_epoch_disposition CHECK (prior_evidence_disposition = 'audit_only')
        )
        """
    )
    op.execute(
        """
        WITH deployed AS (
          SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS at_ms
        )
        INSERT INTO news_learning_epochs (
          epoch_id, starts_at_ms, source_issue, program_factory_id, artifact_schema_version,
          baseline_program_version, baseline_program_sha256, prior_evidence_disposition,
          reset_reason, created_at_ms
        )
        SELECT 'program_v1', at_ms,
               'https://github.com/AnalyThothAI/tracefold/issues/129',
               'tracefold.news.semantic_program.factory_v1',
               'news_semantic_program_artifact_v1', 'news_semantic_program_v1',
               '87c62ed2b3a89eadddbdc90bdab03405c11da30ee259da49e11b0bd973094119',
               'audit_only', 'zero_compatibility_reset_reaccrue_evidence', at_ms
          FROM deployed
        """
    )
    op.execute(
        "CREATE TRIGGER trg_news_learning_epochs_append_only "
        "BEFORE UPDATE OR DELETE ON news_learning_epochs "
        "FOR EACH ROW EXECUTE FUNCTION reject_news_learning_mutation()"
    )

    # CREATE OR REPLACE may only append view columns. Keep the original order
    # byte-for-byte and add Program identity at the end.
    op.execute(
        """
        CREATE OR REPLACE VIEW news_review_task_source_v1 WITH (security_barrier = true) AS
        SELECT e.event_id,
               s.evidence_version,
               s.evidence_sha256,
               s.release_eligible AS evidence_release_eligible,
               s.snapshot AS evidence_snapshot,
               e.opened_at_ms,
               e.admission,
               e.priority,
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
               v.program_sha256
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
        """
    )

    op.execute("GRANT SELECT ON news_learning_epochs TO tracefold_serve, tracefold_workers")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON news_learning_epochs FROM tracefold_serve, tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("20260822_0292 is an irreversible DSPy Program epoch hard cut")
