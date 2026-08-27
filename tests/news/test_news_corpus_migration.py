"""#300: the carry-forward comparator — typed fields exactly, text through the judge, errors never carry."""

from __future__ import annotations

from typing import Any

from tracefold.news.learning.judge import CardEquivalence, CardEquivalenceAssessment
from tracefold.news.learning.migration import assess_replayed_case, verdict_field_diffs

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


def test_text_divergence_is_divergent_and_equivalence_carries() -> None:
    non_equivalent = CardEquivalenceAssessment(
        status="answered",
        verdict=CardEquivalence(headline_equivalent=True, why_equivalent=False, facts_preserved=True),
    )
    assert assess_replayed_case(_VERDICT, dict(_VERDICT), _StubJudge(non_equivalent))["verdict"] == "divergent"  # type: ignore[arg-type]
    assert assess_replayed_case(_VERDICT, dict(_VERDICT), _StubJudge(_equivalent()))["verdict"] == "equivalent"  # type: ignore[arg-type]
