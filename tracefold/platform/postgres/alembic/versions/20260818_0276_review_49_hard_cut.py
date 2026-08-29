"""Review #49 hard cut: drop the Translator presentation table, the DEX discovery/profile/image lanes,
and the unused langgraph checkpoint tables.

Revision ID: 20260818_0276
Revises: 20260818_0275
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0276"
down_revision = "20260818_0275"
branch_labels = None
depends_on = None

_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS = (0x54524644, 0)

_DROPPED_TABLES = (
    # News: title translation folded into the Triage verdict (title_zh); no separate presentation state.
    "news_title_presentations",
    # Market: OKX DEX discovery (HTTP 402) and the DEX profile / image lanes are removed.
    "token_discovery_dirty_lookup_keys",
    "token_discovery_results",
    "asset_profile_refresh_targets",
    "asset_profiles",
    "cex_token_profiles",
    "token_image_source_dirty_targets",
    "token_image_assets",
    "token_profile_current",
    "token_profile_projection_frontiers",
    # Platform: langgraph-checkpoint-postgres was never used at runtime.
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    "checkpoint_migrations",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        f"""
        DO $migration$
        BEGIN
          IF NOT pg_try_advisory_xact_lock(
            {_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS[0]},
            {_RUNTIME_MAINTENANCE_GATE_LOCK_KEYS[1]}
          ) THEN
            RAISE EXCEPTION 'review_49_hard_cut_workers_active' USING ERRCODE = '55006';
          END IF;
        END
        $migration$;
        """
    )
    for table in _DROPPED_TABLES:  # every table exists in baseline 0275 and only references retained tables
        op.execute(f"DROP TABLE {table}")
    op.execute(
        """
        DELETE FROM queue_terminal_events
         WHERE source_table IN (
           'token_discovery_dirty_lookup_keys', 'asset_profile_refresh_targets',
           'token_image_source_dirty_targets', 'token_profile_projection_frontiers'
         )
        """
    )
    op.execute("DELETE FROM provider_circuit_state WHERE provider LIKE 'okx_%'")


def downgrade() -> None:
    raise RuntimeError("review_49_hard_cut_is_irreversible")
