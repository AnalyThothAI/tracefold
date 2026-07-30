"""Persist the compact News Story projection input fingerprint."""

from __future__ import annotations

from alembic import op

revision = "20260730_0217"
down_revision = "20260729_0216"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE news_story_input_state (
          singleton_key text PRIMARY KEY
            CHECK (singleton_key = 'current'),
          input_fingerprint text NOT NULL
            CHECK (btrim(input_fingerprint) <> ''),
          scoring_epoch_ms bigint NOT NULL
            CHECK (scoring_epoch_ms >= 0),
          item_count integer NOT NULL
            CHECK (item_count >= 0),
          temporary_cluster_count integer NOT NULL
            CHECK (temporary_cluster_count >= 0),
          story_count integer NOT NULL
            CHECK (story_count >= 0),
          projected_at_ms bigint NOT NULL
            CHECK (projected_at_ms >= 0)
        );
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260730_0217 is an irreversible News projection-state hard cut")
