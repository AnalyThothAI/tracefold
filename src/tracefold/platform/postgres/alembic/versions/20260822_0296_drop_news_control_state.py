"""Drop the pause/mute control plane.

`news_control_state` never withheld a card. Across the whole retained history
(5373 triage verdicts) `override_rule = 'muted'` appears zero times and no
delivery settled as `delivery_paused`; the live singleton was still the
`paused = false, mutes = []` it was created with. What it did cost was a
control-state read on every Triage and every Delivery message, a `muted`
parameter on the deterministic `decide()` policy, a CLI command, an HTTP field
and a console banner.

Deleting the table is deliberate rather than leaving it unread: an unused
singleton that two hot-path consumers still SELECT is a standing invitation to
re-grow a second decision plane beside `decide()`.

Revision ID: 20260822_0296
Revises: 20260822_0295
"""

from __future__ import annotations

from alembic import op

revision = "20260822_0296"
down_revision = "20260822_0295"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS news_control_state")


def downgrade() -> None:
    raise RuntimeError("20260822_0296 is an irreversible control-plane removal")
