"""Hard-cut News identity products and introduce Brief Activation state."""

from __future__ import annotations

from alembic import op

revision = "20260727_0199"
down_revision = "20260726_0198"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '30min'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration_backup_receipts (
          migration_revision text PRIMARY KEY CHECK (btrim(migration_revision) <> ''),
          backup_sha256 text NOT NULL
            CHECK (backup_sha256 ~ '^[0-9a-f]{64}$'),
          backup_location text NOT NULL CHECK (btrim(backup_location) <> ''),
          backup_created_at_ms bigint NOT NULL CHECK (backup_created_at_ms >= 0),
          recorded_at_ms bigint NOT NULL CHECK (recorded_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM news_feed_observations LIMIT 1)
             OR EXISTS (SELECT 1 FROM news_articles LIMIT 1)
             OR EXISTS (SELECT 1 FROM news_stories LIMIT 1)
          THEN
            IF NOT EXISTS (
              SELECT 1
                FROM schema_migration_backup_receipts
               WHERE migration_revision = '20260727_0199'
                 AND backup_sha256 ~ '^[0-9a-f]{64}$'
                 AND btrim(backup_location) <> ''
                 AND backup_created_at_ms >= 0
                 AND recorded_at_ms >= backup_created_at_ms
            )
            THEN
              RAISE EXCEPTION USING MESSAGE =
                'news_0199_backup_receipt_required'
                || ': verify a recoverable backup before the derived-state hard cut';
            END IF;
          END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DROP TABLE
          news_brief_current,
          news_brief_publications,
          news_ai_attempts,
          news_brief_selection_snapshots,
          news_narrative_grouping_snapshots
        """
    )
    op.execute(
        """
        TRUNCATE TABLE
          news_story_analysis_current,
          news_story_analysis_publications,
          news_story_analysis_requests,
          news_story_material_events,
          news_story_identity_decisions,
          news_story_profiles,
          news_story_memberships,
          news_stories,
          news_article_identity_features,
          news_story_projection_checkpoints
        """
    )
    op.execute(
        """
        CREATE TABLE news_narrative_grouping_snapshots (
          grouping_snapshot_id text PRIMARY KEY,
          input_hash text NOT NULL CHECK (btrim(input_hash) <> ''),
          policy_version text NOT NULL CHECK (btrim(policy_version) <> ''),
          embedding_model text,
          fallback_used boolean NOT NULL DEFAULT false,
          groups jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(groups) = 'array'),
          receipt jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(receipt) = 'object'),
          cutoff_at_ms bigint NOT NULL CHECK (cutoff_at_ms >= 0),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          UNIQUE (input_hash, policy_version, embedding_model)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_brief_selections (
          selection_id text PRIMARY KEY,
          selection_fingerprint text NOT NULL UNIQUE
            CHECK (btrim(selection_fingerprint) <> ''),
          grouping_snapshot_id text NOT NULL
            REFERENCES news_narrative_grouping_snapshots(grouping_snapshot_id)
            ON DELETE RESTRICT,
          policy_version text NOT NULL CHECK (btrim(policy_version) <> ''),
          evidence_cutoff_at_ms bigint NOT NULL CHECK (evidence_cutoff_at_ms >= 0),
          selected_story_ids jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(selected_story_ids) = 'array'),
          decisions jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(decisions) = 'array'),
          critical boolean NOT NULL DEFAULT false,
          verified_critical boolean NOT NULL DEFAULT false,
          synthesis_input_hash text NOT NULL CHECK (btrim(synthesis_input_hash) <> ''),
          evidence_bundle jsonb NOT NULL CHECK (jsonb_typeof(evidence_bundle) = 'object'),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_brief_proposals (
          proposal_id text PRIMARY KEY,
          selection_id text NOT NULL
            REFERENCES news_brief_selections(selection_id) ON DELETE RESTRICT,
          lane text NOT NULL
            CHECK (lane IN ('ordinary', 'verified_critical', 'rectification')),
          status text NOT NULL
            CHECK (status IN ('pending', 'activated', 'cancelled', 'superseded')),
          first_proposed_at_ms bigint NOT NULL CHECK (first_proposed_at_ms >= 0),
          last_observed_at_ms bigint NOT NULL CHECK (last_observed_at_ms >= first_proposed_at_ms),
          activation_due_at_ms bigint NOT NULL CHECK (activation_due_at_ms >= first_proposed_at_ms),
          resolved_at_ms bigint CHECK (resolved_at_ms IS NULL OR resolved_at_ms >= first_proposed_at_ms),
          reason jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(reason) = 'object'),
          created_at_ms bigint NOT NULL CHECK (created_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_news_brief_proposals_pending
          ON news_brief_proposals((status))
          WHERE status = 'pending'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_brief_proposals_history
          ON news_brief_proposals(first_proposed_at_ms DESC, proposal_id DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE news_brief_activations (
          activation_id text PRIMARY KEY,
          activation_sequence bigint NOT NULL UNIQUE CHECK (activation_sequence >= 1),
          selection_id text NOT NULL
            REFERENCES news_brief_selections(selection_id) ON DELETE RESTRICT,
          proposal_id text NOT NULL UNIQUE
            REFERENCES news_brief_proposals(proposal_id) ON DELETE RESTRICT,
          lane text NOT NULL
            CHECK (lane IN ('ordinary', 'verified_critical', 'rectification')),
          activated_at_ms bigint NOT NULL CHECK (activated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_brief_active (
          singleton_key boolean PRIMARY KEY DEFAULT true CHECK (singleton_key),
          activation_id text NOT NULL
            REFERENCES news_brief_activations(activation_id) ON DELETE RESTRICT,
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_brief_publications (
          publication_id text PRIMARY KEY,
          selection_id text NOT NULL
            REFERENCES news_brief_selections(selection_id) ON DELETE RESTRICT,
          synthesis_input_hash text NOT NULL CHECK (btrim(synthesis_input_hash) <> ''),
          evidence_cutoff_at_ms bigint NOT NULL CHECK (evidence_cutoff_at_ms >= 0),
          model text NOT NULL CHECK (btrim(model) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          workflow_version text NOT NULL CHECK (btrim(workflow_version) <> ''),
          schema_version text NOT NULL CHECK (btrim(schema_version) <> ''),
          locale text NOT NULL CHECK (btrim(locale) <> ''),
          payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
          evidence_references jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(evidence_references) = 'array'),
          receipt jsonb NOT NULL DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(receipt) = 'object'),
          published_at_ms bigint NOT NULL CHECK (published_at_ms >= 0),
          UNIQUE (
            synthesis_input_hash,
            model,
            prompt_version,
            workflow_version,
            schema_version,
            locale
          )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE news_brief_activation_analysis (
          activation_id text NOT NULL
            REFERENCES news_brief_activations(activation_id) ON DELETE CASCADE,
          publication_id text NOT NULL
            REFERENCES news_brief_publications(publication_id) ON DELETE RESTRICT,
          attachment_kind text NOT NULL CHECK (attachment_kind IN ('generated', 'reused')),
          attached_at_ms bigint NOT NULL CHECK (attached_at_ms >= 0),
          superseded_at_ms bigint
            CHECK (
              superseded_at_ms IS NULL
              OR superseded_at_ms >= attached_at_ms
            ),
          PRIMARY KEY (activation_id, publication_id)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_news_brief_activation_analysis_current
          ON news_brief_activation_analysis(activation_id)
          WHERE superseded_at_ms IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE news_ai_attempts (
          attempt_key text PRIMARY KEY,
          publication_kind text NOT NULL
            CHECK (publication_kind IN ('brief', 'story_analysis')),
          target_id text NOT NULL CHECK (btrim(target_id) <> ''),
          evidence_hash text NOT NULL CHECK (btrim(evidence_hash) <> ''),
          model text NOT NULL CHECK (btrim(model) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          workflow_version text NOT NULL CHECK (btrim(workflow_version) <> ''),
          schema_version text NOT NULL CHECK (btrim(schema_version) <> ''),
          locale text NOT NULL CHECK (btrim(locale) <> ''),
          status text NOT NULL
            CHECK (status IN ('running', 'failed', 'available', 'insufficient')),
          attempt_count integer NOT NULL CHECK (attempt_count >= 1),
          repair_count integer NOT NULL DEFAULT 0 CHECK (repair_count BETWEEN 0 AND 1),
          lease_token text NOT NULL CHECK (btrim(lease_token) <> ''),
          lease_expires_at_ms bigint NOT NULL DEFAULT 0 CHECK (lease_expires_at_ms >= 0),
          next_attempt_at_ms bigint NOT NULL DEFAULT 0 CHECK (next_attempt_at_ms >= 0),
          validation_errors jsonb NOT NULL DEFAULT '[]'::jsonb
            CHECK (jsonb_typeof(validation_errors) = 'array'),
          last_error text,
          requested_at_ms bigint NOT NULL CHECK (requested_at_ms >= 0),
          updated_at_ms bigint NOT NULL CHECK (updated_at_ms >= 0),
          UNIQUE (
            publication_kind,
            target_id,
            evidence_hash,
            model,
            prompt_version,
            workflow_version,
            schema_version,
            locale
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_ai_attempts_claim
          ON news_ai_attempts(
            status,
            next_attempt_at_ms,
            lease_expires_at_ms,
            requested_at_ms,
            attempt_key
          )
        """
    )
    op.execute(
        """
        CREATE TABLE news_ai_current_targets (
          publication_kind text NOT NULL
            CHECK (publication_kind IN ('brief', 'story_analysis')),
          target_id text NOT NULL CHECK (btrim(target_id) <> ''),
          evidence_hash text NOT NULL CHECK (btrim(evidence_hash) <> ''),
          model text NOT NULL CHECK (btrim(model) <> ''),
          prompt_version text NOT NULL CHECK (btrim(prompt_version) <> ''),
          workflow_version text NOT NULL CHECK (btrim(workflow_version) <> ''),
          schema_version text NOT NULL CHECK (btrim(schema_version) <> ''),
          locale text NOT NULL CHECK (btrim(locale) <> ''),
          desired_at_ms bigint NOT NULL CHECK (desired_at_ms >= 0),
          PRIMARY KEY (publication_kind, target_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_news_stories_latest
          ON news_stories(last_material_evidence_at_ms DESC, story_id)
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260727_0199 is an irreversible News correctness hard cut; restore the verified pre-cut backup to downgrade"
    )
