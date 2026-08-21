"""Durable one-arm canary assignment and activation control.

Revision ID: 20260821_0287
Revises: 20260821_0286
"""

from __future__ import annotations

from alembic import op

revision = "20260821_0287"
down_revision = "20260821_0286"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE news_canary_activations (
          activation_id           text   PRIMARY KEY,
          baseline_bundle_sha     text   NOT NULL,
          candidate_manifest_sha text   NOT NULL,
          candidate_bundle_sha    text   NOT NULL,
          selector_version        text   NOT NULL,
          exposure_bps            integer NOT NULL,
          eligibility_profile_sha text   NOT NULL,
          rolling_profile_sha     text   NOT NULL,
          state                   text   NOT NULL,
          revision                integer NOT NULL DEFAULT 1,
          trip_reason             text,
          hold_reason             text,
          rolling_last_bucket_ms  bigint,
          rolling_breach_windows  integer NOT NULL DEFAULT 0,
          created_at_ms           bigint NOT NULL,
          activated_at_ms         bigint,
          held_at_ms              bigint,
          resumed_at_ms           bigint,
          tripped_at_ms           bigint,
          closed_at_ms            bigint,
          CONSTRAINT news_canary_activation_id CHECK (
            activation_id ~ '^[0-9a-f]{32}$'
          ),
          CONSTRAINT news_canary_baseline_sha CHECK (baseline_bundle_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_canary_candidate_manifest_sha CHECK (candidate_manifest_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_canary_candidate_sha CHECK (candidate_bundle_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_canary_profile_sha CHECK (eligibility_profile_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_canary_rolling_profile_sha CHECK (rolling_profile_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_canary_exposure CHECK (exposure_bps BETWEEN 1 AND 10000),
          CONSTRAINT news_canary_state CHECK (state IN ('armed', 'active', 'tripped', 'closed')),
          CONSTRAINT news_canary_revision CHECK (revision >= 1)
          ,CONSTRAINT news_canary_rolling_breaches CHECK (rolling_breach_windows >= 0)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_news_canary_one_open "
        "ON news_canary_activations ((1)) WHERE state IN ('armed', 'active')"
    )
    op.execute("CREATE INDEX ix_news_canary_activations_created ON news_canary_activations (created_at_ms DESC)")
    op.execute(
        """
        CREATE TABLE news_agent_assignments (
          event_id              text   PRIMARY KEY REFERENCES news_events(event_id) ON DELETE CASCADE,
          activation_id         text   REFERENCES news_canary_activations(activation_id),
          arm                   text   NOT NULL,
          bundle_sha            text   NOT NULL,
          selector_version      text   NOT NULL,
          eligibility_reason    text   NOT NULL,
          assigned_at_ms        bigint NOT NULL,
          CONSTRAINT news_agent_assignment_arm CHECK (arm IN ('stable', 'candidate')),
          CONSTRAINT news_agent_assignment_bundle CHECK (bundle_sha ~ '^[0-9a-f]{64}$')
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_news_agent_assignments_activation "
        "ON news_agent_assignments (activation_id, arm, assigned_at_ms DESC)"
    )
    op.execute(
        """
        CREATE TABLE news_agent_runtime_manifests (
          manifest_sha       text   PRIMARY KEY,
          stable_bundle_sha  text   NOT NULL,
          candidate_shas     jsonb  NOT NULL,
          image_digest       text   NOT NULL,
          runtime_revision   text   NOT NULL,
          registered_at_ms   bigint NOT NULL,
          CONSTRAINT news_agent_manifest_sha CHECK (manifest_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_agent_manifest_stable_sha CHECK (stable_bundle_sha ~ '^[0-9a-f]{64}$'),
          CONSTRAINT news_agent_manifest_candidates CHECK (jsonb_typeof(candidate_shas) = 'array')
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_news_canary_append_only_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'news_canary_append_only';
        END;
        $$
        """
    )
    for table in ("news_agent_assignments", "news_agent_runtime_manifests"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_news_canary_append_only_mutation()"
        )
    op.execute(
        "GRANT SELECT ON news_canary_activations, news_agent_assignments, news_agent_runtime_manifests "
        "TO tracefold_serve"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON news_canary_activations TO tracefold_workers")
    op.execute("GRANT SELECT, INSERT ON news_agent_assignments, news_agent_runtime_manifests TO tracefold_workers")
    op.execute("REVOKE DELETE ON news_canary_activations FROM tracefold_workers")
    op.execute("REVOKE UPDATE, DELETE ON news_agent_assignments, news_agent_runtime_manifests FROM tracefold_workers")


def downgrade() -> None:
    raise RuntimeError("20260821_0287 is an irreversible canary-control contract")
