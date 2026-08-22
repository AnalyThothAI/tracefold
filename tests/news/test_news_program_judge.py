"""#148: semantic equivalence for the metric's free-text retention anchors."""

from __future__ import annotations

import json
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tracefold.news.agents.program_judge import JUDGE_ID, CardEquivalence, CardEquivalenceJudge
from tracefold.news.agents.program_metric import _component, bind_metric, metric_receipt

_ACCEPTED = {
    "headline_zh": "BounceBit Chain 授权漏洞转移 2.865 亿枚 BB，决定永久停止运营",
    "why_zh": "授权漏洞被利用转移 2.865 亿枚 BB 后链方宣布永久停运，BB 代币失去链上支撑，持仓者面临流动性与价值双重损失",
}
# Same facts, same mechanism, different wording — the case that used to score zero.
_REWORDED = {
    "headline_zh": "BounceBit 因授权漏洞被盗 2.865 亿枚 BB 并宣布永久停链",
    "why_zh": "授权漏洞导致 2.865 亿枚 BB 被转出后链方永久停运，BB 失去链上支撑，持有者同时承受流动性枯竭与价值归零",
}
_UNRELATED = {"headline_zh": "美联储维持利率不变", "why_zh": "政策利率不变，短端美债定价的加息预期落空"}


class _ScriptedJudgeLM(dspy.BaseLM):  # type: ignore[misc]
    """Answers the equivalence signature without a provider."""

    def __init__(self, verdict: CardEquivalence | None = None, *, fail: bool = False) -> None:
        super().__init__(model="scripted/judge")
        self._verdict = verdict or CardEquivalence(headline_equivalent=True, why_equivalent=True, facts_preserved=True)
        self._fail = fail
        self.calls = 0

    def __call__(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[str]:
        self.calls += 1
        if self._fail:
            raise RuntimeError("provider unavailable")
        return [json.dumps({"verdict": self._verdict.model_dump(mode="json")})]


def _judge(**kwargs: Any) -> CardEquivalenceJudge:
    return CardEquivalenceJudge(_ScriptedJudgeLM(**kwargs))


def test_identical_cards_need_no_model_call() -> None:
    """`--mode recorded` compares a verdict with itself, so the judge must cost nothing there."""

    lm = _ScriptedJudgeLM()
    judge = CardEquivalenceJudge(lm)
    verdict = judge.equivalence(_ACCEPTED, dict(_ACCEPTED))
    assert verdict.headline_equivalent and verdict.why_equivalent and verdict.facts_preserved
    assert lm.calls == 0 and judge.calls == 0


def test_repeated_pairs_are_asked_once() -> None:
    lm = _ScriptedJudgeLM()
    judge = CardEquivalenceJudge(lm)
    for _ in range(4):
        judge.equivalence(_ACCEPTED, _REWORDED)
    assert lm.calls == 1
    assert judge.stats["cache_entries"] == 1


def test_a_reworded_card_keeps_the_reviewers_pass() -> None:
    """The whole point: 15% of the weight was unreachable because wording differs."""

    dimensions = {"headline_fidelity": "pass", "why_support": "pass", "why_value": "pass"}
    names = ("factual_fidelity", "headline_fidelity", "why_support", "why_value")

    without = _component(dimensions, names, _REWORDED, _ACCEPTED, None, None)
    with_judge = _component(dimensions, names, _REWORDED, _ACCEPTED, None, _judge())
    assert without is not None and with_judge is not None
    assert without[0] == 0.0, "byte equality gives a reworded card nothing"
    assert with_judge[0] == 1.0


def test_an_unrelated_card_does_not_keep_the_pass() -> None:
    judge = _judge(verdict=CardEquivalence(headline_equivalent=False, why_equivalent=False, facts_preserved=False))
    dimensions = {"headline_fidelity": "pass", "why_support": "pass"}
    scored = _component(dimensions, ("headline_fidelity", "why_support"), _UNRELATED, _ACCEPTED, None, judge)
    assert scored is not None and scored[0] == 0.0


def test_enum_dimensions_never_consult_the_judge() -> None:
    """`magnitude` and `direction` are supposed to be exact; a judge there would only add noise."""

    lm = _ScriptedJudgeLM()
    judge = CardEquivalenceJudge(lm)
    production = {**_ACCEPTED, "magnitude": 2, "direction": "bearish"}
    candidate = {**_ACCEPTED, "magnitude": 1, "direction": "bullish"}
    scored = _component(
        {"magnitude": "pass", "direction": "pass"}, ("magnitude", "direction"), candidate, production, None, judge
    )
    assert scored is not None and scored[0] == 0.0
    assert lm.calls == 0


def test_the_judge_never_rescues_a_failed_dimension() -> None:
    """It answers "is this still the same?" — and the reviewer already said the old value was wrong."""

    judge = _judge()
    dimensions = {"why_value": "fail"}
    unchanged = _component(dimensions, ("why_value",), dict(_ACCEPTED), _ACCEPTED, None, judge)
    changed = _component(dimensions, ("why_value",), _REWORDED, _ACCEPTED, None, judge)
    assert unchanged is not None and changed is not None
    assert unchanged[0] == 0.0 and changed[0] == 1.0


def test_timeliness_survives_a_rewrite_without_asking_anyone() -> None:
    """Timeliness is about when the Event arrived. No rewriting can change that."""

    lm = _ScriptedJudgeLM()
    scored = _component({"timeliness": "pass"}, ("timeliness",), _REWORDED, _ACCEPTED, None, CardEquivalenceJudge(lm))
    assert scored is not None and scored[0] == 1.0
    assert lm.calls == 0


def test_a_judge_failure_degrades_to_the_stricter_pre_148_answer() -> None:
    """An unavailable judge must not hand out points it never verified."""

    judge = _judge(fail=True)
    scored = _component({"headline_fidelity": "pass"}, ("headline_fidelity",), _REWORDED, _ACCEPTED, None, judge)
    assert scored is not None and scored[0] == 0.0
    assert judge.failures == 1
    assert judge.stats["cache_entries"] == 0, "a transient failure must not pin this pair for the whole run"


def test_metric_receipt_pins_the_judge_identity() -> None:
    """Two runs judged by different models are not comparable, so the ruler names itself."""

    plain = metric_receipt(bind_metric(None), review_rubric_version="news_review_v3")
    judged = metric_receipt(bind_metric(_judge()), review_rubric_version="news_review_v3")
    assert plain["semantic_judge"] is None
    assert judged["semantic_judge"]["judge_id"] == JUDGE_ID
    assert judged["semantic_judge"]["model"] == "scripted/judge"
    assert judged["semantic_judge"]["instruction_sha256"]
    # Same implementation either way: binding a judge must not fork the scoring code.
    assert plain["implementation"] == judged["implementation"]


def test_bound_metric_still_matches_gepas_two_argument_call() -> None:
    """`dspy.Evaluate` calls `metric(example, prediction)`; GEPA's feedback path passes five."""

    metric = bind_metric(_judge())
    example = dspy.Example(accepted_review={}, production_verdict={}, policy_metric={})
    outcome = metric(example, dspy.Prediction(verdict={}))
    assert outcome.score == 0.0
    with pytest.raises(TypeError):
        metric()  # type: ignore[call-arg]
