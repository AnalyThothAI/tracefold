"""Execution decisions must survive replay without dispatching duplicate venue orders."""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

import pytest

from tracefold.trading.executor.core import EntryFacts, PlanFacts, SignalV4, admit, client_order_id, step


def signal() -> SignalV4:
    return SignalV4(
        seq=1,
        signal_id="1" * 64,
        decision_id="2" * 64,
        case_id="case-1",
        account_slot="demo-primary",
        entry_scope_id="3" * 64,
        asset_id="crypto:SOL",
        native_symbol="SOLUSDT",
        mapping_semantics_digest="4" * 64,
        side="long",
        reference_price=Decimal("100"),
        max_drift_bps=200,
        stop_bps=100,
        tp_bps=200,
        max_hold_s=14_400,
        policy_id="forecast",
        policy_version="v1",
        geometry_version="leg_geometry_v1",
        decided_at_ns=1_000_000_000,
        expires_at_ns=301_000_000_000,
    )


def facts() -> EntryFacts:
    return EntryFacts(
        now_ns=2_000_000_000,
        entries_paused=False,
        emergency_halted=False,
        symbol_position=Decimal(0),
        symbol_regular_orders=0,
        symbol_algo_orders=0,
        active_plans=0,
        max_plans=5,
        equity_usdt=Decimal("1000"),
        active_notional_usdt=Decimal(0),
        max_leverage=5,
        risk_fraction=Decimal("0.01"),
        available_margin_usdt=Decimal("1000"),
        pending_margin_usdt=Decimal(0),
        venue_leverage=5,
        margin_mode="CROSSED",
        mark_price=Decimal("100"),
        fee_bps=Decimal("5"),
        price_buffer_bps=Decimal("20"),
        max_spread_fraction_of_stop=Decimal("0.3"),
        live_mid_price=Decimal("100"),
        live_quote_at_ns=1_000_000_000,
        account_at_ns=1_900_000_000,
        account_max_age_ns=5_000_000_000,
        cooldown_until_ns=0,
        multi_assets_mode=False,
        can_trade=True,
        bid=Decimal("99.95"),
        ask=Decimal("100.05"),
        quote_at_ns=1_900_000_000,
        quote_max_age_ns=5_000_000_000,
        market_min_qty=Decimal("0.01"),
        market_max_qty=Decimal("100"),
        market_step=Decimal("0.01"),
        min_notional=Decimal("5"),
    )


def test_client_ids_are_deterministic_bounded_and_distinct_per_leg_attempt() -> None:
    ids = {
        client_order_id(namespace="DEMO:slot", entry_id="a" * 64, leg=leg, attempt=attempt)
        for leg in ("entry", "sl", "tp", "time_exit", "safety_flatten")
        for attempt in range(1, 20)
    }
    assert len(ids) == 95
    assert all(len(value) == 32 and re.fullmatch(r"^[\.A-Z\:/a-z0-9_-]{1,36}$", value) for value in ids)
    assert client_order_id(namespace="DEMO:slot", entry_id="a" * 64, leg="sl", attempt=1) in ids


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"now_ns": 301_000_000_000}, "expired"),
        ({"entries_paused": True}, "entries_paused"),
        ({"hedge_mode": True}, "hedge_mode_unsupported"),
        ({"emergency_halted": True}, "emergency_halt"),
        ({"symbol_position": Decimal("1")}, "symbol_exposure"),
        ({"symbol_algo_orders": 1}, "symbol_exposure"),
        ({"active_plans": 5}, "capacity"),
        ({"bid": Decimal("98")}, "spread"),
        ({"ask": Decimal("105")}, "spread"),
        ({"quote_at_ns": -5_000_000_000}, "quote_stale"),
        ({"active_notional_usdt": Decimal("5000")}, "leverage_capacity"),
        ({"min_notional": Decimal("6000")}, "market_lot_or_notional"),
        ({"available_margin_usdt": Decimal(0)}, "margin_capacity"),
        ({"available_margin_usdt": Decimal("NaN")}, "entry_facts_invalid"),
        ({"live_mid_price": Decimal("NaN")}, "entry_facts_invalid"),
        ({"live_mid_price": Decimal("0")}, "live_quote_stale"),
        ({"live_quote_at_ns": -5_000_000_000}, "live_quote_stale"),
        ({"pending_margin_usdt": None}, "pending_margin_unknown"),
        ({"pending_margin_usdt": Decimal("1000")}, "margin_capacity"),
        ({"account_at_ns": -5_000_000_000}, "account_stale"),
        ({"account_at_ns": 3_000_000_000}, "account_stale"),
        ({"margin_mode": "ISOLATED"}, "margin_mode_unsupported"),
        ({"multi_assets_mode": True}, "multi_assets_mode_unsupported"),
        ({"can_trade": False}, "account_trading_disabled"),
        ({"cooldown_until_ns": 3_000_000_000}, "post_stop_cooldown"),
    ],
)
def test_entry_gate_returns_one_named_disposition(change: dict, reason: str) -> None:
    assert admit(signal(), replace(facts(), **change)).reason == reason


