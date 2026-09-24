from decimal import Decimal

from tracefold.trading.engine.contracts import ExitPlan
from tracefold.trading.engine.evaluation import evaluate_paper_receipt, evaluate_shadow


def _shadow(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "side": "long",
        "decision_at_ms": 0,
        "scheduled_at_ms": 1_000,
        "decision_quote": {"bid": "99", "ask": "101", "received_at_ms": 0},
        "planned_quote": {"bid": "99", "ask": "101", "received_at_ms": 1_000},
        "mark_rows": ({"event_at_ms": 60_000, "high": "106", "low": "98", "close": "100"},),
        "mark_status": "ok",
        "funding_events": (),
        "funding_coverage_complete": True,
        "exit_plan": ExitPlan(stop_distance_bps=200, take_profit_bps=400, max_holding_seconds=14_400),
        "fee_bps_per_side": Decimal(5),
        "exit_spread_bps": Decimal(10),
        "quote_environment": "live_public",
        "target_environment": "demo",
    }
    values.update(overrides)
    return evaluate_shadow(**values)  # type: ignore[arg-type]


def test_shadow_uses_planned_ask_and_stop_first_if_both_touched() -> None:
    result = _shadow()
    assert result["status"] == "simulated"
    assert result["entry_price"] == "101"
    assert result["exit_reason"] == "stop"
    assert result["paper_comparable"] is False
    assert Decimal(str(result["net_bps"])) < -200
    assert result["assumptions"]["partial_fill"] == "not_simulated"


def test_shadow_missing_cost_or_quote_is_not_zero_pnl() -> None:
    assert _shadow(fee_bps_per_side=None)["reason"] == "cost_assumption_missing"
    assert _shadow(planned_quote=None)["reason"] == "executable_quote_missing"
    assert _shadow(mark_status="partial")["reason"] == "mark_path_incomplete"


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
