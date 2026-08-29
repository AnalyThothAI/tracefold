"""Bound the Verdict-to-Delivery handoff repair scan (#187).

Revision ID: 20260830_0333
Revises: 20260830_0332
"""

from __future__ import annotations

from alembic import op

revision = "20260830_0333"
down_revision = "20260830_0332"
branch_labels = None
depends_on = None

_INDEX = "ix_news_verdicts_unpublished_delivery"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE INDEX {_INDEX}
            ON news_verdicts (created_at_ms, event_id, policy_version)
         WHERE stage = 'triage'
           AND published_at_ms IS NULL
           AND final_decision IN ('push', 'escalate')
        """
    )


def downgrade() -> None:
    raise RuntimeError("news_verdict_handoff_downgrade_unsupported")
