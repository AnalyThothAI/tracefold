"""Runtime-owned risk reads Nautilus state and returns values only."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from nautilus_trader.model.enums import OmsType, OrderSide
from nautilus_trader.model.identifiers import ClientId, ClientOrderId, PositionId, VenueOrderId
from nautilus_trader.model.objects import Money
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs

from tests.nautilus_oi_runtime_fixtures import ACCOUNT_ID, NOW_NS, registered_oi_strategy, trade_signal
from tracefold.integrations.nautilus.oi_runtime.risk import (
    DayStartBaseline,
    NautilusRiskFacts,
    OiFuturesRiskPolicy,
    fixed_risk_quantity,
)


def _submitted_order(
    context: object,
    client_order_id: str,
    quantity: str,
    *,
    accepted: bool,
    reduce_only: bool = False,
) -> object:
    strategy = context.strategy  # type: ignore[attr-defined]
    instrument = context.instrument  # type: ignore[attr-defined]
    cache = context.cache  # type: ignore[attr-defined]
    order = strategy.order_factory.market(
        instrument_id=instrument.id,
        order_side=OrderSide.BUY,
        quantity=instrument.make_qty(Decimal(quantity)),
        reduce_only=reduce_only,
        client_order_id=ClientOrderId(client_order_id),
    )
    cache.add_order(order, client_id=ClientId("BINANCE"))
    order.apply(TestEventStubs.order_submitted(order, account_id=ACCOUNT_ID, ts_event=NOW_NS))
    cache.update_order(order)
    if accepted:
        order.apply(
            TestEventStubs.order_accepted(
                order,
                account_id=ACCOUNT_ID,
                venue_order_id=VenueOrderId(f"venue-{client_order_id}"),
                ts_event=NOW_NS,
            )
        )
        cache.update_order(order)
    return order


def test_cache_portfolio_snapshot_aggregates_position_open_and_inflight_risk() -> None:
    context = registered_oi_strategy()
    strategy = context.strategy
    instrument = context.instrument
    cache = context.cache
    position_id = PositionId("BTCUSDT-PERP.BINANCE-OI-RUNTIME")

    entry = _submitted_order(context, "owned-entry", "0.01", accepted=True)
    fill = TestEventStubs.order_filled(
        order=entry,
        instrument=instrument,
        strategy_id=strategy.id,
        account_id=ACCOUNT_ID,
        venue_order_id=VenueOrderId("venue-owned-entry"),
        position_id=position_id,
        last_qty=instrument.make_qty(Decimal("0.01")),
        last_px=instrument.make_price(Decimal("10000")),
        commission=Money(0, instrument.quote_currency),
        ts_event=NOW_NS,
    )
    entry.apply(fill)
    cache.update_order(entry)
    cache.add_position(Position(instrument, fill), OmsType.NETTING)
    open_order = _submitted_order(context, "owned-open", "0.02", accepted=True)
    inflight_order = _submitted_order(context, "owned-inflight", "0.03", accepted=False)
    protective_order = _submitted_order(context, "owned-protection", "0.04", accepted=True, reduce_only=True)

    facts = NautilusRiskFacts.collect(
        cache=cache,
        portfolio=context.portfolio,
        account_id=ACCOUNT_ID,
        strategy_id=strategy.id,
        routes={instrument.id: 200},
        candidate_instrument_id=instrument.id,
        owned_order_ids=frozenset(
            {
                entry.client_order_id,
                open_order.client_order_id,
                inflight_order.client_order_id,
                protective_order.client_order_id,
            }
        ),
        owned_position_ids=frozenset({position_id}),
        account_observed_at_ns=NOW_NS,
        reconciliation_observed_at_ns=NOW_NS,
    )

    mid = Decimal("9999.5")
    assert facts.gross_position_notional_usd == Decimal("0.01") * mid
    assert facts.open_order_notional_usd == Decimal("0.02") * mid
    assert facts.inflight_order_notional_usd == Decimal("0.03") * mid
    assert facts.aggregate_risk_usd == Decimal("0.06") * mid * Decimal("0.02")
    assert facts.current_positions == 1
    assert facts.unexpected_exposure is False


def test_risk_snapshot_uses_oldest_contributing_quote_and_pending_position_slot() -> None:
    context = registered_oi_strategy()
    eth = TestInstrumentProvider.ethusdt_perp_binance()
    context.cache.add_instrument(eth)
    context.cache.add_quote_tick(
        TestDataStubs.quote_tick(
            instrument=eth,
            bid_price=1_999,
            ask_price=2_000,
            ts_event=NOW_NS - 10_000_000_001,
            ts_init=NOW_NS - 10_000_000_001,
        )
    )
    pending = context.strategy.order_factory.market(
        instrument_id=eth.id,
        order_side=OrderSide.BUY,
        quantity=eth.make_qty(Decimal("0.01")),
        client_order_id=ClientOrderId("owned-eth-entry"),
    )
    context.cache.add_order(pending, client_id=ClientId("BINANCE"))
    pending.apply(TestEventStubs.order_submitted(pending, account_id=ACCOUNT_ID, ts_event=NOW_NS))
    context.cache.update_order(pending)

    facts = NautilusRiskFacts.collect(
        cache=context.cache,
        portfolio=context.portfolio,
        account_id=ACCOUNT_ID,
        strategy_id=context.strategy.id,
        routes={context.instrument.id: 200, eth.id: 200},
        candidate_instrument_id=context.instrument.id,
        owned_order_ids=frozenset({pending.client_order_id}),
        owned_position_ids=frozenset(),
        account_observed_at_ns=NOW_NS,
        reconciliation_observed_at_ns=NOW_NS,
    )

    assert facts.market_observed_at_ns == NOW_NS - 10_000_000_001
    assert facts.current_positions == 1
    stale = OiFuturesRiskPolicy(context.profile.risk).evaluate_entry(
        facts=facts,
        baseline=DayStartBaseline("2030-03-17", Decimal("1000"), NOW_NS, "4" * 64),
        now_ns=NOW_NS,
        requested_risk_usd=Decimal("10"),
        requested_leverage=2,
        candidate_is_new_position=True,
    )
    full = OiFuturesRiskPolicy(replace(context.profile.risk, max_positions=1)).evaluate_entry(
        facts=replace(facts, market_observed_at_ns=NOW_NS),
        baseline=DayStartBaseline("2030-03-17", Decimal("1000"), NOW_NS, "4" * 64),
        now_ns=NOW_NS,
        requested_risk_usd=Decimal("10"),
        requested_leverage=2,
        candidate_is_new_position=True,
    )

    assert stale.reason == "market_stale"
    assert full.reason == "position_limit"


def test_exhausted_leverage_capacity_disposes_signal_instead_of_losing_it() -> None:
    signal = trade_signal()
    context = registered_oi_strategy(values=(signal,))
    existing = _submitted_order(context, "owned-capacity", "0.201", accepted=True)
    context.strategy._orders[existing.client_order_id] = ("existing", "entry")
    context.strategy._stop_bps[context.instrument.id] = 100

    context.strategy.on_timer(None)

    written: list[object] = []
    context.audit.flush_once(written.extend)
    dispositions = [value for value in written if value.normalized_kind == "signal_disposition"]
    assert context.strategy.submitted == []
    assert len(dispositions) == 1
    assert dispositions[0].summary["disposition"] == "oi_runtime_sizing_capacity_exhausted"


def test_unowned_native_order_halts_new_risk() -> None:
    context = registered_oi_strategy()
    external = _submitted_order(context, "manual-ui-order", "0.01", accepted=True)

    facts = NautilusRiskFacts.collect(
        cache=context.cache,
        portfolio=context.portfolio,
        account_id=ACCOUNT_ID,
        strategy_id=context.strategy.id,
        routes={context.instrument.id: 200},
        candidate_instrument_id=context.instrument.id,
        owned_order_ids=frozenset(),
        owned_position_ids=frozenset(),
        account_observed_at_ns=NOW_NS,
        reconciliation_observed_at_ns=NOW_NS,
    )
    decision = OiFuturesRiskPolicy(context.profile.risk).evaluate_entry(
        facts=facts,
        baseline=DayStartBaseline("2030-03-17", Decimal("1000"), NOW_NS, "4" * 64),
        now_ns=NOW_NS,
        requested_risk_usd=Decimal("10"),
        requested_leverage=2,
        candidate_is_new_position=True,
    )

    assert external.client_order_id.value == "manual-ui-order"
    assert decision.action == "halt"
    assert decision.reason == "unexpected_exposure"


def test_day_loss_baseline_survives_policy_restart_and_fails_closed() -> None:
    context = registered_oi_strategy()
    baseline = DayStartBaseline("2030-03-17", Decimal("1000"), NOW_NS - 1, "4" * 64)
    facts = NautilusRiskFacts(
        equity_usd=Decimal("949.99"),
        gross_position_notional_usd=Decimal(0),
        open_order_notional_usd=Decimal(0),
        inflight_order_notional_usd=Decimal(0),
        aggregate_risk_usd=Decimal(0),
        current_positions=0,
        market_observed_at_ns=NOW_NS,
        account_observed_at_ns=NOW_NS,
        reconciliation_observed_at_ns=NOW_NS,
        unexpected_exposure=False,
    )

    first = OiFuturesRiskPolicy(context.profile.risk)
    restarted = OiFuturesRiskPolicy(context.profile.risk)

    for policy in (first, restarted):
        decision = policy.evaluate_entry(
            facts=facts,
            baseline=baseline,
            now_ns=NOW_NS,
            requested_risk_usd=Decimal("10"),
            requested_leverage=2,
            candidate_is_new_position=True,
        )
        assert decision.action == "halt"
        assert decision.reason == "daily_loss_limit"


@pytest.mark.parametrize(
    ("updates", "leverage", "reason"),
    [
        ({"current_positions": 3}, 2, "position_limit"),
        ({}, 3, "leverage_limit"),
        ({"market_observed_at_ns": NOW_NS - 10_000_000_001}, 2, "market_stale"),
        ({"account_observed_at_ns": NOW_NS - 10_000_000_001}, 2, "account_stale"),
        (
            {"reconciliation_observed_at_ns": NOW_NS - 10_000_000_001},
            2,
            "reconciliation_stale",
        ),
    ],
)
def test_closed_position_leverage_and_staleness_guards(
    updates: dict[str, object],
    leverage: int,
    reason: str,
) -> None:
    context = registered_oi_strategy()
    base = NautilusRiskFacts(
        equity_usd=Decimal("1000"),
        gross_position_notional_usd=Decimal(0),
        open_order_notional_usd=Decimal(0),
        inflight_order_notional_usd=Decimal(0),
        aggregate_risk_usd=Decimal(0),
        current_positions=0,
        market_observed_at_ns=NOW_NS,
        account_observed_at_ns=NOW_NS,
        reconciliation_observed_at_ns=NOW_NS,
        unexpected_exposure=False,
    )
    facts = replace(base, **updates)

    decision = OiFuturesRiskPolicy(context.profile.risk).evaluate_entry(
        facts=facts,
        baseline=DayStartBaseline("2030-03-17", Decimal("1000"), NOW_NS, "4" * 64),
        now_ns=NOW_NS,
        requested_risk_usd=Decimal("10"),
        requested_leverage=leverage,
        candidate_is_new_position=True,
    )

    assert decision.action in {"deny", "halt"}
    assert decision.reason == reason


@pytest.mark.parametrize(
    "clock_updates",
    [
        {"market_observed_at_ns": NOW_NS + 60_000_000_000},
        {"account_observed_at_ns": NOW_NS + 60_000_000_000},
        {"reconciliation_observed_at_ns": NOW_NS + 60_000_000_000},
    ],
)
def test_future_source_clocks_do_not_gate_entry(clock_updates: dict[str, int]) -> None:
    context = registered_oi_strategy()
    base = NautilusRiskFacts(
        equity_usd=Decimal("1000"),
        gross_position_notional_usd=Decimal(0),
        open_order_notional_usd=Decimal(0),
        inflight_order_notional_usd=Decimal(0),
        aggregate_risk_usd=Decimal(0),
        current_positions=0,
        market_observed_at_ns=NOW_NS,
        account_observed_at_ns=NOW_NS,
        reconciliation_observed_at_ns=NOW_NS,
        unexpected_exposure=False,
    )
    facts = replace(base, **clock_updates)

    decision = OiFuturesRiskPolicy(context.profile.risk).evaluate_entry(
        facts=facts,
        baseline=DayStartBaseline(
            utc_day="2026-08-31",
            equity_usd=Decimal("1000"),
            recorded_at_ns=NOW_NS - 1,
            event_id="a" * 64,
        ),
        now_ns=NOW_NS,
        requested_risk_usd=Decimal("10"),
        requested_leverage=1,
        candidate_is_new_position=True,
    )

    assert decision.action == "allow"


def test_aggregate_risk_reduces_then_denies_without_creating_a_reservation() -> None:
    context = registered_oi_strategy()
    policy = OiFuturesRiskPolicy(context.profile.risk)
    baseline = DayStartBaseline("2030-03-17", Decimal("1000"), NOW_NS, "4" * 64)
    facts = NautilusRiskFacts(
        equity_usd=Decimal("1000"),
        gross_position_notional_usd=Decimal(0),
        open_order_notional_usd=Decimal(0),
        inflight_order_notional_usd=Decimal(0),
        aggregate_risk_usd=Decimal("20"),
        current_positions=0,
        market_observed_at_ns=NOW_NS,
        account_observed_at_ns=NOW_NS,
        reconciliation_observed_at_ns=NOW_NS,
        unexpected_exposure=False,
    )

    reduced = policy.evaluate_entry(
        facts=facts,
        baseline=baseline,
        now_ns=NOW_NS,
        requested_risk_usd=Decimal("10"),
        requested_leverage=2,
        candidate_is_new_position=True,
    )
    denied = policy.evaluate_entry(
        facts=replace(facts, aggregate_risk_usd=Decimal("25")),
        baseline=baseline,
        now_ns=NOW_NS,
        requested_risk_usd=Decimal("10"),
        requested_leverage=2,
        candidate_is_new_position=True,
    )

    assert reduced.action == "reduce"
    assert reduced.allowed_risk_usd == Decimal("5")
    assert denied.action == "deny"
    assert denied.reason == "aggregate_risk_limit"


def test_fixed_risk_quantity_floors_to_increment_without_exceeding_approved_risk() -> None:
    quantity = fixed_risk_quantity(
        price=Decimal("1000"),
        stop_distance_bps=100,
        allowed_risk_usd=Decimal("0.015"),
        equity_usd=Decimal("1000"),
        max_leverage=2,
        existing_notional_usd=Decimal(0),
        size_increment=Decimal("0.001"),
    )

    assert quantity == Decimal("0.001")
