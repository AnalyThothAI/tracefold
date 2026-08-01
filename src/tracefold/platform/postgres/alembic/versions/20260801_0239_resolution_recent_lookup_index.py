"""Bound recent resolution lookup work by the material event key.

Revision ID: 20260801_0239
Revises: 20260801_0238
"""

from __future__ import annotations

from alembic import op

revision = "20260801_0239"
down_revision = "20260801_0238"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE token_discovery_dirty_lookup_keys
          DROP CONSTRAINT token_discovery_reprocess_continuation_check,
          ADD CONSTRAINT token_discovery_reprocess_continuation_check CHECK (
            (
              reprocess_lookup_keys IS NULL
              AND reprocess_after_intent_id IS NULL
              AND reprocess_resolved = false
              AND reprocess_queue_due_at_ms IS NULL
            )
            OR (
              cardinality(reprocess_lookup_keys) > 0
              AND (
                reprocess_after_intent_id IS NULL
                OR length(reprocess_after_intent_id) > 0
              )
              AND reprocess_queue_due_at_ms IS NOT NULL
              AND reprocess_queue_due_at_ms >= 0
            )
          );

        CREATE INDEX idx_token_intent_lookup_keys_event_lookup_intent
          ON token_intent_lookup_keys(event_id, lookup_key, intent_id);
        ANALYZE token_intent_lookup_keys;
        """
    )


def downgrade() -> None:
    raise RuntimeError("20260801_0239 is an irreversible resolution hot-path index hard cut")
