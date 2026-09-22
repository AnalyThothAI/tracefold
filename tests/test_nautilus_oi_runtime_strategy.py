"""The OI Runtime Strategy on real Nautilus: Nautilus owns every order and position (#680 PR-1).

The first half runs the production Strategy inside a real `BacktestEngine` -- real Cache, execution and
risk engines, and a simulated venue that fills, triggers and cancels -- so what it proves is what the
Strategy does to real Nautilus orders and positions. The second half registers the same Strategy on a
bare Cache whose every fact the test writes, for the gates and verdicts that need an exact picture.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide, OrderType, TradingState, TriggerType
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.identifiers import ClientOrderId, StrategyId

from tests.nautilus_oi_runtime_fixtures import (
    ACCOUNT_ID,
    INSTRUMENT,
    MARKET,
    NOW_NS,
    SECOND_NS,
    backtest_runtime,
    cached_position,
    cached_protection,
    oi_profile,
    open_plan,
    operator_intent,
    quotes,
    seed_reconciled_position,
    trade_signal,
    unit_runtime,
)
from tracefold.integrations.nautilus.oi_runtime.entry import deterministic_client_order_id
from tracefold.integrations.nautilus.oi_runtime.strategy import OpenPlan, RuntimeControlSnapshot

_NAMESPACE = oi_profile().namespace
_ENTRY_ID = "1" * 64


def _leg_id(leg: str, entry_id: str = _ENTRY_ID) -> ClientOrderId:
    return deterministic_client_order_id(namespace=_NAMESPACE, entry_id=entry_id, leg=leg)


def _by_id(orders: list[Any]) -> dict[str, Any]:
    return {order.client_order_id.value: order for order in orders}


def _rejected(order: Any, reason: str) -> OrderRejected:
    return OrderRejected(
        trader_id=order.trader_id,
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        account_id=ACCOUNT_ID,
        reason=reason,
        event_id=UUID4(),
        ts_event=NOW_NS,
        ts_init=NOW_NS,
    )


# -- real Nautilus ---------------------------------------------------------------------------------


def test_an_entry_fill_places_one_mark_price_stop_and_take_profit_and_only_then_says_accepted() -> None:
    runtime = backtest_runtime(
        tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=20),
        signals=(trade_signal(),),
    )
    runtime.run()

    orders = _by_id(runtime.orders())
    entry = orders[_leg_id("entry").value]
    assert entry.order_type == OrderType.MARKET and entry.side == OrderSide.BUY and not entry.is_reduce_only
    assert entry.quantity.as_decimal() == runtime.receipts[0].plan.entry_quantity
    stop = orders[_leg_id("stop").value]
    take_profit = orders[_leg_id("take_profit").value]
    for order, kind, trigger in (
        (stop, OrderType.STOP_MARKET, 9_800),
        (take_profit, OrderType.MARKET_IF_TOUCHED, 10_200),
    ):
        assert order.order_type == kind
        assert order.side == OrderSide.SELL
        assert order.is_reduce_only
        assert order.trigger_type == TriggerType.MARK_PRICE
        assert order.trigger_price == INSTRUMENT.make_price(trigger)
        assert order.quantity == entry.quantity
        assert order.is_open
    assert len(orders) == 3

    kinds = [(value.normalized_kind, value.summary.get("leg")) for value in runtime.observations()]
    assert kinds.index(("signal_disposition", None)) > kinds.index(("fill", "entry"))
    assert runtime.dispositions() == [{"disposition": "accepted"}]
    [plan] = runtime.plans()
    assert plan.status == "open" and plan.opened_at_ns is not None


def test_the_plan_is_committed_before_its_entry_order_exists_and_a_refused_commit_sends_nothing() -> None:
    runtime = backtest_runtime(
        tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
        signals=(trade_signal(),),
        refuse_plans=True,
    )
    runtime.run()

    assert [receipt.committed for receipt in runtime.receipts] == [False]
    assert runtime.orders() == []
    assert runtime.dispositions() == [{"disposition": "trade_plan_rejected"}]


def test_a_stop_fill_ends_the_plan_as_stop_filled_cancels_the_take_profit_and_records_fees() -> None:
    runtime = backtest_runtime(
        tape=[
            *quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
            *quotes(9_700, 9_701, start_ns=NOW_NS + 2 * SECOND_NS, count=10),
        ],
        signals=(trade_signal(),),
    )
    runtime.run()

    orders = _by_id(runtime.orders())
    assert orders[_leg_id("stop").value].is_closed
    assert orders[_leg_id("take_profit").value].is_closed
    assert runtime.engine.cache.positions_open() == []
    [plan] = runtime.plans()
    assert (plan.status, plan.exit_reason) == ("closed", "stop_filled")
    fills = runtime.observations("fill")
    assert [fill.summary["leg"] for fill in fills] == ["entry", "stop"]
    assert all(
        fill.summary["commission_currency"] == "USDT" and Decimal(str(fill.summary["commission"])) > 0 for fill in fills
    )
    [closed] = [value for value in runtime.observations("position") if value.summary["status"] == "closed"]
    assert closed.summary["exit_reason"] == "stop_filled"
    assert closed.signal_id == _ENTRY_ID


def test_a_take_profit_fill_ends_the_plan_as_take_profit_and_cancels_the_stop() -> None:
    runtime = backtest_runtime(
        tape=[
            *quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
            *quotes(10_300, 10_301, start_ns=NOW_NS + 2 * SECOND_NS, count=10),
        ],
        signals=(trade_signal(),),
    )
    runtime.run()

    orders = _by_id(runtime.orders())
    assert orders[_leg_id("stop").value].is_closed and not orders[_leg_id("stop").value].filled_qty.as_decimal()
    [plan] = runtime.plans()
    assert (plan.status, plan.exit_reason) == ("closed", "take_profit")


def test_a_position_held_past_its_maximum_is_closed_at_market_as_a_time_exit() -> None:
    profile = oi_profile()
    short_hold = replace(profile, exit_policy=replace(profile.exit_policy, max_holding_ns=1 * SECOND_NS))
    runtime = backtest_runtime(
        tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=80),
        signals=(trade_signal(),),
        profile=short_hold,
    )
    runtime.run()

    closing = [order for order in runtime.orders() if order.order_type == OrderType.MARKET and order.is_reduce_only]
    assert len(closing) == 1 and "time_exit" in (closing[0].tags or [])
    [plan] = runtime.plans()
    assert (plan.status, plan.exit_reason) == ("closed", "time_exit")
    assert runtime.engine.cache.positions_open() == []


def test_a_restart_with_a_position_and_both_orders_adopts_them_and_sends_nothing() -> None:
    """The acceptance case: after reconciliation the Strategy submits, cancels and closes nothing."""

    plan = open_plan()
    runtime = backtest_runtime(
        tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=120),
        open_plans=(OpenPlan(plan, disposition_pending=False),),
        seed=seed_reconciled_position,
    )
    runtime.run()

    orders = runtime.orders()
    assert sorted(order.client_order_id.value for order in orders) == [
        "RECONCILED-ENTRY",
        "RECONCILED-STOP",
        "RECONCILED-TP",
    ]
    assert [order.client_order_id.value for order in orders if order.is_open] == ["RECONCILED-STOP", "RECONCILED-TP"]
    assert len(runtime.engine.cache.positions_open()) == 1
    assert runtime.plans() == []
    assert runtime.observations("risk") == []
    assert runtime.strategy.runtime_view(NOW_NS + 12 * SECOND_NS).protection_status == "protected"


def test_a_restart_that_finds_the_stop_missing_places_it_again_and_touches_nothing_else() -> None:
    plan = open_plan()

    def seed(engine: Any, strategy: Any) -> None:
        seed_reconciled_position(engine, strategy, stop=None)

    runtime = backtest_runtime(
        tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=60),
        open_plans=(OpenPlan(plan, disposition_pending=False),),
        seed=seed,
    )
    runtime.run()

    orders = _by_id(runtime.orders())
    stop = orders[_leg_id("stop").value]
    assert stop.order_type == OrderType.STOP_MARKET and stop.is_open and stop.trigger_type == TriggerType.MARK_PRICE
    assert stop.trigger_price == INSTRUMENT.make_price(9_800)
    assert orders["RECONCILED-TP"].is_open
    assert len(orders) == 3


def test_a_restart_that_finds_the_plans_position_opens_it_and_writes_the_verdict_it_owed() -> None:
    plan = open_plan(opened_at_ns=None)
    runtime = backtest_runtime(
        tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=20),
        open_plans=(OpenPlan(plan, disposition_pending=True),),
        seed=seed_reconciled_position,
    )
    runtime.run()

    [opened] = runtime.plans()
    assert (opened.status, opened.opened_at_ns) == ("open", plan.created_at_ns)
    assert runtime.dispositions() == [{"disposition": "accepted"}]


def test_a_refused_entry_ends_its_plan_now_as_not_submitted_and_its_signal_is_not_accepted() -> None:
    runtime = backtest_runtime(
        tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
        signals=(trade_signal(),),
    )
    # The pre-trade risk engine refuses every order that adds exposure, as a venue would, in its words.
    runtime.engine.kernel.risk_engine.set_trading_state(TradingState.HALTED)
    runtime.run()

    [plan] = runtime.plans()
    assert (plan.status, plan.exit_reason, plan.opened_at_ns) == ("closed", "not_submitted", None)
    [disposition] = runtime.dispositions()
    assert disposition["disposition"] == "venue_rejected"
    assert "HALTED" in str(disposition["venue_reason"])
    assert runtime.engine.cache.positions_open() == []


# -- the gates, on a bare Cache ---------------------------------------------------------------------


def test_entries_paused_and_halted_refuse_before_anything_is_sized() -> None:
    for control, reason in (
        (RuntimeControlSnapshot(entries_paused=True, emergency_halted=False), "entries_paused"),
        (RuntimeControlSnapshot(entries_paused=True, emergency_halted=True), "emergency_halted"),
    ):
        runtime = unit_runtime(signals=(trade_signal(),), control=control)
        runtime.pump()
        assert runtime.journal.pending_prepare() is None
        assert runtime.dispositions() == [{"disposition": reason}]


def test_a_stop_out_cools_the_market_down_for_signals_but_not_for_a_manual_entry() -> None:
    stopped = {MARKET: NOW_NS - 60 * SECOND_NS}
    runtime = unit_runtime(signals=(trade_signal(),), stop_exits=stopped)
    runtime.pump()
    assert runtime.dispositions() == [{"disposition": "post_stop_cooldown"}]

    manual = operator_intent(command_id="6" * 64, action="manual_entry", market_key=MARKET, direction="long")
    runtime = unit_runtime(commands=(manual,), stop_exits=stopped)
    runtime.pump()
    assert runtime.settle() is not None
    assert [order.client_order_id for order, _ in runtime.strategy.submitted] == [_leg_id("entry", "6" * 64)]

    expired = unit_runtime(signals=(trade_signal(),), stop_exits={MARKET: NOW_NS - 5 * 3_600 * SECOND_NS})
    expired.pump()
    assert expired.settle() is not None


def test_position_limit_counts_the_instrument_a_plan_already_holds() -> None:
    runtime = unit_runtime(
        signals=(trade_signal(signal_id="2" * 64),),
        open_plans=(OpenPlan(open_plan(), disposition_pending=False),),
    )
    cached_position(runtime)
    cached_protection(runtime, leg="stop", trigger=Decimal(9_800))
    cached_protection(runtime, leg="take_profit", trigger=Decimal(10_200))
    runtime.pump()
    assert runtime.dispositions() == [{"disposition": "position_limit"}]


def test_the_daily_loss_limit_counts_what_this_trade_can_still_lose() -> None:
    # 40 already lost, 10 at risk: 50 is the limit and 50 is not over it.
    within = unit_runtime(signals=(trade_signal(),), day_start_equity=Decimal(1_040), profile=oi_profile())
    within.pump()
    assert within.settle() is not None
    # 45 already lost + ~10 at risk crosses 50, although the day has not lost 50 yet.
    over = unit_runtime(signals=(trade_signal(),), day_start_equity=Decimal(1_045))
    over.pump()
    assert over.journal.pending_prepare() is None
    assert over.dispositions() == [{"disposition": "daily_loss_limit"}]


def test_an_unrouted_market_is_refused_by_name() -> None:
    signal = trade_signal().model_copy(update={"market_key": "crypto:perp:NOPE:USDT"})
    runtime = unit_runtime(signals=(signal,))
    runtime.pump()
    assert runtime.dispositions() == [{"disposition": "instrument_unmapped"}]


def test_an_entry_waits_for_its_first_quote_within_its_ttl_and_subscribes_only_its_instrument() -> None:
    runtime = unit_runtime(signals=(trade_signal(),), with_quote=False)
    runtime.pump()
    assert runtime.dispositions() == []
    assert runtime.journal.pending_prepare() is None
    assert runtime.strategy.subscribed == [INSTRUMENT.id]
    runtime.advance(3 * SECOND_NS)
    runtime.pump()
    assert runtime.journal.pending_prepare() is None
    runtime.add_quote(9_999, 10_000)
    runtime.pump()
    assert runtime.settle() is not None


def test_a_quote_that_never_arrives_ends_the_signal_at_its_ttl_with_the_reason_it_waited_for() -> None:
    runtime = unit_runtime(signals=(trade_signal(expires_at_ns=NOW_NS + 5 * SECOND_NS),), with_quote=False)
    runtime.pump()
    runtime.advance(6 * SECOND_NS)
    runtime.pump()
    assert runtime.dispositions() == [{"disposition": "market_unavailable"}]
    runtime.pump()
    assert runtime.strategy.unsubscribed == [INSTRUMENT.id]


def test_a_spread_wider_than_its_share_of_the_stop_waits_and_ends_with_the_spread_it_measured() -> None:
    # The route's stop is 200 bps, so the widest admissible spread is 0.3 x 200 = 60 bps.
    runtime = unit_runtime(signals=(trade_signal(expires_at_ns=NOW_NS + 5 * SECOND_NS),), with_quote=False)
    runtime.add_quote(9_950, 10_050)
    runtime.pump()
    assert runtime.dispositions() == []
    runtime.advance(6 * SECOND_NS)
    runtime.add_quote(9_950, 10_050)
    runtime.pump()
    assert runtime.dispositions() == [{"disposition": "spread_limit", "spread_bps": "100.00"}]


def test_a_spread_that_narrows_within_the_ttl_admits_the_entry() -> None:
    runtime = unit_runtime(signals=(trade_signal(),), with_quote=False)
    runtime.add_quote(9_950, 10_050)
    runtime.pump()
    runtime.advance(SECOND_NS)
    runtime.add_quote(9_998, 10_000)
    runtime.pump()
    plan = runtime.settle()
    assert plan is not None and plan.entry_quantity > 0


def test_orders_resting_on_the_instrument_make_the_entry_wait_rather_than_share_it() -> None:
    runtime = unit_runtime(signals=(trade_signal(expires_at_ns=NOW_NS + 5 * SECOND_NS),))
    cached_protection(runtime, leg="stop", trigger=Decimal(9_800))
    runtime.pump()
    assert runtime.journal.pending_prepare() is None
    runtime.advance(6 * SECOND_NS)
    runtime.pump()
    assert {"disposition": "instrument_busy"} in runtime.dispositions()


def test_exposure_no_plan_claims_blocks_entries_is_recorded_and_is_never_flattened() -> None:
    runtime = unit_runtime(signals=(trade_signal(),))
    cached_position(runtime, strategy_id=StrategyId("EXTERNAL"))
    runtime.pump()
    assert runtime.dispositions() == [{"disposition": "unexpected_exposure"}]
    [risk] = runtime.observations("risk")
    assert risk.summary["risk_fact"] == "unexpected_exposure" and risk.summary["count"] == 1
    assert runtime.strategy.closed == [] and runtime.strategy.submitted == []
    view = runtime.strategy.runtime_view(NOW_NS)
    assert (view.entries_armed, view.entry_block_reason, view.unexpected_exposure) == (
        False,
        "unexpected_exposure",
        True,
    )


def test_a_venue_rejection_ends_the_plan_with_the_venues_words() -> None:
    plan = open_plan(opened_at_ns=None)
    runtime = unit_runtime(open_plans=(OpenPlan(plan, disposition_pending=True),))
    entry = runtime.strategy.order_factory.market(
        instrument_id=INSTRUMENT.id,
        order_side=OrderSide.BUY,
        quantity=INSTRUMENT.make_qty(plan.entry_quantity),
        client_order_id=ClientOrderId(plan.entry_client_order_id),
    )
    runtime.cache.add_order(entry)
    reason = "code=-4411, msg=Please sign TradFi-Perps agreement contract."
    runtime.strategy.on_order_rejected(_rejected(entry, reason))

    [closed] = runtime.plans()
    assert (closed.status, closed.exit_reason) == ("closed", "not_submitted")
    assert runtime.dispositions() == [{"disposition": "venue_rejected", "venue_reason": reason}]
    [order] = runtime.observations("order")
    assert (order.summary["leg"], order.summary["status"], order.summary["reason"]) == ("entry", "rejected", reason)


def test_a_stop_the_venue_says_would_trigger_immediately_closes_the_position_as_a_stop() -> None:
    runtime = unit_runtime(open_plans=(OpenPlan(open_plan(), disposition_pending=False),))
    cached_position(runtime)
    stop = cached_protection(runtime, leg="stop", trigger=Decimal(9_800))
    runtime.strategy.on_order_rejected(_rejected(stop, "code=-2021, msg=Order would immediately trigger."))
    [(position, tags)] = runtime.strategy.closed
    assert position.instrument_id == INSTRUMENT.id and tags == ["stop_filled"]


def test_flatten_pauses_entries_and_closes_every_position_this_strategy_holds() -> None:
    flatten = operator_intent(action="flatten", scope="account")
    runtime = unit_runtime(commands=(flatten,), open_plans=(OpenPlan(open_plan(), disposition_pending=False),))
    cached_position(runtime)
    runtime.pump()
    [(position, tags)] = runtime.strategy.closed
    assert tags == ["operator_flatten"] and position.instrument_id == INSTRUMENT.id
    assert runtime.strategy.control_state().entries_paused
    assert runtime.dispositions() == [
        {
            "action": "flatten",
            "disposition": "accepted",
            "reason": "flatten_submitted",
            "positions": "1",
            "unowned_positions": "0",
        }
    ]


def test_pause_resume_and_a_sticky_halt_are_three_distinct_verdicts() -> None:
    runtime = unit_runtime(
        commands=(
            operator_intent(command_id="a" * 64, action="pause_entries"),
            operator_intent(command_id="b" * 64, action="resume_entries"),
            operator_intent(command_id="c" * 64, action="emergency_halt", scope="account"),
            operator_intent(command_id="d" * 64, action="resume_entries"),
        )
    )
    runtime.pump()
    assert [(value["action"], value["disposition"], value["reason"]) for value in runtime.dispositions()] == [
        ("pause_entries", "accepted", "entries_paused"),
        ("resume_entries", "accepted", "entries_resumed"),
        ("emergency_halt", "accepted", "emergency_halted"),
        ("resume_entries", "rejected", "emergency_halt_sticky"),
    ]
    assert runtime.strategy.control_state() == RuntimeControlSnapshot(entries_paused=True, emergency_halted=True)


def test_a_failing_step_never_escapes_the_pump_and_the_next_input_still_runs() -> None:
    runtime = unit_runtime(signals=(trade_signal(),))
    calls: list[int] = []

    def broken(_now_ns: int) -> None:
        calls.append(1)
        raise ConnectionError("tls close_notify EOF")

    runtime.strategy._converge = broken  # type: ignore[method-assign]
    runtime.pump()
    runtime.advance(6 * SECOND_NS)
    runtime.pump()
    assert calls == [1, 1]
    assert runtime.settle() is not None


def test_an_entry_the_runtime_cannot_evaluate_is_refused_once_instead_of_retried_forever() -> None:
    runtime = unit_runtime(signals=(trade_signal(),))

    def broken(*_args: Any) -> None:
        raise ArithmeticError("decimal context")

    runtime.strategy._gate = broken  # type: ignore[method-assign]
    runtime.pump()
    runtime.pump()
    assert runtime.dispositions() == [{"disposition": "runtime_error"}]


@pytest.mark.parametrize("missing", ["stop", "take_profit"])
def test_the_view_calls_a_position_missing_either_protective_order_unprotected(missing: str) -> None:
    runtime = unit_runtime(open_plans=(OpenPlan(open_plan(), disposition_pending=False),))
    cached_position(runtime)
    for leg, trigger in (("stop", 9_800), ("take_profit", 10_200)):
        if leg != missing:
            cached_protection(runtime, leg=leg, trigger=Decimal(trigger))
    view = runtime.strategy.runtime_view(NOW_NS)
    assert view.protection_status == "unprotected"
    [position] = view.account_snapshot.positions
    assert position.owned and not position.protected
