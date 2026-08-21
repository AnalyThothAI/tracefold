"""Drop the News Strategy allowlist columns.

`news.opennews_strategy_ids` was a second switch for a decision the provider
account already owns: Tracefold sends no subscription frame, so the socket
pushes what the account has enabled, and the local list only decided what the
Receiver threw away. With the list gone there is no configured set to record
and nothing to disagree with, so `configured_strategy_ids` and the
`strategy_warnings` they produced go with it.

`provider_enabled_strategy_ids` stays: it is the observed provider fact the
status surface reports and recovery reads.

Revision ID: 20260821_0291
Revises: 20260821_0290
"""

from __future__ import annotations

from alembic import op

revision = "20260821_0291"
down_revision = "20260821_0290"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE news_ingest_state DROP COLUMN IF EXISTS configured_strategy_ids")
    op.execute("ALTER TABLE news_ingest_state DROP COLUMN IF EXISTS strategy_warnings")


def downgrade() -> None:
    raise RuntimeError("20260821_0291 is an irreversible strategy allowlist hard cut")
