"""#148: semantic equivalence for the metric's free-text retention anchors."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import dspy  # type: ignore[import-untyped]
import pytest

from tracefold.news.artifact_identity import canonical_json
from tracefold.news.learning.contracts import (
    METRIC_JUDGE_MAX_TOKENS,
    METRIC_JUDGE_TIMEOUT_SECONDS,
    ModelExecutionIdentity,
)
from tracefold.news.learning.judge import (
    JUDGE_ID,
    JUDGE_MAX_CALLS_PER_QUESTION,
    JUDGE_PROGRAM_SHA256,
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
from tracefold.news.program.lm import AuditedConfiguredLM, RuntimeModelIdentity

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


class _JudgeDelegate(dspy.BaseLM):  # type: ignore[misc]
    """Typed provider double below the real DSPy adapter and audited LM seam."""

    forward_contract = "typed_lm"

    def __init__(
        self,
        verdict: CardEquivalence | None = None,
        *,
        facts_supported: bool = True,
        fail: bool = False,
        steps: list[Any] | None = None,
    ) -> None:
        super().__init__("scripted/judge", cache=False, num_retries=0)
        self._verdict = verdict or CardEquivalence(headline_equivalent=True, why_equivalent=True, facts_preserved=True)
        self._facts_supported = facts_supported
        self._fail = fail
        self._steps = list(steps or [])
        self.calls = 0
        self.requests: list[dspy.LMRequest] = []
        self._calls_lock = threading.Lock()

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        with self._calls_lock:
            self.calls += 1
            call_number = self.calls
            self.requests.append(request)
        self._before_answer(call_number)
        if self._fail:
            raise dspy.LMServerError("provider unavailable", status=503)
        if self._steps:
            step = self._steps.pop(0)
            if isinstance(step, BaseException):
                raise step
            text = step if isinstance(step, str) else canonical_json(step)
        else:
            response_schema = request.config.response_format
            schema_text = (
                canonical_json(cast(Any, response_schema).model_json_schema())
                if isinstance(response_schema, type)
                else ""
            )
            verdict: CardEquivalence | FactualEvidenceSupport
            if "supported_by_evidence" in schema_text:
                verdict = FactualEvidenceSupport(supported_by_evidence=self._facts_supported)
            else:
                verdict = self._verdict
            text = canonical_json({"verdict": verdict.model_dump(mode="json")})
        return dspy.LMResponse.from_text(
            text,
            model=self.model,
            usage={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
        )

    def _before_answer(self, call_number: int) -> None:
        del call_number


class _ScriptedJudgeLM(MetricJudgeEndpoint):
    def __init__(self, *args: Any, steps: list[Any] | None = None, **kwargs: Any) -> None:
        self.delegate = _JudgeDelegate(*args, steps=steps, **kwargs)
        audited = AuditedConfiguredLM(
            self.delegate,
            structured_output="json_schema",
            runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=self.delegate.model),
            predictor="metric_judge",
            route="compile",
            model_binding="metric_judge.primary",
        )
        super().__init__(audited)

    @property
    def calls(self) -> int:
        return self.delegate.calls


class _BlockingJudgeLM(_ScriptedJudgeLM):
    """Holds admitted provider calls so concurrent judge callers overlap deterministically."""

    def __init__(self) -> None:
        super().__init__()
        self.first_call_entered = threading.Event()
        self.second_call_entered = threading.Event()
        self.release = threading.Event()

        def before_answer(call_number: int) -> None:
            if call_number == 1:
                self.first_call_entered.set()
            elif call_number == 2:
                self.second_call_entered.set()
            if not self.release.wait(timeout=2):
                raise RuntimeError("test did not release provider call")

        self.delegate._before_answer = before_answer  # type: ignore[method-assign]


def _judge(**kwargs: Any) -> CardEquivalenceJudge:
    return CardEquivalenceJudge(_ScriptedJudgeLM(**kwargs))


def test_endpoint_has_two_named_native_predictors() -> None:
    endpoint = _ScriptedJudgeLM()

    assert [name for name, _ in endpoint.named_predictors()] == ["equivalence", "factual_evidence"]
    assert endpoint.equivalence.signature.instructions.startswith("You are checking whether a rewritten Chinese")
    assert endpoint.factual_evidence.signature.instructions.startswith(
        "You are checking whether a corrected Chinese news card"
    )
    assert endpoint.identity["program_sha256"] == JUDGE_PROGRAM_SHA256
    assert endpoint.identity["program"]["max_calls_per_question"] == JUDGE_MAX_CALLS_PER_QUESTION == 2


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


def test_json_format_fallback_spends_one_global_admission_per_physical_call() -> None:
    answer = {
        "verdict": {
            "headline_equivalent": True,
            "why_equivalent": True,
            "facts_preserved": True,
        }
    }
    refused_lm = _ScriptedJudgeLM(steps=["not-json", answer])
    refused = CardEquivalenceJudge(refused_lm, max_model_calls=1, require_exact_accounting=True)

    unavailable = refused.equivalence(_ACCEPTED, _REWORDED)

    assert unavailable.status == "unavailable"
    assert refused_lm.calls == 1, "the second JSON attempt must be refused before the provider"
    assert refused.stats == {
        "attempts": 1,
        "model_calls": 1,
        "cache_entries": 0,
        "failures": 1,
        "actual_cost_microusd": 0,
    }

    allowed_lm = _ScriptedJudgeLM(steps=["not-json", answer])
    allowed = CardEquivalenceJudge(allowed_lm, max_model_calls=2, require_exact_accounting=True)

    answered = allowed.equivalence(_ACCEPTED, _REWORDED)

    assert answered.status == "answered"
    assert allowed_lm.calls == 2
    assert allowed.stats["model_calls"] == 2
    assert allowed.stats["cache_entries"] == 1


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
    adapter = judged["semantic_judge"]["adapter"]
    assert adapter["implementation"] == "dspy.JSONAdapter"
    assert adapter["dspy_version"] == "3.3.1"
    assert adapter["program_sha256"] == JUDGE_PROGRAM_SHA256
    assert len(adapter["equivalence_render_sha256"]) == 64
    assert len(adapter["factual_evidence_render_sha256"]) == 64
    assert adapter["native_function_calling"] is False
    assert adapter["format_fallback"] is True
    assert adapter["max_calls_per_question"] == JUDGE_MAX_CALLS_PER_QUESTION == 2
    assert adapter["effective_lm_capability"] == {
        "supported_params": ["response_format"],
        "supports_response_schema": True,
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
        metric()


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
