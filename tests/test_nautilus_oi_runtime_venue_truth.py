"""The Strategy's venue-truth invariant: the Cache is compared with the venue, and never trusted alone (#680 PR-3).

Every test registers the production Strategy on a bare Cache with `venue_reads=True`, the live root's
setting, and hands it venue reads the way the root's reader does. The Cache is Binance's `BTCUSDT`
perpetual (`BTCUSDT-PERP.BINANCE`); a plan's position is 0.049 long with a stop at 9,800 and a
take-profit at 10,200.

The rules under test:

* a close none of the Runtime's legs sent (a close by hand, or a fill Nautilus' reconciliation
  invented) keeps the stop, the take-profit and the plan until a venue read confirms the instrument
  flat, and is unexpected exposure until then; the Runtime's own stop, take-profit, time exit and
  operator flatten still cancel what is left at once;
* a failed venue read is unknown: nothing is canceled or closed on it, and entries wait for a fresh
  read that agrees with the Cache;
* a disagreement between venue and Cache is a suspect on the first read and unexpected exposure on the
  second; `/readyz` counts what only the venue holds;
* `/flatten account` closes what only the venue holds with a reduce-only market order.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model.enums import OmsType, OrderSide, OrderType
from nautilus_trader.model.identifiers import ClientOrderId, PositionId
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.stubs.events import TestEventStubs

from tests.nautilus_oi_runtime_fixtures import (
    ACCOUNT_ID,
    INSTRUMENT,
    NOW_NS,
    SECOND_NS,
    UnitRuntime,
    cached_position,
    cached_protection,
    close_cached_position,
    open_plan,
    operator_intent,
    trade_signal,
    unit_runtime,
)
from tracefold.integrations.nautilus.oi_runtime.strategy import OpenPlan
from tracefold.integrations.nautilus.oi_runtime.venue import VENUE_STALE_AFTER_NS, VenueReading

SYMBOL = "BTCUSDT"
HELD = {SYMBOL: "0.049"}
FLAT: dict[str, str] = {}


def _protected_plan(**kwargs: Any) -> tuple[UnitRuntime, Any, Any, Any]:
    """A plan holding its position with both protective orders resting, and one agreeing venue read."""

    runtime = unit_runtime(venue_reads=True, open_plans=(OpenPlan(open_plan(), disposition_pending=False),), **kwargs)
    position = cached_position(runtime)
    stop = cached_protection(runtime, leg="stop", trigger=Decimal(9_800))
    take_profit = cached_protection(runtime, leg="take_profit", trigger=Decimal(10_200))
    runtime.venue(HELD)
    assert runtime.strategy.canceled_all == [] and runtime.strategy.submitted == []
    return runtime, position, stop, take_profit


def _unexpected(runtime: UnitRuntime) -> list[str]:
    risks = runtime.observations("risk")
    return [] if not risks else sorted(filter(None, str(risks[-1].summary["exposure"]).split(",")))


def _synthetic_close(runtime: UnitRuntime) -> Any:
    """A plain market SELL nobody tagged: what Nautilus submits for a fill its reconciliation invented."""

    return runtime.strategy.order_factory.market(
        instrument_id=INSTRUMENT.id,
        order_side=OrderSide.SELL,
        quantity=INSTRUMENT.make_qty(Decimal("0.049")),
        client_order_id=ClientOrderId("65688f0e-7a4b-4c1e-9d1e-5e5a690d0001"),
    )


# -- protection is removed only when the venue says flat --------------------------------------------


def test_a_close_no_leg_sent_keeps_protection_and_the_plan_until_the_venue_reads_flat() -> None:
    runtime, position, _stop, _take_profit = _protected_plan()

    close_cached_position(runtime, position, _synthetic_close(runtime), price=Decimal(10_000))
    runtime.pump()

    assert runtime.strategy.canceled_all == [] and runtime.strategy.canceled == []
    assert [plan.status for plan in runtime.plans()] == []  # the plan is neither closed nor rewritten
    [closed] = [value for value in runtime.observations("position") if value.summary["status"] == "closed"]
    assert closed.summary["exit_reason"] == "external"
    assert _unexpected(runtime) == [f"unconfirmed_close:{INSTRUMENT.id.value}"]
    view = runtime.strategy.runtime_view(runtime.clock.timestamp_ns())
    assert (view.entries_armed, view.entry_block_reason, view.unexpected_exposure) == (
        False,
        "unexpected_exposure",
        True,
    )

    # The venue still holds it: a suspect on the first read, unexpected exposure on the second, and the
    # stop and take-profit that are still its only protection stay where they are.
    runtime.venue(HELD)
    assert _unexpected(runtime) == [f"unconfirmed_close:{INSTRUMENT.id.value}"]
    runtime.venue(HELD)
    assert _unexpected(runtime) == [
        f"unconfirmed_close:{INSTRUMENT.id.value}",
        "venue:BTCUSDT:venue=0.049:cache=0",
    ]
    assert runtime.strategy.canceled_all == [] and runtime.strategy.closed == []
    view = runtime.strategy.runtime_view(runtime.clock.timestamp_ns())
    # `/readyz` counts the position only the venue holds, and it is protected.
    assert (view.positions_count, view.protection_status, view.unexpected_exposure) == (1, "unknown", True)

    # Only a venue read that says flat ends the plan and takes the protection off.
    runtime.venue(FLAT)
    assert runtime.strategy.canceled_all[:1] == [INSTRUMENT.id]
    [ended] = runtime.plans()
    assert (ended.status, ended.exit_reason) == ("closed", "external")
    view = runtime.strategy.runtime_view(runtime.clock.timestamp_ns())
    assert (view.unexpected_exposure, view.positions_count, view.entries_armed) == (False, 0, True)


def test_a_failed_venue_read_never_takes_protection_off_a_close_no_leg_sent() -> None:
    runtime, position, _stop, _take_profit = _protected_plan()
    close_cached_position(runtime, position, _synthetic_close(runtime), price=Decimal(10_000))

    for _ in range(10):
        runtime.venue(None)

    assert runtime.strategy.canceled_all == [] and runtime.strategy.canceled == []
    assert runtime.plans() == []
    assert runtime.strategy.runtime_view(runtime.clock.timestamp_ns()).unexpected_exposure


def test_a_read_that_began_before_the_close_does_not_confirm_it() -> None:
    runtime, position, _stop, _take_profit = _protected_plan()
    started_at_ns = runtime.clock.timestamp_ns()
    runtime.advance(SECOND_NS)
    close_cached_position(runtime, position, _synthetic_close(runtime), price=Decimal(10_000))
    runtime.strategy.observe_venue(VenueReading(started_at_ns, runtime.clock.timestamp_ns(), {}))
    runtime.pump()

    assert runtime.strategy.canceled_all == []
    assert runtime.plans() == []


@pytest.mark.parametrize(
    ("leg", "reason"),
    [
        ("stop", "stop_filled"),
        ("take_profit", "take_profit"),
        ("time_exit", "time_exit"),
        ("operator_flatten", "operator_flatten"),
    ],
)
def test_the_runtimes_own_closing_legs_still_cancel_what_is_left_at_once(leg: str, reason: str) -> None:
    runtime, position, stop, take_profit = _protected_plan()
    if leg in {"stop", "take_profit"}:
        closing = stop if leg == "stop" else take_profit
    else:
        runtime.strategy._close_position_with_reason(position, leg)
        [closing] = [order for order in runtime.cache.orders() if order.tags == [leg]]

    close_cached_position(runtime, position, closing, price=Decimal(10_000))

    assert runtime.strategy.canceled_all == [INSTRUMENT.id]
    [ended] = runtime.plans()
    assert (ended.status, ended.exit_reason) == ("closed", reason)


def test_orphan_protection_on_an_instrument_no_plan_holds_waits_for_a_flat_venue_read() -> None:
    runtime = unit_runtime(venue_reads=True)
    stop = cached_protection(runtime, leg="stop", trigger=Decimal(9_800))
    entry = runtime.strategy.order_factory.limit(
        instrument_id=INSTRUMENT.id,
        order_side=OrderSide.BUY,
        quantity=INSTRUMENT.make_qty(Decimal("0.01")),
        price=INSTRUMENT.make_price(9_000),
    )
    runtime.cache.add_order(entry)
    entry.apply(TestEventStubs.order_submitted(entry, account_id=stop.account_id, ts_event=NOW_NS))
    entry.apply(TestEventStubs.order_accepted(entry, account_id=stop.account_id, ts_event=NOW_NS))
    runtime.cache.update_order(entry)

    runtime.pump()
    # An order that could add exposure goes at once; the reduce-only one waits, and is named.
    assert runtime.strategy.canceled == [entry]
    assert runtime.strategy.canceled_all == []
    assert f"order:{stop.client_order_id.value}" in _unexpected(runtime)

    runtime.venue(None)
    assert runtime.strategy.canceled_all == []
    runtime.venue(FLAT)
    assert runtime.strategy.canceled_all == [INSTRUMENT.id]


# -- unknown is never flat, and entries need a venue that agrees ------------------------------------


def test_entries_wait_for_a_fresh_venue_read_and_an_unreadable_venue_blocks_them() -> None:
    runtime = unit_runtime(venue_reads=True, signals=(trade_signal(expires_at_ns=NOW_NS + 400 * SECOND_NS),))
    runtime.pump()
    assert runtime.journal.pending_prepare() is None
    assert runtime.strategy.entry_block_reason(runtime.clock.timestamp_ns()) == "venue_unverified"

    runtime.venue(None)
    assert runtime.journal.pending_prepare() is None

    runtime.venue(FLAT)
    assert runtime.journal.pending_prepare() is not None
    runtime.settle()
    assert runtime.strategy.entry_block_reason(runtime.clock.timestamp_ns()) is None

    # Failed reads leave the last good one in charge until it is two minutes old.
    runtime.venue(None)
    assert runtime.strategy.entry_block_reason(runtime.clock.timestamp_ns()) is None
    runtime.advance(VENUE_STALE_AFTER_NS)
    runtime.venue(None)
    assert runtime.strategy.entry_block_reason(runtime.clock.timestamp_ns()) == "venue_unverified"
    view = runtime.strategy.runtime_view(runtime.clock.timestamp_ns())
    assert (view.entries_armed, view.unexpected_exposure) == (False, False)


def test_a_signal_that_never_sees_the_venue_ends_as_venue_unverified() -> None:
    runtime = unit_runtime(venue_reads=True, signals=(trade_signal(expires_at_ns=NOW_NS + 5 * SECOND_NS),))
    runtime.pump()
    runtime.advance(6 * SECOND_NS)
    runtime.pump()
    assert runtime.dispositions() == [{"disposition": "venue_unverified"}]


# -- venue and Cache disagree -----------------------------------------------------------------------


def test_a_position_only_the_venue_holds_is_unexpected_exposure_on_the_second_read() -> None:
    runtime = unit_runtime(venue_reads=True, signals=(trade_signal(expires_at_ns=NOW_NS + 60 * SECOND_NS),))

    runtime.venue({SYMBOL: "0.5"})
    # One read is a suspect: entries wait, nothing is named yet.
    assert runtime.journal.pending_prepare() is None
    assert runtime.observations("risk") == []
    assert runtime.strategy.entry_block_reason(runtime.clock.timestamp_ns()) == "venue_unverified"

    runtime.venue({SYMBOL: "0.5"})
    assert _unexpected(runtime) == ["venue:BTCUSDT:venue=0.5:cache=0"]
    assert runtime.dispositions() == [{"disposition": "unexpected_exposure"}]
    view = runtime.strategy.runtime_view(runtime.clock.timestamp_ns())
    assert (view.unexpected_exposure, view.positions_count, view.protection_status) == (True, 1, "unknown")
    # Detect-only: nothing was sent to the venue.
    assert runtime.strategy.submitted == [] and runtime.strategy.canceled_all == [] and runtime.strategy.closed == []

    # One agreeing read clears it.
    runtime.venue(FLAT)
    assert _unexpected(runtime) == []
    assert not runtime.strategy.runtime_view(runtime.clock.timestamp_ns()).unexpected_exposure


def test_last_venue_only_position_remains_visible_after_failed_and_expired_reads() -> None:
    runtime = unit_runtime(venue_reads=True)
    runtime.venue({SYMBOL: "0.5"})
    runtime.venue({SYMBOL: "0.5"})
    last_read_ns = runtime.strategy.runtime_view(runtime.clock.timestamp_ns()).venue_read_completed_at_ns

    runtime.venue(None)
    runtime.advance(VENUE_STALE_AFTER_NS)
    view = runtime.strategy.runtime_view(runtime.clock.timestamp_ns())

    assert view.venue_read_completed_at_ns == last_read_ns
    assert view.venue_read_failure == "BinanceClientError:-1021"
    assert view.entry_block_reason == "unexpected_exposure"
    assert not view.entries_armed
    assert view.positions_count == 1
    [position] = view.account_snapshot.positions
    assert (position.source, position.quantity, position.protection_status) == ("venue", "0.5", "unknown")
    assert runtime.strategy.take_recovery_request(runtime.clock.timestamp_ns()) is None


def test_a_cache_position_the_venue_does_not_hold_is_named_and_never_protected_or_exited_again() -> None:
    runtime = unit_runtime(venue_reads=True, open_plans=(OpenPlan(open_plan(), disposition_pending=False),))
    cached_position(runtime)

    runtime.venue(FLAT)
    runtime.venue(FLAT)

    assert _unexpected(runtime) == ["venue:BTCUSDT:venue=0:cache=0.049"]
    # A reduce-only stop, take-profit or time exit for a position the venue does not hold would only be
    # refused, again, every convergence.
    runtime.advance(5 * 3_600 * SECOND_NS)
    runtime.venue(FLAT)
    assert runtime.strategy.submitted == [] and runtime.strategy.closed == []


def test_a_read_that_crossed_a_fill_in_flight_is_not_an_alarm() -> None:
    plan = open_plan(opened_at_ns=None)
    runtime = unit_runtime(venue_reads=True, open_plans=(OpenPlan(plan, disposition_pending=True),))
    entry = runtime.strategy.order_factory.market(
        instrument_id=INSTRUMENT.id,
        order_side=OrderSide.BUY,
        quantity=INSTRUMENT.make_qty(plan.entry_quantity),
        client_order_id=ClientOrderId(plan.entry_client_order_id),
    )
    runtime.cache.add_order(entry)
    entry.apply(TestEventStubs.order_submitted(entry, account_id=ACCOUNT_ID, ts_event=NOW_NS))
    entry.apply(TestEventStubs.order_accepted(entry, account_id=ACCOUNT_ID, ts_event=NOW_NS))
    runtime.cache.update_order(entry)
    runtime.venue(FLAT)
    started_at_ns = runtime.clock.timestamp_ns()

    fill = TestEventStubs.order_filled(
        order=entry,
        instrument=INSTRUMENT,
        strategy_id=runtime.strategy.id,
        account_id=ACCOUNT_ID,
        position_id=PositionId(f"{INSTRUMENT.id}-{runtime.strategy.id}"),
        last_qty=INSTRUMENT.make_qty(plan.entry_quantity),
        last_px=INSTRUMENT.make_price(10_000),
        ts_event=started_at_ns,
    )
    entry.apply(fill)
    runtime.cache.update_order(entry)
    position = Position(INSTRUMENT, fill)
    runtime.cache.add_position(position, OmsType.NETTING)
    runtime.strategy.on_order_filled(fill)
    runtime.strategy.on_position_opened(TestEventStubs.position_opened(position))
    # The read began before the fill arrived and answered after it: it says nothing about BTCUSDT.
    runtime.strategy.observe_venue(VenueReading(started_at_ns, runtime.clock.timestamp_ns() + 1, {}))
    runtime.pump()

    assert runtime.strategy.entry_block_reason(runtime.clock.timestamp_ns()) is None
    assert _unexpected(runtime) == []
    # And the position is protected as usual.
    assert {order.order_type for order, _ in runtime.strategy.submitted} == {
        OrderType.STOP_MARKET,
        OrderType.MARKET_IF_TOUCHED,
    }


# -- /flatten account reaches what only the venue holds ----------------------------------------------


def test_flatten_closes_a_position_only_the_venue_holds_with_a_reduce_only_market_order() -> None:
    flatten = operator_intent(action="flatten", scope="account")
    runtime = unit_runtime(venue_reads=True)
    runtime.venue({SYMBOL: "0.5", "NOTLOADEDUSDT": "-3"})
    runtime.signals.poll_commands_once(lambda *_args: (flatten,))
    runtime.pump()

    [(order, position_id)] = runtime.strategy.submitted
    assert position_id is None
    assert (order.order_type, order.side, order.quantity, order.is_reduce_only, order.tags) == (
        OrderType.MARKET,
        OrderSide.SELL,
        INSTRUMENT.make_qty(Decimal("0.5")),
        True,
        ["operator_flatten"],
    )
    assert runtime.strategy.control_state().entries_paused
    assert runtime.dispositions()[-1] == {
        "action": "flatten",
        "disposition": "accepted",
        "reason": "flatten_submitted",
        "positions": "0",
        "unowned_positions": "0",
        "venue_positions": "read",
        "venue_only_positions": "1",
        "venue_unroutable_positions": "1",
    }


def test_flatten_without_a_fresh_venue_read_closes_the_cache_and_says_the_venue_was_unknown() -> None:
    flatten = operator_intent(action="flatten", scope="account")
    runtime = unit_runtime(
        venue_reads=True, commands=(flatten,), open_plans=(OpenPlan(open_plan(), disposition_pending=False),)
    )
    cached_position(runtime)
    runtime.pump()

    [(_position, tags)] = runtime.strategy.closed
    assert tags == ["operator_flatten"]
    # Unknown is not flat: the position is protected as usual, and nothing is closed on a guess.
    assert {order.order_type for order, _ in runtime.strategy.submitted} == {
        OrderType.STOP_MARKET,
        OrderType.MARKET_IF_TOUCHED,
    }
    assert runtime.dispositions()[-1]["venue_positions"] == "unknown"


def test_native_recovery_keeps_retrying_with_bounded_backoff_in_the_same_generation() -> None:
    runtime, _position, _stop, _take_profit = _protected_plan()
    strategy = runtime.strategy
    runtime.venue({SYMBOL: "0.05"})
    runtime.venue({SYMBOL: "0.05"})
    requested: list[int] = []
    for delay_seconds in (5, 10, 20, 40, 60, 60, 60):
        now_ns = runtime.clock.timestamp_ns()
        at_ns = strategy.take_recovery_request(now_ns)
        assert at_ns is not None
        requested.append(at_ns)
        assert strategy.take_recovery_request(now_ns) is None
        # New identical evidence must not reset the delay; after it expires the
        # fourth and later attempts still run without replacing the generation.
        runtime.venue({SYMBOL: "0.05"})
        retry_ns = now_ns + delay_seconds * SECOND_NS
        assert strategy.take_recovery_request(retry_ns - 1) is None
        runtime.clock.set_time(retry_ns)
        runtime.venue({SYMBOL: "0.05"})
    assert len(set(requested)) == 7

    # A genuine change in the discrepancy is new work, while agreement clears
    # the delay so a later recurrence is recoverable immediately.
    runtime.venue({SYMBOL: "0.06"})
    assert strategy.take_recovery_request(runtime.clock.timestamp_ns()) is not None
    runtime.venue(HELD)
    assert strategy.take_recovery_request(runtime.clock.timestamp_ns()) is None
    runtime.venue({SYMBOL: "0.06"})
    runtime.venue({SYMBOL: "0.06"})
    assert strategy.take_recovery_request(runtime.clock.timestamp_ns()) is not None


def test_recovery_waits_for_fresh_successful_evidence_and_stops_with_its_generation() -> None:
    runtime, _position, _stop, _take_profit = _protected_plan()
    runtime.venue({SYMBOL: "0.05"})
    runtime.venue({SYMBOL: "0.05"})
    runtime.venue(None)
    assert runtime.strategy.take_recovery_request(runtime.clock.timestamp_ns()) is None
    runtime.venue({SYMBOL: "0.05"})
    runtime.advance(VENUE_STALE_AFTER_NS + SECOND_NS)
    assert runtime.strategy.take_recovery_request(runtime.clock.timestamp_ns()) is None
    runtime.venue({SYMBOL: "0.05"})
    runtime.strategy.on_stop()
    assert runtime.strategy.take_recovery_request(runtime.clock.timestamp_ns()) is None


def test_unclaimed_venue_position_never_requests_automatic_recovery() -> None:
    runtime = unit_runtime(venue_reads=True)
    runtime.venue({SYMBOL: "0.5"})
    runtime.venue({SYMBOL: "0.5"})
    assert runtime.strategy.take_recovery_request(runtime.clock.timestamp_ns()) is None