def test_sizing_counts_inflight_notional_and_market_step() -> None:
    result = admit(signal(), replace(facts(), active_notional_usdt=Decimal("4500")))
    assert result.accepted and result.quantity == Decimal("4.99")
    assert result.quantity * facts().ask <= Decimal("500")


def test_live_account_reproduction_is_limited_by_available_margin_and_costs() -> None:
    account = replace(
        facts(),
        equity_usdt=Decimal("3950.11534580"),
        active_notional_usdt=Decimal("3763.35957972"),
        available_margin_usdt=Decimal("183.69098636"),
        max_leverage=1,
        venue_leverage=1,
    )
    result = admit(signal(), account)
    assert result.accepted and result.quantity is not None and result.reserved_margin_usdt is not None
    assert result.reserved_margin_usdt <= account.available_margin_usdt
    assert result.quantity * account.ask < Decimal("183.69098636")
    assert result.quantity * account.ask < Decimal("186.71867")


def test_pending_margin_is_reserved_once_and_actual_leverage_is_used() -> None:
    account = replace(facts(), available_margin_usdt=Decimal("100"), venue_leverage=1)
    first = admit(signal(), account)
    assert first.accepted and first.reserved_margin_usdt is not None
    second = admit(signal(), replace(account, pending_margin_usdt=first.reserved_margin_usdt))
    assert not second.accepted  # The remaining funds cannot support the minimum lot/notional.
    high = admit(signal(), replace(account, venue_leverage=5))
    assert high.quantity is not None and first.quantity is not None and high.quantity > first.quantity


def test_exchange_limit_and_market_max_are_applied_before_lot_rounding() -> None:
    account = replace(facts(), market_step=Decimal("3"), market_max_qty=Decimal("5"), market_min_qty=Decimal("3"))
    assert admit(signal(), account).quantity == 3
    capped = admit(signal(), replace(facts(), venue_max_notional_usdt=Decimal("200")))
    assert capped.quantity is not None and capped.quantity * facts().ask <= 200


def test_spread_configuration_is_consumed() -> None:
    assert admit(signal(), replace(facts(), max_spread_fraction_of_stop=Decimal("0.001"))).reason == "spread"


def plan() -> PlanFacts:
    return PlanFacts(
        now_ns=20_000_000_000,
        entered_at_ns=10_000_000_000,
        max_hold_s=120,
        position_amount=Decimal("1"),
        entry_order_status="FILLED",
        entry_submission_unknown=False,
        sl_status=None,
        tp_status=None,
        sl_attempts=0,
        tp_attempts=0,
        sl_submission_unknown=False,
        tp_submission_unknown=False,
        flatten_status=None,
        exit_fill_client_id=None,
        sl_client_ids=frozenset({"sl-1"}),
        tp_client_ids=frozenset({"tp-1"}),
    )


def test_partial_or_unknown_entry_protects_exposure_then_cancels_entry() -> None:
    assert step(replace(plan(), entry_order_status="PARTIALLY_FILLED")).action == "submit_sl"
    assert step(replace(plan(), entry_submission_unknown=True, sl_status="NEW")).action == "cancel_entry"
    assert step(replace(plan(), entry_submission_unknown=True, position_amount=Decimal(0))).action == "query_entry"
    assert (
        step(replace(plan(), entry_order_status="PARTIALLY_FILLED", position_amount=Decimal(0))).action == "await_entry"
    )
    assert step(plan()).action == "submit_sl"
    assert step(replace(plan(), sl_status="NEW")).action == "submit_tp"
    assert step(replace(plan(), sl_status="NEW", tp_status="NEW")).action == "await_venue"


def test_protection_failures_and_time_exit_converge_to_flatten() -> None:
    assert step(replace(plan(), sl_attempts=3)).reason == "protection_failed"
    assert step(replace(plan(), sl_submission_unknown=True)).action == "query_sl"
    assert step(replace(plan(), now_ns=130_000_000_000)).reason == "time_exit"
    assert step(replace(plan(), flatten_status="unknown")).action == "query_flatten"


def test_venue_flat_terminates_even_when_exit_evidence_is_missing() -> None:
    closed = replace(plan(), position_amount=Decimal(0), sl_status=None, tp_status=None)
    assert step(closed).reason == "external"
    assert step(replace(closed, exit_fill_client_id="sl-1")).reason == "stop_filled"
    assert step(replace(closed, exit_fill_client_id="tp-1")).reason == "take_profit"
    assert step(replace(closed, sl_status="NEW")).action == "cancel_protection"
