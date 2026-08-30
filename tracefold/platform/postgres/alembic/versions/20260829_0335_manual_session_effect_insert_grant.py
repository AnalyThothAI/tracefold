"""Grant Workers the two initial manual-session effect-fence columns (#327).

Revision ID: 20260829_0335
Revises: 20260829_0334
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0335"
down_revision = "20260829_0334"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "GRANT INSERT (last_effect_update_id, last_effect_result_code) ON trading_manual_sessions TO tracefold_workers"
    )


def downgrade() -> None:
    raise RuntimeError("20260829_0335 repairs the manual-session effect grant and cannot be downgraded")
