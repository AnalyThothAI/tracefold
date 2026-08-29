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
    # The execution policy identity moves in this release (`trade_intent_policy_v2` -> `v3`), and this
    # migration removes the CHECK that pinned it. 0325 guards exactly this class of change and 0326
    # owes the same guard: an Intent frozen under the old policy that survives the cutover would be
    # handed to the engine, which stamps `engine_identity` with the *new* digest and fences a real
    # entry under a policy the row never named. For this payload the numbers happen to be identical —
    # only a throughput key was dropped — but the mechanism that made it impossible is what is being
    # removed here, so the drain has to be asserted rather than assumed.
    op.execute(
        """
        DO $cutover$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM trading_intents
             WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
          ) THEN
            RAISE EXCEPTION 'daily_entry_fence_nonterminal_intent';
          END IF;
        END
        $cutover$
        """
    )

    op.execute("DROP INDEX IF EXISTS ux_trading_intents_one_entry_per_utc_day")

    # The same mistake one layer down. `trading_intents_v2_shape_check` pinned the *value* of
    # `intent_policy_sha256` to one literal digest, so changing the execution policy did not merely
    # move an identity — it made the table unwritable, and would have made every row already in it
    # violate its own constraint had it been revalidated. A shape check should assert the shape: a
    # v2 Intent carries a 64-hex policy digest. *Which* policy it was emitted under is a fact the row
    # records, not a constant the schema is entitled to know.
    #
    # Two honest limits on that principle, so nobody reads more into this edit than it does. The
    # regex is not new protection — `trading_intents_sha256_check` (0316) already asserts 64-hex on
    # this column unconditionally — so the real effect of the DROP+ADD is removing the value pin and
    # nothing else. And the principle is applied here only: the same CHECK still pins
    # `blacklist_snapshot_payload_at_emission ->> 'snapshot_version'` by value, as does
    # `trading_intents_env_check` on `execution_environment`. Those are deliberate: a snapshot
    # version and an execution environment are contracts the schema *is* entitled to know, because
    # changing either is a migration in its own right. A policy digest moves whenever a threshold
    # moves, which is why it does not belong in the same category.
    #
    # What constrains the writer to the current policy is `tests/contract/test_trading_intent_policy
    # _identity.py`, an explicit pin on the digest — the same shape of evidence the Program identity
    # uses. `TradeIntent.create()` cannot serve that role: it assigns the constant and then compares
    # against it, which is a tautology.
    op.execute("ALTER TABLE trading_intents DROP CONSTRAINT IF EXISTS trading_intents_v2_shape_check")
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
