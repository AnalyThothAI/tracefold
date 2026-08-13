"""Restore the Token Radar source-time covering read visibility.

Revision ID: 20260813_0264
Revises: 20260813_0263
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0264"
down_revision = "20260813_0263"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        ALTER TABLE events SET (
          autovacuum_vacuum_scale_factor = 0.01,
          autovacuum_vacuum_threshold = 10000,
          autovacuum_vacuum_insert_scale_factor = 0.01,
          autovacuum_vacuum_insert_threshold = 10000
        );

        ANALYZE token_intents;
        ANALYZE token_intent_resolutions;
        """
    )

    # 0261 rewrote every Events heap page. Restore the visibility map before
    # Workers resume the source-time index-only replay.
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '120s'")
        op.execute("VACUUM (ANALYZE) events")
        op.execute("RESET statement_timeout")


def downgrade() -> None:
    raise RuntimeError("20260813_0264 is an irreversible Token Radar visibility cut")
