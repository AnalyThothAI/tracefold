"""#148: semantic equivalence for the metric's free-text retention anchors."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from tracefold.news.learning.contracts import (
    METRIC_JUDGE_MAX_TOKENS,
    METRIC_JUDGE_TIMEOUT_SECONDS,
    ModelExecutionIdentity,
)
from tracefold.news.learning.judge import (
    JUDGE_ID,
    CardEquivalence,
    CardEquivalenceAssessment,
    CardEquivalenceJudge,
    FactualEvidenceSupport,
    MetricJudgeEndpoint,
)
from tracefold.news.learning.metric import (
    CandidatePrediction,
    CompileExample,
    _component,
    bind_metric,
    metric_receipt,
)
from tracefold.news.program.transport import ProviderCallMetrics

_ACCEPTED = {
    "headline_zh": "BounceBit Chain 授权漏洞转移 2.865 亿枚 BB，决定永久停止运营",
    "why_zh": (
        "授权漏洞被利用转移 2.865 亿枚 BB 后链方宣布永久停运，BB 代币失去链上支撑，持仓者面临流动性与价值双重损失"
    ),
}
# Same facts, same mechanism, different wording — the case that used to score zero.
_REWORDED = {
    "headline_zh": "BounceBit 因授权漏洞被盗 2.865 亿枚 BB 并宣布永久停链",
    "why_zh": "授权漏洞导致 2.865 亿枚 BB 被转出后链方永久停运，BB 失去链上支撑，持有者同时承受流动性枯竭与价值归零",
}
_UNRELATED = {"headline_zh": "美联储维持利率不变", "why_zh": "政策利率不变，短端美债定价的加息预期落空"}


class _ScriptedJudgeLM(MetricJudgeEndpoint):
    """Answers both judge questions over the real request envelope, without a socket.

    A subclass rather than a duck: since #306 Phase 3 the judge composes its own chat request, so what a
    fixture has to stand in for is the HTTP round trip and nothing above it.
    """

    def __init__(
        self,
        verdict: CardEquivalence | None = None,
        *,
        facts_supported: bool = True,
        fail: bool = False,
    ) -> None:
        super().__init__(model_name="scripted/judge", api_key="k", api_base="https://judge.invalid/v1")
        self._verdict = verdict or CardEquivalence(headline_equivalent=True, why_equivalent=True, facts_preserved=True)
        self._facts_supported = facts_supported
        self._fail = fail
        self.calls = 0
        self.bodies: list[dict[str, Any]] = []

    def ask(self, **kwargs: Any) -> Any:
        # Composed through the production path, so a rendering defect fails here rather than in production.
        self.bodies.append(
            self.request_body(
                instruction=kwargs["instruction"],
                field_order=kwargs["field_order"],
                values=kwargs["values"],
                output_model=kwargs["output_model"],
            )
        )
        self.calls += 1
        if self._fail:
            raise RuntimeError("provider unavailable")
        return self._answer(kwargs["output_model"])

    def _answer(self, output_model: Any) -> Any:
        metrics = ProviderCallMetrics(response_model=self.model, total_tokens=20, finish_reason="stop")
        if output_model is FactualEvidenceSupport:
            return FactualEvidenceSupport(supported_by_evidence=self._facts_supported), metrics
        return self._verdict, metrics


class _BlockingJudgeLM(_ScriptedJudgeLM):
    """Holds admitted provider calls so concurrent judge callers overlap deterministically."""

    def __init__(self) -> None:
        super().__init__()
        self.first_call_entered = threading.Event()
        self.second_call_entered = threading.Event()
        self.release = threading.Event()
        self._calls_lock = threading.Lock()

    def ask(self, **kwargs: Any) -> Any:
        with self._calls_lock:
            self.calls += 1
            if self.calls == 1:
                self.first_call_entered.set()
            elif self.calls == 2:
                self.second_call_entered.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("test did not release provider call")
        return self._answer(kwargs["output_model"])


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


@pytest.mark.parametrize("route", ["equivalence", "facts_supported"])
def test_concurrent_same_key_misses_share_one_provider_call(route: str) -> None:
    lm = _BlockingJudgeLM()
    judge = CardEquivalenceJudge(lm, max_model_calls=1)
    callers_ready = threading.Barrier(2)

    def invoke() -> CardEquivalenceAssessment | bool:
        callers_ready.wait(timeout=1)
        if route == "equivalence":
            return judge.equivalence(_ACCEPTED, _REWORDED)
        return judge.facts_supported('{"leader_title":"issuer filed no update"}', _REWORDED)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke) for _ in range(2)]
        assert lm.first_call_entered.wait(timeout=1)
        duplicate_reached_provider = lm.second_call_entered.wait(timeout=0.2)
        lm.release.set()
        results = [future.result(timeout=1) for future in futures]

    assert duplicate_reached_provider is False
    assert lm.calls == 1
    assert results[0] == results[1]
    assert judge.stats == {
        "attempts": 1,
        "model_calls": 1,
        "cache_entries": 1,
        "failures": 0,
        "actual_cost_microusd": 0,
    }


def test_concurrent_different_keys_cannot_overrun_model_call_budget() -> None:
    lm = _BlockingJudgeLM()
    judge = CardEquivalenceJudge(lm, max_model_calls=1)
    callers_ready = threading.Barrier(2)
    candidates = (_REWORDED, _UNRELATED)

    def invoke(candidate: dict[str, str]) -> CardEquivalenceAssessment:
        callers_ready.wait(timeout=1)
        return judge.equivalence(_ACCEPTED, candidate)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, candidate) for candidate in candidates]
        assert lm.first_call_entered.wait(timeout=1)
        over_budget_call_reached_provider = lm.second_call_entered.wait(timeout=0.2)
        lm.release.set()
        results = [future.result(timeout=1) for future in futures]

    assert over_budget_call_reached_provider is False
    assert lm.calls == 1
    assert sorted(result.status for result in results) == ["answered", "unavailable"]
    assert judge.stats == {
        "attempts": 2,
        "model_calls": 1,
        "cache_entries": 1,
        "failures": 1,
        "actual_cost_microusd": 0,
    }


def test_a_reworded_card_keeps_the_reviewers_pass() -> None:
    """The whole point: 15% of the weight was unreachable because wording differs."""

    dimensions = {"headline_fidelity": "pass", "why_support": "pass", "why_value": "pass"}
    names = ("factual_fidelity", "headline_fidelity", "why_support", "why_value")

    without = _component(dimensions, names, _REWORDED, _ACCEPTED, None, None)
    with_judge = _component(dimensions, names, _REWORDED, _ACCEPTED, None, _judge())
    assert without is not None and with_judge is not None
    assert without[0] == 0.0, "byte equality gives a reworded card nothing"
    assert with_judge[0] == 1.0


def test_factual_repair_is_verified_against_immutable_event_evidence() -> None:
    supported_lm = _ScriptedJudgeLM(facts_supported=True)
    contradicted_lm = _ScriptedJudgeLM(facts_supported=False)
    evidence = (
        '<tracefold-untrusted-event-json-v1>{"leader_title":"issuer filed no update"}'
        "</tracefold-untrusted-event-json-v1>"
    )
    candidate = {**_REWORDED, "direction": "bullish", "magnitude": 2}

    supported = CardEquivalenceJudge(supported_lm).facts_supported(evidence, candidate)
    contradicted = CardEquivalenceJudge(contradicted_lm).facts_supported(evidence, candidate)

    assert supported is True and contradicted is False
    assert supported_lm.calls == contradicted_lm.calls == 1


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


def test_failed_free_text_without_exact_gold_is_not_guessed_by_the_judge() -> None:
    """Equivalence cannot invent the correct copy after the reviewer rejected the old value."""

    judge = _judge()
    dimensions = {"why_value": "fail"}
    unchanged = _component(dimensions, ("why_value",), dict(_ACCEPTED), _ACCEPTED, None, judge)
    changed = _component(dimensions, ("why_value",), _REWORDED, _ACCEPTED, None, judge)
    assert unchanged == (None, 0, 0, 1)
    assert changed == (None, 0, 0, 1)
    assert judge.stats["attempts"] == 0


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


def test_a_judge_failure_is_explicitly_unavailable_and_never_cached() -> None:
    judge = _judge(fail=True)

    first = judge.equivalence(_ACCEPTED, _REWORDED)
    second = judge.equivalence(_ACCEPTED, _REWORDED)

    assert isinstance(first, CardEquivalenceAssessment)
    assert first.status == "unavailable"
    assert first.verdict is None
    assert second.status == "unavailable"
    assert judge.stats == {
        "attempts": 2,
        "model_calls": 2,
        "cache_entries": 0,
        "failures": 2,
        "actual_cost_microusd": 0,
    }


def test_judge_rejects_a_role_binding_that_does_not_match_its_own_ceiling() -> None:
    """#306 Phase 3 turned two runtime refusals into a structural absence.

    The judge used to check `cache is False` and `num_retries == 0` on the framework LM it held, because
    either would have made "one verdict, one provider call" untrue. `MetricJudgeEndpoint` composes one
    request and has neither setting, so what is left to refuse is a role binding that attests a different
    ceiling than the judge actually runs under.
    """

    endpoint = _ScriptedJudgeLM()

    # The judge's declared ceiling and the endpoint's own must be the same number, or the identity in the
    # metric receipt attests a contract the requests were not sent under.
    with pytest.raises(ValueError, match="role_binding_mismatch"):
        CardEquivalenceJudge(endpoint, max_tokens=METRIC_JUDGE_MAX_TOKENS - 1)

    endpoint.tracefold_compiler_role_binding = ModelExecutionIdentity.issue(
        role="task",
        model="scripted/judge",
        api_base="https://judge.invalid/v1",
        max_output_tokens=METRIC_JUDGE_MAX_TOKENS,
        timeout_seconds=METRIC_JUDGE_TIMEOUT_SECONDS,
        temperature=0,
        model_kwargs={},
    )
    with pytest.raises(ValueError, match="role_binding_mismatch"):
        CardEquivalenceJudge(endpoint)


def test_metric_receipt_pins_the_judge_identity() -> None:
    """Two runs judged by different models are not comparable, so the ruler names itself."""

    plain = metric_receipt(bind_metric(None), review_rubric_version="news_review_v4")
    judged = metric_receipt(bind_metric(_judge()), review_rubric_version="news_review_v4")
    assert plain["semantic_judge"] is None
    assert judged["semantic_judge"]["judge_id"] == JUDGE_ID
    assert judged["semantic_judge"]["model"] == "scripted/judge"
    assert judged["semantic_judge"]["instruction_sha256"]
    assert judged["semantic_judge"]["factual_evidence_instruction_sha256"]
    assert judged["semantic_judge"]["factual_evidence_signature_sha256"]
    assert judged["semantic_judge"]["factual_evidence_output_schema_sha256"]
    assert judged["semantic_judge"]["implementation_source_sha256"]
    assert judged["semantic_judge"]["adapter"] == {
        "implementation": "tracefold.news.program.transport.chat_request_body",
        "native_function_calling": False,
        "format_fallback": False,
    }
    assert judged["semantic_judge"]["execution"]["max_output_tokens"] == 4_096
    assert judged["semantic_judge"]["execution"]["cache"] is False
    assert judged["semantic_judge"]["execution"]["num_retries"] == 0
    assert judged["semantic_judge"]["success_cache"] is True
    assert judged["semantic_judge"]["failure_cache"] is False
    # Same implementation either way: binding a judge must not fork the scoring code.
    assert plain["implementation"] == judged["implementation"]


def test_bound_metric_scores_one_example_and_one_prediction() -> None:
    """The judge is bound as a keyword, so a caller cannot accidentally score with a different ruler.

    Until #306 Phase 3 this test was about DSPy's two calling conventions: `dspy.Evaluate` passed two
    positional arguments and GEPA's feedback path passed five, and a missing default turned every
    full-valset evaluation into a silent zero. The adapter this repository owns calls it one way.
    """

    metric = bind_metric(_judge())
    example = CompileExample(
        case_id="case-1",
        cluster_id="cluster-1",
        context=_metric_context(),
        accepted_review={},
        production_judgment=None,
        policy_metric={},
        card_evidence_json="",
        source_title="",
        told_count=0,
    )
    outcome = metric(example, CandidatePrediction(verdict={}))
    assert outcome.score == 0.0 and outcome.hard_gate == "schema_invalid"
    with pytest.raises(TypeError):
        metric()  # type: ignore[call-arg]


def _metric_context() -> Any:
    from tracefold.news.program.contracts import TriageContext

    return TriageContext.from_card(
        {"event_id": "e", "leader_title": "t", "opened_at_ms": 1, "storyline_key": "k"},
        watchlist=(),
        told_rows=(),
        now_ms=1,
        queue_lag_ms=0,
    )


def test_no_judge_is_exactly_the_pre_148_rule() -> None:
    """`bind_metric(None)` is what the receipt calls `score_byte_equality` and what every earlier baseline
    was scored with. Relaxing anything on that arm would silently change the number without changing its
    name — `timeliness` in particular must not become a free pass there."""

    for dimension in ("timeliness", "headline_fidelity", "factual_fidelity", "why_value"):
        scored = _component({dimension: "pass"}, (dimension,), _REWORDED, _ACCEPTED, None, None)
        assert scored is not None and scored[0] == 0.0, dimension


def test_timeliness_is_retained_only_once_a_judge_is_present() -> None:
    scored = _component({"timeliness": "pass"}, ("timeliness",), _REWORDED, _ACCEPTED, None, _judge())
    assert scored is not None and scored[0] == 1.0


def test_identical_text_does_not_excuse_a_flipped_direction() -> None:
    """`factual_fidelity` judges the whole card. A candidate can copy both sentences verbatim and still
    contradict the accepted verdict, and a text-only judge would hand it the anchor for free."""

    lm = _ScriptedJudgeLM(verdict=CardEquivalence(headline_equivalent=True, why_equivalent=True, facts_preserved=False))
    judge = CardEquivalenceJudge(lm)
    accepted = {**_ACCEPTED, "direction": "bullish", "magnitude": 2}
    flipped = {**_ACCEPTED, "direction": "bearish", "magnitude": 2}
    scored = _component({"factual_fidelity": "pass"}, ("factual_fidelity",), flipped, accepted, None, judge)
    assert scored is not None and scored[0] == 0.0
    assert lm.calls == 1, "the structured fields differ, so the judge must actually be asked"


def test_the_short_circuit_still_applies_when_everything_matches() -> None:
    lm = _ScriptedJudgeLM()
    judge = CardEquivalenceJudge(lm)
    same = {**_ACCEPTED, "direction": "bullish", "magnitude": 2}
    verdict = judge.equivalence(same, dict(same))
    assert verdict.facts_preserved and lm.calls == 0
