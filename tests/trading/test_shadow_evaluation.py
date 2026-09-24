from decimal import Decimal

from tracefold.trading.engine.contracts import ExitPlan
from tracefold.trading.engine.evaluation import evaluate_paper_receipt, evaluate_shadow


def _shadow(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "side": "long",
        "decision_at_ms": 0,
        "scheduled_at_ms": 1_000,
        "decision_quote": {"bid": "99", "ask": "101", "bid_quantity": "20", "ask_quantity": "20", "received_at_ms": 0},
        "planned_quote": {
            "bid": "99",
            "ask": "101",
            "bid_quantity": "20",
            "ask_quantity": "20",
            "received_at_ms": 1_000,
        },
        "exit_quotes": (
            {
                "status": "ok",
                "bid": "98",
                "ask": "99",
                "bid_quantity": "20",
                "ask_quantity": "20",
                "received_at_ms": 60_000,
                "environment": "live",
                "quote_ref": "exit-ref",
            },
        ),
        "requested_notional_usdt": Decimal("100"),
        "instrument_rules": {
            "trading_status": "TRADING",
            "contract_type": "PERPETUAL",
            "quote_asset": "USDT",
            "settlement_asset": "USDT",
            "price_tick_size": "0.01",
            "market_min_quantity": "0.01",
            "market_max_quantity": "1000",
            "market_step_size": "0.01",
            "minimum_notional": "5",
        },
        "mark_rows": ({"event_at_ms": 60_000, "high": "106", "low": "98", "close": "100"},),
        "mark_status": "ok",
        "funding_events": (),
        "funding_coverage_complete": True,
        "exit_plan": ExitPlan(stop_distance_bps=200, take_profit_bps=400, max_holding_seconds=14_400),
        "fee_bps_per_side": Decimal(5),
        "quote_environment": "live",
        "target_environment": "live",
    }
    values.update(overrides)
    return evaluate_shadow(**values)  # type: ignore[arg-type]


def test_shadow_uses_planned_ask_and_stop_first_if_both_touched() -> None:
    result = _shadow()
    assert result["status"] == "simulated"
    assert result["entry_price"] == "101"
    assert result["quantity_base"] == "0.99"
    assert result["simulated_notional_usdt"] == "99.99"
    assert result["exit_reason"] == "stop"
    assert result["paper_comparable"] is False
    assert Decimal(str(result["net_bps"])) < -200
    assert result["assumptions"]["partial_fill"] == "top_book_size_covers_full_research_quantity"
    assert result["exit_quote_ref"] == "exit-ref"
    assert result["exit_at_ms"] == result["exit_quote_at_ms"]
    assert Decimal(str(result["net_bps"])) == (
        Decimal(str(result["net_components_bps"]["gross"]))
        - Decimal(str(result["net_components_bps"]["fees"]))
        + Decimal(str(result["net_components_bps"]["funding_cashflow"]))
    )


def test_shadow_missing_cost_or_quote_is_not_zero_pnl() -> None:
    assert _shadow(target_environment="demo")["reason"] == "environment_mismatch"
    assert _shadow(fee_bps_per_side=None)["reason"] == "cost_assumption_missing"
    assert _shadow(planned_quote=None)["reason"] == "executable_quote_missing"
    assert _shadow(mark_status="partial")["reason"] == "mark_path_incomplete"
    assert _shadow(exit_quotes=())["reason"] == "exit_quote_missing"
    assert _shadow(instrument_rules=None)["reason"] == "instrument_rules_missing"


def test_shadow_respects_market_quantity_and_price_filters() -> None:
    rules = {
        "trading_status": "TRADING",
        "contract_type": "PERPETUAL",
        "quote_asset": "USDT",
        "settlement_asset": "USDT",
        "price_tick_size": "0.01",
        "market_min_quantity": "1",
        "market_max_quantity": "1000",
        "market_step_size": "1",
        "minimum_notional": "5",
    }
    assert _shadow(instrument_rules=rules)["reason"] == "market_quantity_filter_rejects_research_size"
    assert _shadow(instrument_rules={**rules, "trading_status": "BREAK"})["reason"] == "instrument_rules_invalid"
    assert _shadow(
        instrument_rules={**rules, "market_min_quantity": "0.01", "market_step_size": "0.01", "price_tick_size": "1000"}
    )["reason"] == ("protection_price_filter_rejects_levels")


def test_shadow_requires_top_book_capacity_at_entry_and_exit() -> None:
    planned = {"bid": "99", "ask": "101", "bid_quantity": "20", "ask_quantity": "0.5", "received_at_ms": 1_000}
    assert _shadow(planned_quote=planned)["reason"] == "entry_top_size_insufficient"
    exit_quote = {
        "status": "ok",
        "bid": "98",
        "ask": "99",
        "bid_quantity": "0.5",
        "ask_quantity": "20",
        "received_at_ms": 60_000,
        "environment": "live",
        "quote_ref": "exit-ref",
    }
    assert _shadow(exit_quotes=(exit_quote,))["reason"] == "exit_top_size_insufficient_or_invalid"


def test_shadow_does_not_use_a_future_or_wrong_environment_exit_quote() -> None:
    late = {
        "status": "ok",
        "bid": "98",
        "ask": "99",
        "bid_quantity": "20",
        "ask_quantity": "20",
        "received_at_ms": 160_001,
        "environment": "live",
        "quote_ref": "exit-ref",
    }
    assert _shadow(exit_quotes=(late,))["reason"] == "exit_quote_missing"
    assert (
        _shadow(exit_quotes=({**late, "received_at_ms": 60_000, "environment": "demo"},))["reason"]
        == "exit_quote_provenance_invalid"
    )


def test_entry_minute_favorable_extreme_cannot_claim_a_take_profit() -> None:
    result = _shadow(mark_rows=({"event_at_ms": 60_000, "high": "110", "low": "100", "close": "102"},))
    assert result["status"] == "unevaluable"
    assert result["reason"] == "mark_endpoint_missing"


def test_paper_net_needs_venue_reconciliation_protection_and_full_fills() -> None:
    entry = ({"quantity": "2", "price": "100", "fee_usd": "0.1"},)
    exit = ({"quantity": "2", "price": "110", "fee_usd": "0.1"},)
    result = evaluate_paper_receipt(
        venue_reconciled=True,
        protection_confirmed=True,
        entry_fills=entry,
        exit_fills=exit,
        funding_usd=Decimal("-0.5"),
        side="long",
    )
    assert result["status"] == "paper_venue_net"
    assert result["net_usd"] == "19.3"
    missing = evaluate_paper_receipt(
        venue_reconciled=True,
        protection_confirmed=True,
        entry_fills=entry,
        exit_fills=(),
        funding_usd=Decimal(0),
        side="long",
    )
    assert missing["status"] == "unevaluable"
