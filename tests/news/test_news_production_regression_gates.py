from __future__ import annotations

import pytest

from tracefold.news.learning.metric import (
    ProductionRegressionGateEvidenceV1,
    production_regression_measurements,
)


def _output(*, delivered: bool, asset: str, novelty: str, surprise: str) -> dict[str, object]:
    return {
        "scored_judgment": {"present": True},
        "verdict": {
            "assets": [{"symbol": asset, "role": "primary"}],
            "novelty": novelty,
        },
        "editorial": {
            "relevance": {
                "surprise": surprise,
                "channels": ["regulatory"],
            }
        },
        "delivered": delivered,
    }


def test_production_regression_gates_measure_each_contract_independently() -> None:
    review = {
        "should_push": "must_push",
        "dimensions": {
            "asset_grounding": "pass",
            "trade_surprise": "pass",
            "trade_channels": "fail",
        },
        "novelty": {"judgment": "new_fact"},
        "payload": {"expected": {"trade_channels": ["regulatory"]}},
    }
    stable = _output(delivered=True, asset="BTC", novelty="new_fact", surprise="high")
    candidate = _output(delivered=False, asset="ETH", novelty="restatement", surprise="low")

    measured = production_regression_measurements(review, stable, candidate)

    assert measured["production_action"].candidate_only_regression_n == 1
    assert measured["asset_grounding"].candidate_only_regression_n == 1
    assert measured["novelty"].candidate_only_regression_n == 1
    assert measured["trade_relevance"].denominator_n == 2
    assert measured["trade_relevance"].candidate_only_regression_n == 1


def test_production_regression_gate_evidence_derives_its_own_outcome() -> None:
    with pytest.raises(ValueError, match="news_production_regression_gate_outcome_mismatch"):
        ProductionRegressionGateEvidenceV1(
            gate="novelty",
            metric_sha256="a" * 64,
            denominator_n=1,
            stable_failure_n=0,
            candidate_failure_n=1,
            candidate_only_regression_n=1,
            candidate_only_case_ids=("case-1",),
            outcome="pass",
        )


def test_unassigned_candidate_does_not_manufacture_regression_evidence() -> None:
    measured = production_regression_measurements(
        {"should_push": "must_push"},
        _output(delivered=True, asset="BTC", novelty="new_fact", surprise="high"),
        {"not_assigned": True},
    )

    assert all(item.denominator_n == 0 for item in measured.values())
