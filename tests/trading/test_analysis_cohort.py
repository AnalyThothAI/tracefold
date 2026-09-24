from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.trading_analysis_cohort import PURGE_MS, _split, evaluate
from tracefold.trading.engine.strategy import STRATEGY_VERSION


def _case(case_id: str, root: str, at_ms: int, *, group: str | None = None) -> dict[str, object]:
    return {
        "case_id": case_id,
        "root_trigger_id": root,
        "source_group_id": group or root,
        "asset_id": "crypto:SOL",
        "created_at_ms": at_ms,
        "run_kind": "initial",
        "state": "DONE",
        "decision_action": "NO_TRADE",
    }


def test_time_purge_and_source_group_keep_complete_roots_together() -> None:
    cutoff = 100_000_000
    assert _split(cutoff - PURGE_MS, cutoff) == "development"
    assert _split(cutoff - PURGE_MS + 1, cutoff) == "purged"
    assert _split(cutoff, cutoff) == "holdout"
    cases = [
        _case("a", "root-a", cutoff - PURGE_MS, group="shared"),
        _case("b", "root-b", cutoff, group="shared"),
        _case("c", "root-c", cutoff + 1),
    ]
    cases.append({**_case("child", "root-c", cutoff + 2), "run_kind": "conditional"})
    report = evaluate(cases, expected_roots=3, cutoff_ms=cutoff, invalid_outputs=[], expected_invalid=0)
    assert report["denominator"] == {"root_triggers": 3, "cases": 4}
    assert report["split_roots"] == {"cross_split_excluded": 2, "holdout": 1}
    assert report["funnel"]["conditional_cases"] == 1
    assert report["arms"]["holdout"]["dspy"]["net_evaluable"] == 0
    assert report["arms"]["holdout"]["dspy"]["net_unknown"] == 0


def test_net_requires_contemporary_strategy_receipt_and_applies_capital() -> None:
    cutoff = 100_000_000
    first = _case("a", "root-a", cutoff)
    first["decision_action"] = "TRADE"
    first["arm_evaluations"] = {
        "dspy": {
            "status": "simulated",
            "strategy_version": STRATEGY_VERSION,
            "entry_at_ms": cutoff + 1000,
            "exit_at_ms": cutoff + 2000,
            "net_bps": "100",
            "net_components_bps": {
                "gross": "130",
                "entry_cost": "5",
                "exit_cost": "5",
                "fees": "20",
                "funding_cashflow": "0",
            },
            "entry_quote_ref": "entry",
            "exit_quote_ref": "exit",
            "mark_path_ref": "marks",
            "funding_ref": "funding",
            "fee_ref": "fees",
            "latency_ms": 100,
            "requested_notional_usdt": "100",
            "stop_bps": "100",
        }
    }
    second = _case("b", "root-b", cutoff + 100)
    second["decision_action"] = "TRADE"
    report = evaluate(
        [first, second],
        expected_roots=2,
        cutoff_ms=cutoff,
        invalid_outputs=[],
        expected_invalid=0,
        initial_equity_usdt=Decimal("1000"),
    )
    summary = report["arms"]["holdout"]["dspy"]
    assert summary["net_evaluable"] == 1
    assert summary["net_unknown"] == 1
    assert summary["ending_equity_usdt"] == "1001"
    assert summary["account_drawdown_usdt"] is None
    first["arm_evaluations"]["dspy"]["equity_marks"] = [
        {"at_ms": cutoff + 1000, "liquidation_net_bps": "-100"},
        {"at_ms": cutoff + 1500, "liquidation_net_bps": "-300"},
    ]
    marked = evaluate(
        [first, second],
        expected_roots=2,
        cutoff_ms=cutoff,
        invalid_outputs=[],
        expected_invalid=0,
        initial_equity_usdt=Decimal("1000"),
    )
    assert marked["arms"]["holdout"]["dspy"]["account_drawdown_usdt"] == "3"


def test_wrong_historical_denominator_fails_before_metrics() -> None:
    row = _case("a", "r", 1)
    with pytest.raises(ValueError, match="root_denominator_mismatch"):
        evaluate([row], expected_roots=531, cutoff_ms=10, invalid_outputs=[], expected_invalid=22)
