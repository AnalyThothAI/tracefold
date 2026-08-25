"""Freeze the exit policy onto the order row when the intent is approved (#209).

``stop_price`` and ``take_profit_price`` were already persisted as absolute prices, so the two
price-based exits have always been reproducible from the row alone. The other two numbers were not:

* ``max_holding_ms`` lived only in the running configuration. ``must_close_at_ms`` is first written
  when reconciliation promotes an ACKNOWLEDGED order to OPEN, so an order approved before a restart
  and promoted after one got its holding deadline from *the configuration in force at promotion*.
* the taker fee charged on both legs of ``realized_bps`` was read from the same live configuration at
  close time, so replaying an old close under a new fee assumption produced a different number.

Neither is a funds-management question. Both are audit questions: an order that has already been
approved must not have its execution semantics rewritten by a later deploy.

Both columns are nullable. Terminal history is not backfilled — a closed order's realised return is
already a durable fact and inventing the inputs that produced it would be worse than leaving them
absent. Active pre-#209 rows are frozen once, at their next reconcile turn, against the configuration
then in force and recorded as a ``legacy_runtime_snapshot`` observation, so the row still says which
numbers governed it and nobody has to guess later.

Revision ID: 20260825_0308
Revises: 20260825_0307
"""

from __future__ import annotations

from alembic import op

revision = "20260825_0308"
down_revision = "20260825_0307"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE trading_orders ADD COLUMN max_holding_ms BIGINT")
    op.execute("ALTER TABLE trading_orders ADD COLUMN taker_fee_bps INTEGER")
    # A snapshot that is present must be usable. `NULL` means "pre-#209, not yet frozen" and is the
    # only absence the runners accept; zero or negative would be a snapshot that silently disables the
    # holding deadline, which is exactly the drift this migration exists to stop.
    op.execute(
        "ALTER TABLE trading_orders ADD CONSTRAINT trading_orders_max_holding_positive "
        "CHECK (max_holding_ms IS NULL OR max_holding_ms > 0)"
    )
    op.execute(
        "ALTER TABLE trading_orders ADD CONSTRAINT trading_orders_taker_fee_non_negative "
        "CHECK (taker_fee_bps IS NULL OR taker_fee_bps >= 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE trading_orders DROP CONSTRAINT IF EXISTS trading_orders_taker_fee_non_negative")
    op.execute("ALTER TABLE trading_orders DROP CONSTRAINT IF EXISTS trading_orders_max_holding_positive")
    op.execute("ALTER TABLE trading_orders DROP COLUMN IF EXISTS taker_fee_bps")
    op.execute("ALTER TABLE trading_orders DROP COLUMN IF EXISTS max_holding_ms")
