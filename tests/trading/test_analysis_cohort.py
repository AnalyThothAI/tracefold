from __future__ import annotations

import pytest

from scripts.trading_analysis_cohort import _split, evaluate


def test_cohort_keeps_root_denominator_and_never_leaks_an_asset_across_time() -> None:
    development_asset = next(f"crypto:DEV{i}" for i in range(100) if _split(f"crypto:DEV{i}", 1, 10) == "development")
    holdout_asset = next(f"crypto:HOLD{i}" for i in range(100) if _split(f"crypto:HOLD{i}", 20, 10) == "holdout")
    cases = [
        {
            "case_id": "a",
            "root_trigger_id": "root-a",
            "asset_id": development_asset,
            "created_at_ms": 1,
            "run_kind": "initial",
            "state": "DONE",
            "decision_action": "WATCH",
            "watch_status": "satisfied",
        },
        {
            "case_id": "a-child",
            "root_trigger_id": "root-a",
            "asset_id": development_asset,
            "created_at_ms": 2,
            "run_kind": "recheck",
            "state": "DONE",
            "decision_action": "NO_TRADE",
        },
        {
            "case_id": "b",
            "root_trigger_id": "root-b",
            "asset_id": holdout_asset,
            "created_at_ms": 20,
            "run_kind": "initial",
            "state": "FAILED",
            "decision_action": None,
        },
    ]
    report = evaluate(cases, expected_roots=2, cutoff_ms=10, invalid_outputs=[], expected_invalid=0)
    assert report["denominator"] == {"root_triggers": 2, "cases": 3}
    assert report["funnel"]["initial_failures"] == 1
    assert report["funnel"]["recheck_cases"] == 1
    assert report["split_roots"] == {"development": 1, "holdout": 1}
    assert report["arms"]["holdout"]["recorded_predict"]["net_evaluable"] == 0
    assert report["arms"]["holdout"]["recorded_predict"]["mean_net_bps"] is None


def test_wrong_historical_denominator_fails_before_metrics() -> None:
    row = {
        "case_id": "a",
        "root_trigger_id": "r",
        "asset_id": "crypto:SOL",
        "created_at_ms": 1,
        "run_kind": "initial",
        "state": "EXCLUDED",
    }
    with pytest.raises(ValueError, match="root_denominator_mismatch"):
        evaluate([row], expected_roots=531, cutoff_ms=10, invalid_outputs=[], expected_invalid=22)
