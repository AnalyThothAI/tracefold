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

Two backfills, and only where the answer is a fact rather than a guess:

* ``taker_fee_bps`` is set to 5 on every non-terminal row. It is not now and has never been a
  configuration key — ``TradingOrderSettings`` forbids extras and declares no fee field — so
  ``DEFAULT_TAKER_FEE_BPS = 5`` is the only value any ``realized_bps`` has ever been charged at.
  Writing it down is recording history, not inventing it.
* ``max_holding_ms`` is set to ``must_close_at_ms - position_opened_at_ms`` wherever both exist.
  ``promote_acknowledged`` writes that pair in one statement, so their difference *is* the holding
  budget that governed the row. Reading today's configuration for it instead would contradict the
  deadline the row is actually going to close on.

Terminal history is not backfilled: a closed order's realised return is already a durable fact and
the inputs that produced it are of no further use. An active row that never opened a position has no
provable budget — nothing in the database says what ``max_holding_seconds`` was when it was approved
— so it is left NULL and the reconciler sends it to ``MANUAL_REVIEW_REQUIRED`` rather than quietly
applying whatever configuration is in force after the deploy. That is the same drain every other
unprovable outcome uses, and on a deployment where Trading has been disabled there is nothing to
drain.

Revision ID: 20260825_0308
Revises: 20260825_0307
"""

from __future__ import annotations

from alembic import op

revision = "20260825_0308"
down_revision = "20260825_0307"
branch_labels = None
depends_on = None

# Historical state snapshot used by this migration to rebuild the partial unique index.
_ACTIVE_ORDER_STATES = (
    "PREPARED",
    "AWAITING_APPROVAL",
    "APPROVED",
    "SUBMITTING",
    "AMBIGUOUS",
    "RECONCILING",
    "MANUAL_REVIEW_REQUIRED",
    "ACKNOWLEDGED",
    "PARTIAL",
    "OPEN",
    "UNPROTECTED",
    "SAFETY_CLOSING",
)
# `DEFAULT_TAKER_FEE_BPS`. Repeated as a literal because a migration is a historical record and must
# not change meaning when the constant it mirrors does.
_HISTORICAL_TAKER_FEE_BPS = 5


def upgrade() -> None:
    op.execute("ALTER TABLE trading_orders ADD COLUMN max_holding_ms BIGINT")
    op.execute("ALTER TABLE trading_orders ADD COLUMN taker_fee_bps INTEGER")
    active = ", ".join(f"'{state}'" for state in _ACTIVE_ORDER_STATES)
    op.execute(
        f"""
        UPDATE trading_orders
           SET taker_fee_bps = {_HISTORICAL_TAKER_FEE_BPS},
               max_holding_ms = CASE
                 WHEN must_close_at_ms IS NOT NULL
                  AND position_opened_at_ms IS NOT NULL
                  AND must_close_at_ms > position_opened_at_ms
                 THEN must_close_at_ms - position_opened_at_ms
                 ELSE NULL
               END
         WHERE state IN ({active})
        """
    )
    # A snapshot that is present must be usable. `NULL` means "pre-#209, never provable" and is the
    # only absence the runners accept — and they refuse to manage such a row rather than reading a
    # configuration for it. Zero or negative would be a snapshot that silently disables the holding
    # deadline, which is exactly the drift this migration exists to stop.
    op.execute(
        "ALTER TABLE trading_orders ADD CONSTRAINT trading_orders_max_holding_positive "
        "CHECK (max_holding_ms IS NULL OR max_holding_ms > 0)"
    )
    op.execute(
        "ALTER TABLE trading_orders ADD CONSTRAINT trading_orders_taker_fee_non_negative "
        "CHECK (taker_fee_bps IS NULL OR taker_fee_bps >= 0)"
    )


def downgrade() -> None:
    # `20260823_0300` refuses for the same reason, and these two columns are that same capital state:
    # dropping them destroys the frozen exit policy of every order, including open ones, and a
    # re-upgrade would then leave the rows unprovable. A downgrade/upgrade cycle would be a silent way
    # to perform exactly the exit-policy rewrite this revision exists to make impossible.
    raise RuntimeError("20260825_0308 owns the frozen exit policy of open orders; dropping it rewrites it")
