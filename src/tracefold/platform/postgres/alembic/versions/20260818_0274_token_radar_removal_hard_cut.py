"""Remove the Token Radar product surface from the schema.

Revision ID: 20260818_0274
Revises: 20260815_0273
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0274"
down_revision = "20260815_0273"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL transaction_timeout = '300s'")
    op.execute(
        r"""
        DROP TABLE IF EXISTS token_radar_current;

        DROP INDEX IF EXISTS idx_events_token_radar_source_time;
        ALTER TABLE events DROP COLUMN IF EXISTS token_radar_text_fingerprint;

        DROP INDEX IF EXISTS idx_token_intent_resolutions_token_radar_material;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260818_0274 is an irreversible Token Radar removal hard cut")
