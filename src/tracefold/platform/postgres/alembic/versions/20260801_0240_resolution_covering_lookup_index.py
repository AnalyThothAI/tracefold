"""Cover the intent-ordered resolution lookup scan.

Revision ID: 20260801_0240
Revises: 20260801_0239
"""

from __future__ import annotations

from alembic import op

revision = "20260801_0240"
down_revision = "20260801_0239"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP INDEX idx_token_intent_lookup_keys_event_lookup_intent;
        DROP INDEX idx_token_intent_lookup_keys_intent_lookup;
        CREATE INDEX idx_token_intent_lookup_keys_intent_lookup
          ON token_intent_lookup_keys(intent_id, lookup_key)
          INCLUDE(event_id);
        ANALYZE token_intent_lookup_keys;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260801_0240 is an irreversible resolution covering-index hard cut")
