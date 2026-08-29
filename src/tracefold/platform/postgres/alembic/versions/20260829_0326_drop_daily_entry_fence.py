"""Drop the one-entry-per-UTC-day fence, and unpin the policy digest (#348).

The fence capped how many theses the lane could act on in a day. It never bounded exposure: that is
`ux_trading_intents_one_active`, a unique index admitting a single nonterminal Intent, which this
migration leaves untouched. What the daily cap actually produced was a blind spot — after the day's
first entry every later frame was refused *before* the policy ran, so on exactly the days the lane
was working it could not say which of the day's remaining frames it should have taken. Measured over
seven days of production frames it would have capped the busiest day at one of six qualifying frames.

One-way. Recreating the index would fail against any day that has since taken more than one entry,
and re-imposing a throughput cap is a product decision, not a rollback.

Revision ID: 20260829_0326
Revises: 20260829_0325
"""

from __future__ import annotations

from alembic import op

revision = "20260829_0326"
down_revision = "20260829_0325"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_trading_intents_one_entry_per_utc_day")

    # The same mistake one layer down. `trading_intents_v2_shape_check` pinned the *value* of
    # `intent_policy_sha256` to one literal digest, so changing the execution policy did not merely
    # move an identity — it made the table unwritable, and would have made every row already in it
    # violate its own constraint had it been revalidated. A shape check should assert the shape: a
    # v2 Intent carries a 64-hex policy digest. *Which* policy it was emitted under is a fact the row
    # records, not a constant the schema is entitled to know. The writer is what must be constrained
    # to the current policy, and `TradeIntent.create()` is where that now lives.
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT trading_intents_v2_shape_check")
    op.execute(
        """
        ALTER TABLE trading_intents
          ADD CONSTRAINT trading_intents_v2_shape_check CHECK (
            (
              intent_version = 'trade_intent_v1'
              AND execution_capability_snapshot_sha256 IS NULL
              AND blacklist_revision_at_emission IS NULL
              AND blacklist_snapshot_sha256_at_emission IS NULL
              AND blacklist_snapshot_payload_at_emission IS NULL
              AND underlying_key IS NULL
            )
            OR (
              intent_version = 'trade_intent_v2'
              AND intent_policy_sha256 ~ '^[0-9a-f]{64}$'
              AND execution_capability_snapshot_sha256 IS NOT NULL
              AND blacklist_revision_at_emission IS NOT NULL
              AND blacklist_snapshot_sha256_at_emission ~ '^[0-9a-f]{64}$'
              AND blacklist_snapshot_payload_at_emission ->> 'snapshot_version' = 'blacklist_snapshot_v1'
              AND underlying_key ~ '^crypto:[A-Z0-9]{1,32}$'
            )
          )
        """
    )


def downgrade() -> None:
    raise RuntimeError("daily_entry_fence_downgrade_unsupported")
