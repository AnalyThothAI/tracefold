"""TradePlan ownership and the native callback/DB hand-off (#644)."""

from tests.nautilus_oi_runtime_fixtures import registered_oi_strategy, trade_signal


def test_entry_cannot_submit_before_the_database_bridge_commits_its_plan() -> None:
    harness = registered_oi_strategy(values=(trade_signal(),))
    harness.strategy.on_timer(None)
    assert harness.strategy.submitted == []


def test_native_position_closed_pnl_includes_recorded_settlement_commissions() -> None:
    from decimal import Decimal

    from nautilus_trader.model.currencies import USDT
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import PositionId, TradeId
    from nautilus_trader.model.objects import Money
    from nautilus_trader.model.position import Position
    from nautilus_trader.test_kit.stubs.events import TestEventStubs

    from tests.nautilus_oi_runtime_fixtures import ACCOUNT_ID, NOW_NS

    harness = registered_oi_strategy()
    instrument = harness.instrument
    buy = harness.strategy.order_factory.market(
        instrument_id=instrument.id, order_side=OrderSide.BUY, quantity=instrument.make_qty(Decimal("0.05"))
    )
    opened = TestEventStubs.order_filled(
        buy,
        instrument=instrument,
        account_id=ACCOUNT_ID,
        position_id=PositionId("PNL-PROOF"),
        last_px=instrument.make_price(Decimal("10000")),
        commission=Money("0.2", USDT),
        trade_id=TradeId("PNL-ENTRY"),
        ts_event=NOW_NS,
    )
    position = Position(instrument, opened)
    sell = harness.strategy.order_factory.market(
        instrument_id=instrument.id,
        order_side=OrderSide.SELL,
        quantity=instrument.make_qty(Decimal("0.05")),
        reduce_only=True,
    )
    closed = TestEventStubs.order_filled(
        sell,
        instrument=instrument,
        account_id=ACCOUNT_ID,
        position_id=position.id,
        last_px=instrument.make_price(Decimal("10100")),
        commission=Money("0.3", USDT),
        trade_id=TradeId("PNL-EXIT"),
        ts_event=NOW_NS + 1,
    )
    position.apply(closed)
    event = TestEventStubs.position_closed(position)
    assert position.is_closed
    assert event.realized_pnl.as_decimal() == Decimal("4.5")  # 5 price PnL - 0.2 - 0.3 recorded fees.


def test_delayed_commit_cannot_submit_against_a_stale_quote() -> None:
    from tests.nautilus_oi_runtime_fixtures import NOW_NS

    harness = registered_oi_strategy(values=(trade_signal(),))
    harness.strategy.on_timer(None)
    plan = harness.plans.pending_prepare()
    assert plan is not None
    harness.plans.committed(plan, newly_committed=True)
    harness.clock.set_time(NOW_NS + 11_000_000_000)
    harness.readiness.reconciled(
        account_observed_at_ns=NOW_NS + 11_000_000_000, reconciliation_observed_at_ns=NOW_NS + 11_000_000_000
    )
    harness.strategy.on_timer(None)
    assert harness.strategy.submitted == []
    assert harness.plans.pending_updates()[0].exit_reason == "not_submitted"
