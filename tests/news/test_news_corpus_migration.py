"""#300: the carry-forward comparator — typed fields exactly, text through the judge, errors never carry."""

from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.learning.judge import CardEquivalence, CardEquivalenceAssessment
from tracefold.news.learning.migration import (
    assess_replayed_case,
    contaminated_case_ids,
    editorial_diffs,
    verdict_field_diffs,
)

_VERDICT: dict[str, Any] = {
    "event_type": "earnings",
    "magnitude": 2,
    "direction": "bullish",
    "actionable": True,
    "scope": "single_name",
    "assets": [{"symbol": "NVDA", "role": "primary", "market_type": "equity"}],
    "decision": "push",
    "novelty": "new_fact",
    "headline_zh": "英伟达财报超预期",
    "why_zh": "数据中心业务加速。",
}


class _StubJudge:
    def __init__(self, assessment: CardEquivalenceAssessment) -> None:
        self._assessment = assessment
        self.calls = 0

    def equivalence(self, accepted: Any, candidate: Any) -> CardEquivalenceAssessment:
        self.calls += 1
        return self._assessment


def _equivalent() -> CardEquivalenceAssessment:
    return CardEquivalenceAssessment(
        status="answered",
        verdict=CardEquivalence(headline_equivalent=True, why_equivalent=True, facts_preserved=True),
    )


def test_identical_verdicts_have_no_field_diffs() -> None:
    assert verdict_field_diffs(_VERDICT, dict(_VERDICT)) == ()


def test_pipeline_and_semantic_fields_both_gate_the_diff() -> None:
    assert verdict_field_diffs(_VERDICT, {**_VERDICT, "magnitude": 1}) == ("magnitude",)
    # `decision` drives delivery and `novelty` drives restatement policy; neither is a judge concern, so
    # the comparator must catch them itself.
    assert verdict_field_diffs(_VERDICT, {**_VERDICT, "decision": "drop"}) == ("decision",)
    assert verdict_field_diffs(_VERDICT, {**_VERDICT, "novelty": "restatement"}) == ("novelty",)
    # `decide()` reads the pointed-at told entry: the same restatement against a different entry is a
    # different production outcome, and `audience` is reader-visible routing.
    assert verdict_field_diffs({**_VERDICT, "restates": 0}, {**_VERDICT, "restates": 2}) == ("restates",)
    assert verdict_field_diffs({**_VERDICT, "audience": "crypto"}, {**_VERDICT, "audience": "us_equity"}) == (
        "audience",
    )


def test_asset_comparison_ignores_order_but_not_content() -> None:
    two = [
        {"symbol": "NVDA", "role": "primary", "market_type": "equity"},
        {"symbol": "MSFT", "role": "mentioned", "market_type": "equity"},
    ]
    assert verdict_field_diffs({**_VERDICT, "assets": two}, {**_VERDICT, "assets": list(reversed(two))}) == ()
    assert verdict_field_diffs(_VERDICT, {**_VERDICT, "assets": two}) == ("assets",)


def test_typed_divergence_never_consults_the_judge() -> None:
    judge = _StubJudge(_equivalent())
    outcome = assess_replayed_case(_VERDICT, {**_VERDICT, "direction": "bearish"}, judge)  # type: ignore[arg-type]
    assert outcome == {"verdict": "divergent", "field_diffs": ["direction"], "judge_status": "not_consulted"}
    assert judge.calls == 0


def test_judge_unavailability_is_an_error_not_a_carry() -> None:
    judge = _StubJudge(
        CardEquivalenceAssessment(status="unavailable", verdict=None, error_code="metric_judge_unavailable")
    )
    outcome = assess_replayed_case(_VERDICT, dict(_VERDICT), judge)  # type: ignore[arg-type]
    assert outcome["verdict"] == "error"


def test_malformed_verdicts_surface_as_comparator_errors_not_crashes() -> None:
    # run_corpus_migration wraps the comparator per case; the contract here is just that comparison of a
    # malformed historical entry raises (so the wrapper can file one error) rather than mis-comparing.
    with pytest.raises(TypeError):
        verdict_field_diffs({**_VERDICT, "assets": [object()]}, dict(_VERDICT))


def test_text_divergence_is_divergent_and_equivalence_carries() -> None:
    non_equivalent = CardEquivalenceAssessment(
        status="answered",
        verdict=CardEquivalence(headline_equivalent=True, why_equivalent=False, facts_preserved=True),
    )
    assert assess_replayed_case(_VERDICT, dict(_VERDICT), _StubJudge(non_equivalent))["verdict"] == "divergent"  # type: ignore[arg-type]
    assert assess_replayed_case(_VERDICT, dict(_VERDICT), _StubJudge(_equivalent()))["verdict"] == "equivalent"  # type: ignore[arg-type]


def test_editorial_projection_gates_the_carry() -> None:
    recorded = {"editorial_origin": "model", "relevance": {"tradability": "direct", "surprise": "unscheduled"}}
    assert editorial_diffs(recorded, dict(recorded)) == ()
    assert editorial_diffs(recorded, {**recorded, "relevance": {**recorded["relevance"], "tradability": "none"}}) == (
        "relevance",
    )
    assert editorial_diffs(recorded, {"editorial_origin": "degraded_unavailable", "relevance": None}) == (
        "editorial_origin",
        "relevance",
    )


def test_history_contamination_downgrades_only_downstream_citers() -> None:
    # Case A diverged and was delivered by the stale arm; case B's told ledger cites A, case C's does not.
    per_case = [
        {"case_id": "A", "verdict": "divergent"},
        {"case_id": "B", "verdict": "equivalent"},
        {"case_id": "C", "verdict": "equivalent"},
    ]
    contaminated = contaminated_case_ids(
        per_case,
        told_event_ids_by_case={"A": [], "B": ["event-A"], "C": ["event-Z"]},
        delivered_event_ids_by_case={"A": "event-A"},
    )
    assert contaminated == {"B": "event-A"}
    # A diverged case the stale arm never delivered leaves no counterfeit history behind.
    assert (
        contaminated_case_ids(
            per_case,
            told_event_ids_by_case={"A": [], "B": ["event-A"], "C": []},
            delivered_event_ids_by_case={},
        )
        == {}
    )
