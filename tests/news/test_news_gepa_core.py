from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.contracts import (
    METRIC_JUDGE_MAX_TOKENS,
    METRIC_JUDGE_TIMEOUT_SECONDS,
    REFLECTION_MAX_TOKENS,
    REFLECTION_TIMEOUT_SECONDS,
    ModelExecutionIdentity,
    OptimizationBudget,
)
from tracefold.news.learning.metric import (
    CandidatePrediction,
    CompileExample,
    MetricOutcome,
    _compile_example,
    _metric_receipt,
    accepted_review_metric,
)
from tracefold.news.learning.objective import DevelopmentEpisode, _honest_split, _retrieval_receipt
from tracefold.news.learning.optimizer import (
    InstructionProposer,
    _BudgetMeter,
    _MeteredPredictorAdapter,
    _MeteredReflectionLM,
    build_reflection_lm,
    build_task_adapter,
    require_model_identity,
    run_gepa,
)
from tracefold.news.learning.optimizer import optimizer_config_receipt as _optimizer_config_receipt
from tracefold.news.models import TRIAGE_POLICY_VERSION, TriageVerdict
from tracefold.news.program.artifact import (
    load_stable_program_artifact,
)
from tracefold.news.program.contracts import (
    EditorialEnvelope,
    ScoredJudgment,
    TradeRelevanceV1,
    TriageContext,
)
from tracefold.news.program.transport import (
    PredictorRequest,
    PredictorResponse,
    PredictorSpec,
    ProviderCallMetrics,
    RuntimeModelIdentity,
)
from tracefold.news.told_context import ToldLedgerEntry


def _frozen_policy_projection() -> dict[str, object]:
    """The exact-policy fields `_production_action` now requires of any policy-scored example.

    The metric no longer falls back to `DEFAULT_POLICY`: an example that cannot prove which policy scored it
    is a different question wearing the same name. Tests carry the defaults explicitly, so a fixture that
    forgets them fails loudly instead of quietly scoring the wrong arm.
    """

    from tracefold.news.artifact_identity import canonical_sha
    from tracefold.news.triage_rules import DEFAULT_POLICY

    values = DEFAULT_POLICY.as_dict()
    return {
        "policy_version": TRIAGE_POLICY_VERSION,
        "policy_values": values,
        "policy_sha256": canonical_sha(values),
    }


def _relevance(**overrides: Any) -> TradeRelevanceV1:
    values: dict[str, Any] = {
        "impact_breadth": "single_instrument",
        "tradability": "direct",
        "surprise": "material_vs_expectation",
        "development_delta": "state_change",
        "channels": ["earnings_cashflow"],
        "affected_markets": ["single_asset"],
        "reader_value": "realtime",
    }
    values.update(overrides)
    return TradeRelevanceV1.model_validate(values)


def _metric_verdict(**overrides: Any) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "decision": "push",
        "novelty": "new_fact",
        "restates": -1,
        "event_type": "filing",
        "assets": [{"symbol": "ABC", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 2,
        "actionable": True,
        "confidence": 0.8,
        "audience": "us_equity",
        "headline_zh": "发行人提交重大更新，交付时间表整体推迟一个季度",
        "title_zh": "",
        "why_zh": "时间表发生变化。",
    }
    verdict.update(overrides)
    return verdict


def _judgment(*, relevance: TradeRelevanceV1 | None = None, **verdict: Any) -> ScoredJudgment:
    return ScoredJudgment.issue(
        verdict=TriageVerdict.model_validate(_metric_verdict(**verdict)),
        editorial=EditorialEnvelope.issue(
            editorial_origin="model",
            relevance=relevance or _relevance(),
        ),
    )


class _NoopJudge:
    def __init__(self) -> None:
        self.identity = {"judge_id": "test/noop", "failure_cache": False}
        self.stats = {
            "attempts": 0,
            "model_calls": 0,
            "cache_entries": 0,
            "failures": 0,
            "actual_cost_microusd": 0,
        }

    def retains(self, *_args: Any, **_kwargs: Any) -> bool:
        return False


class _EvidenceJudge(_NoopJudge):
    def __init__(self, *, supported: bool) -> None:
        super().__init__()
        self.supported = supported
        self.evidence: list[str] = []

    def facts_supported(self, evidence_json: str, _candidate: dict[str, Any]) -> bool:
        self.evidence.append(evidence_json)
        return self.supported


class _MeteredTaskAdapter:
    """A fake task route carrying the same stamp `build_task_adapter` puts on a real one.

    `optimize` refuses a route without `tracefold_compiler_endpoint_identity`, before any provider call,
    because the role contract — temperature, output ceiling, deadline — is what an identity attests and
    cannot be inferred from the object it describes. A fake that skips the stamp is not a cheaper fixture,
    it is a route the optimizer would refuse in production.
    """

    def __init__(
        self,
        model: str = "task/model",
        *,
        cost: float = 0.000002,
        api_base: str = "https://compiler.test/v1",
    ) -> None:
        self.model = model
        self.cost = cost
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._runtime = RuntimeModelIdentity.issue(provider="fake", model=model)
        self.tracefold_compiler_endpoint_identity = ModelExecutionIdentity.issue(
            role="task",
            model=model,
            api_base=api_base,
            max_output_tokens=512,
            timeout_seconds=20.0,
            temperature=0.0,
            model_kwargs={},
        )

    def runtime_identity(self, model_binding: str) -> RuntimeModelIdentity:
        del model_binding
        return self._runtime

    async def invoke(self, request: PredictorRequest, spec: PredictorSpec) -> PredictorResponse:
        self.calls.append((spec.name, dict(request.inputs)))
        answer = _semantics() if spec.name == "event_semantics" else _card()
        return PredictorResponse(
            output={spec.output_field: answer},
            provider="fake",
            model=self.model,
            model_sha256=canonical_sha({"provider": "fake", "model": self.model}),
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            provider_cost_microusd=round(self.cost * 1_000_000),
            finish_reason="stop",
            runtime_binding_sha256=request.runtime_binding_sha256,
        )


class _MeteredFakeReflectionLM:
    """The reflection role's fake: one callable, one settled cost, one stamped identity."""

    def __init__(self, model: str = "reflection/model", *, cost: float = 0.000003) -> None:
        self.model = model
        self.prompts: list[Any] = []
        self.last_metrics = ProviderCallMetrics(
            provider_cost_microusd=round(cost * 1_000_000), finish_reason="stop", total_tokens=9
        )
        self.tracefold_compiler_endpoint_identity = ModelExecutionIdentity.issue(
            role="reflection",
            model=model,
            api_base="https://compiler.test/v1",
            max_output_tokens=REFLECTION_MAX_TOKENS,
            timeout_seconds=REFLECTION_TIMEOUT_SECONDS,
            temperature=1.0,
            model_kwargs={},
        )

    def __call__(self, prompt: Any) -> str:
        self.prompts.append(prompt)
        return "```\nA proposed replacement instruction.\n```"


def _semantics(**overrides: Any) -> dict[str, Any]:
    """The EventSemantics shape the fake task route answers with."""

    values: dict[str, Any] = {
        "novelty": "new_fact",
        "restates": -1,
        "event_type": "filing",
        "assets": [{"symbol": "ABC", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 2,
        "confidence": 0.8,
        "audience": "us_equity",
        "relevance": _relevance().model_dump(mode="json"),
    }
    values.update(overrides)
    return values


def _card(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "headline_zh": "发行人提交重大更新，交付时间表整体推迟一个季度",
        "why_zh": "交付推迟一个季度，已签约的下游客户要重排产能",
    }
    values.update(overrides)
    return values


def _spec(predictor: str = "event_semantics") -> PredictorSpec:
    from tracefold.news.program.graph import predictor_spec

    return predictor_spec(load_stable_program_artifact().predictor_state(predictor))


def _fake_request(adapter: Any, spec: PredictorSpec) -> PredictorRequest:
    runtime = adapter.runtime_identity(f"{spec.name}.primary")
    return PredictorRequest(
        program_version="test",
        program_sha256="a" * 64,
        context_sha256="b" * 64,
        predictor=spec.name,
        route="primary",
        attempt=1,
        model_binding=f"{spec.name}.primary",
        runtime_provider=runtime.provider,
        runtime_model=runtime.model,
        runtime_model_sha256=runtime.model_sha256,
        runtime_binding_sha256=runtime.binding_sha256,
        inputs={"evidence_json": "{}"},
    )


class _FakeGepaOptimize:
    """Stands in for `gepa.optimize`, and drives the adapter the way the real engine does.

    It is a class only so the recorded kwargs survive between the call and the assertion; what `run_gepa`
    receives is the `__call__` below, which is exactly the `optimize_fn` seam.
    """

    calls: ClassVar[list[dict[str, Any]]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        adapter = kwargs["adapter"]
        trainset, valset = list(kwargs["trainset"]), list(kwargs["valset"])
        seed = dict(kwargs["seed_candidate"])
        # The whole point of the split: GEPA optimizes on one set and picks the winner on another.
        assert trainset and valset
        assert {example.case_id for example in trainset}.isdisjoint({example.case_id for example in valset})
        assert {example.cluster_id for example in trainset}.isdisjoint({example.cluster_id for example in valset})
        # The card Predictor's evidence is visibly delimited before the optimizer ever sees it: the model is
        # told where untrusted Event bytes begin and end, on the compile path as much as in production.
        for example in trainset + valset:
            assert example.card_evidence_json.startswith("<tracefold-untrusted-event-json-v1>\n")
            assert example.card_evidence_json.endswith("\n</tracefold-untrusted-event-json-v1>")

        batch = adapter.evaluate(trainset[:1], seed, capture_traces=True)
        adapter.make_reflective_dataset(seed, batch, ["event_semantics"])
        adapter.propose_new_texts(seed, {"event_semantics": [{"Feedback": "x"}]}, ["event_semantics"])

        winner = {**seed, "event_semantics": seed["event_semantics"] + "\nCompiler candidate instruction."}
        return SimpleNamespace(
            best_candidate=winner,
            candidates=[seed, winner],
            parents=[[None], [0]],
            val_aggregate_scores=[0.4, 0.7],
            discovery_eval_counts=[1, 2],
            total_metric_calls=2,
            num_full_val_evals=1,
            seed=17,
            best_idx=1,
        )


def _episode_payloads() -> tuple[dict[str, Any], ...]:
    context = TriageContext.from_card(
        {
            "event_id": "event-1",
            "evidence_version": 1,
            "evidence_sha256": "e" * 64,
            "focus_fact_id": "fact-1",
            "leader_title": "Issuer files a material update",
            "leader_description": "The filing changes the expected timetable.",
            "opened_at_ms": 1_800_000_000_000,
            "storyline_key": "asset:ABC",
            "grounded_assets": ["ABC"],
            "asset_class": "equity",
            "admission": "candidate",
        },
        watchlist=(),
        told_rows=(),
        now_ms=1_800_000_000_000,
        queue_lag_ms=0,
    )
    return tuple(
        {
            "case_id": f"case-{cluster}-{name}",
            # A target and a control are different facts. Giving both the same cluster used to hide that the
            # optimizer counted Event members rather than connected facts; Objective Plan v2 deliberately
            # elects only one representative from a real connected cluster.
            "cluster_id": f"cluster-{cluster}-{name}",
            "stratum": "review_failure" if name == "target" else "delivered",
            "context": context,
            "policy_metric": {
                "gate": {
                    "grounded_assets": ["ABC"],
                    "watchlist_symbols": [],
                    "admission": "candidate",
                },
                "storyline": {"title": "Issuer files a material update", "family": "filing"},
                "seen": [],
                "told": [],
                "recorded_decision_result": {
                    "final": final,
                    "rule_baseline": final,
                    "override_rule": None,
                    "throttled_by": None,
                    "watchlist_hits": [],
                    "seen_similarity": None,
                    "seen_against": -1,
                    "seen_scope": "",
                },
                **_frozen_policy_projection(),
            },
            "accepted_review": review,
            "production_judgment": _judgment(**verdict).model_dump(mode="json"),
        }
        # Three target/control pairs so the 70/30 split is possible, and both halves carry every required stratum: a
        # safety/positive case and a safety/negative one. Since #199 each half also has to carry at
        # least one verified Prompt target *and* at least one stable-correct control — GEPA is handed
        # `target + control` only, so a corpus of failures alone no longer splits at all.
        for cluster in (1, 2, 3)
        for name, final, review, verdict in (
            (
                "target",
                "push",
                {
                    "should_push": "must_push",
                    "dimensions": {"direction": "fail", "factual_fidelity": "pass"},
                    "novelty": {"judgment": "new_fact", "duplicate_of": ""},
                    # The owner an operator wrote into the submission, not the one ReviewDesk derives
                    # for the queue. Without it this case is an excluded diagnostic.
                    "first_bad_owner_explicit": "triage_prompt",
                    "first_bad_owner": "triage_prompt",
                    "evidence_refs": ["filing#timetable"],
                    "expected": {"direction": "bullish"},
                    "expected_correction": "The direction must follow the filing's actual mechanism.",
                },
                {"direction": "neutral"},
            ),
            (
                "control",
                "drop",
                {
                    "should_push": "must_hold",
                    "dimensions": {"direction": "pass", "factual_fidelity": "pass"},
                    "novelty": {"judgment": "new_fact", "duplicate_of": ""},
                    "evidence_refs": [],
                    "expected": {},
                    "expected_correction": "",
                },
                {},
            ),
        )
    )


def _episodes() -> tuple[DevelopmentEpisode, ...]:
    return tuple(DevelopmentEpisode.model_validate(payload) for payload in _episode_payloads())


def _budget(**overrides: Any) -> OptimizationBudget:
    values: dict[str, Any] = {
        "max_metric_calls": 3,
        "max_task_model_calls": 4,
        "max_reflection_model_calls": 4,
        "max_metric_judge_model_calls": 16,
        "max_cost_microusd": 20,
        "max_call_cost_microusd": 5,
        "max_wall_clock_seconds": 900.0,
        "seed": 17,
    }
    values.update(overrides)
    return OptimizationBudget(**values)


def _run(*, optimize_fn: Any = None, judge: Any = None) -> Any:
    """One bounded GEPA run over the corpus above, through the shared core.

    The container, the proxy and `ProgramCompiler` are gone (#202 PR-C); what is left is the core itself,
    which is what these tests were ever about. The metered path is exercised through `optimize()` in
    `tests/news/test_news_learning_optimize.py`.
    """

    meter = _BudgetMeter(_budget(), imputed_call_cost_microusd=5)
    return run_gepa(
        base_program=load_stable_program_artifact(),
        episodes=_episodes(),
        task_adapter=_MeteredPredictorAdapter(_MeteredTaskAdapter(), meter=meter),
        reflection_lm=_MeteredReflectionLM(_MeteredFakeReflectionLM(), meter=meter),
        judge=judge or _NoopJudge(),
        max_metric_calls=3,
        seed=17,
        review_rubric_version="news_review_v4",
        optimize_fn=optimize_fn or _FakeGepaOptimize(),
    )


def test_the_meter_settles_each_provider_answer_from_the_response_it_returned() -> None:
    inner = _MeteredTaskAdapter(cost=0.000002)
    meter = _BudgetMeter(_budget(), imputed_call_cost_microusd=5)
    adapter = _MeteredPredictorAdapter(inner, meter=meter)
    spec = _spec()
    request = _fake_request(adapter, spec)

    assert asyncio.run(adapter.invoke(request, spec)).output == {"semantics": _semantics()}

    # The provider reported 2 micro-USD for this call; nothing else is consulted for the number.
    assert meter.actual_cost_microusd == 2
    assert meter.task_model_calls == 1


def test_the_meter_charges_a_provider_answer_that_failed_to_parse() -> None:
    """A refused answer is still a paid call, and `provider_observation` is how the transport says so."""

    from tracefold.news.program.transport import PredictorAdapterError, ProviderCallObservation

    class _ParseFailingAdapter(_MeteredTaskAdapter):
        async def invoke(self, request: PredictorRequest, spec: PredictorSpec) -> PredictorResponse:
            raise PredictorAdapterError(
                "news_program_provider_output_not_json",
                output_failure=True,
                provider_observation=ProviderCallObservation(
                    provider="fake",
                    model=self.model,
                    model_sha256=canonical_sha({"provider": "fake", "model": self.model}),
                    latency_ms=3,
                    input_tokens=10,
                    output_tokens=5,
                    cached_tokens=0,
                    total_tokens=15,
                    provider_cost_microusd=2,
                    finish_reason="stop",
                    runtime_binding_sha256=request.runtime_binding_sha256,
                ),
            )

    inner = _ParseFailingAdapter()
    meter = _BudgetMeter(_budget(), imputed_call_cost_microusd=5)
    adapter = _MeteredPredictorAdapter(inner, meter=meter)
    spec = _spec()

    with pytest.raises(PredictorAdapterError, match="output_not_json"):
        asyncio.run(adapter.invoke(_fake_request(adapter, spec), spec))

    assert meter.actual_cost_microusd == 2
    assert meter.task_model_calls == 1


def test_the_core_returns_only_the_typed_two_instruction_write_set() -> None:
    _FakeGepaOptimize.calls.clear()

    result = _run()

    kwargs = _FakeGepaOptimize.calls[-1]
    assert kwargs["max_metric_calls"] == 3
    assert kwargs["seed"] == 17
    assert kwargs["track_best_outputs"] is False
    assert set(kwargs["seed_candidate"]) == {"event_semantics", "reader_card"}
    assert result.patch.parent_program_sha256 == load_stable_program_artifact().program_sha256
    # The whole write-set: one complete instruction per Predictor, carrying exactly what the optimizer
    # wrote. The fake optimizer appends to the seed, so the patch is the seed plus its line — an untouched
    # Predictor keeps the seed rather than the empty string it used to keep.
    stable = load_stable_program_artifact()
    assert result.patch.event_semantics_instruction == (
        stable.event_semantics_instruction + "\nCompiler candidate instruction."
    )
    assert result.patch.reader_card_instruction == stable.reader_card_instruction
    assert result.metric_calls == 2
    assert result.failure_cluster_ids == ("cluster-1-target", "cluster-2-target", "cluster-3-target")
    assert result.target_dimensions == ("direction",)
    receipts = result.model_dump(mode="json")
    assert receipts["optimizer_config"]["optimizer"]["implementation"] == "gepa.optimize"
    assert receipts["optimizer_config"]["optimizer"]["evaluator"].startswith("production NewsSemanticProgram")
    assert "dspy_version" not in receipts["optimizer_config"]["optimizer"]
    assert "source" in receipts["metric"]["implementation"]


def test_non_json_trajectory_value_fails_closed() -> None:
    class _UnsafeOptimize(_FakeGepaOptimize):
        def __call__(self, **kwargs: Any) -> Any:
            run = super().__call__(**kwargs)
            run.parents = [[object()]]
            return run

    with pytest.raises(TypeError, match="non_json_receipt_value"):
        _run(optimize_fn=_UnsafeOptimize())


def test_nonfinite_trajectory_value_fails_closed() -> None:
    class _NonfiniteOptimize(_FakeGepaOptimize):
        def __call__(self, **kwargs: Any) -> Any:
            run = super().__call__(**kwargs)
            run.val_aggregate_scores = [float("nan")]
            return run

    with pytest.raises(TypeError, match="nonfinite_receipt_value"):
        _run(optimize_fn=_NonfiniteOptimize())


def test_a_winner_missing_a_component_is_refused_rather_than_shipped() -> None:
    """gepa returns a `dict[str, str]`; the write-set is exactly two named texts or it is not a candidate."""

    class _PartialOptimize(_FakeGepaOptimize):
        def __call__(self, **kwargs: Any) -> Any:
            run = super().__call__(**kwargs)
            run.best_candidate = {"event_semantics": "only one component"}
            return run

    with pytest.raises(ValueError, match="compile_result_type_invalid"):
        _run(optimize_fn=_PartialOptimize())


def test_an_unstamped_or_misrouted_endpoint_is_refused_before_anything_is_spent() -> None:
    """The role contract — temperature, token ceiling, deadline — is what an identity attests.

    Inferring it from the object it describes would be circular, so an LM without the stamp is refused.
    """

    class _Unstamped(_MeteredTaskAdapter):
        def __init__(self, model: str = "task/model") -> None:
            super().__init__(model)
            del self.tracefold_compiler_endpoint_identity

    with pytest.raises(ValueError, match="endpoint_identity_unavailable"):
        require_model_identity(_Unstamped(), role="task")
    # Right stamp, wrong role: the reflection endpoint cannot answer as the task one.
    with pytest.raises(ValueError, match="endpoint_identity_unavailable"):
        require_model_identity(_MeteredFakeReflectionLM(), role="task")


def test_each_role_is_built_under_its_own_exact_budget() -> None:
    """The reflection role's budget is exact, not a floor: its builder takes no timeout or ceiling at all."""

    reflection = build_reflection_lm(
        model_name="reflection/model",
        api_key="k",
        api_base="https://reflection.test/v1",
    )
    identity = require_model_identity(reflection, role="reflection")

    assert identity.max_output_tokens == REFLECTION_MAX_TOKENS
    assert identity.timeout_seconds == REFLECTION_TIMEOUT_SECONDS
    assert identity.temperature == 1

    task = build_task_adapter(
        model_name="task/model",
        api_key="k",
        api_base="https://task.test/v1",
        timeout=20.0,
        max_tokens=1_200,
    )
    task_identity = require_model_identity(task, role="task")
    assert (task_identity.max_output_tokens, task_identity.timeout_seconds, task_identity.temperature) == (
        1_200,
        20.0,
        0,
    )
    assert (METRIC_JUDGE_MAX_TOKENS, METRIC_JUDGE_TIMEOUT_SECONDS) == (4_096, 120.0)


def test_metric_receipt_hash_binds_the_executed_implementation_source() -> None:
    def changed_metric(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return MetricOutcome(score=0.5, feedback="changed")

    original = _metric_receipt(accepted_review_metric, review_rubric_version="news_review_v4")
    changed = _metric_receipt(changed_metric, review_rubric_version="news_review_v4")

    assert original["metric_id"] == changed["metric_id"]
    assert canonical_sha(original) != canonical_sha(changed)


def test_optimizer_config_receipt_hash_binds_every_scalar_and_both_model_identities() -> None:
    constructor = {
        "max_metric_calls": 3,
        "reflection_minibatch_size": 1,
        "seed": 17,
        "track_stats": True,
    }
    metric_sha = "a" * 64
    base = _optimizer_config_receipt(
        constructor=constructor,
        task_adapter=_MeteredTaskAdapter("task/model"),
        reflection_lm=_MeteredFakeReflectionLM(),
        proposer=InstructionProposer(reflection_lm=_MeteredFakeReflectionLM()),
        metric_sha256=metric_sha,
        example_count=1,
        train_count=1,
        val_count=1,
    )
    changed_scalar = _optimizer_config_receipt(
        constructor={**constructor, "seed": 18},
        task_adapter=_MeteredTaskAdapter("task/model"),
        reflection_lm=_MeteredFakeReflectionLM(),
        proposer=InstructionProposer(reflection_lm=_MeteredFakeReflectionLM()),
        metric_sha256=metric_sha,
        example_count=1,
        train_count=1,
        val_count=1,
    )
    changed_model = _optimizer_config_receipt(
        constructor=constructor,
        task_adapter=_MeteredTaskAdapter("task/other-model"),
        reflection_lm=_MeteredFakeReflectionLM(),
        proposer=InstructionProposer(reflection_lm=_MeteredFakeReflectionLM()),
        metric_sha256=metric_sha,
        example_count=1,
        train_count=1,
        val_count=1,
    )
    changed_endpoint = _optimizer_config_receipt(
        constructor=constructor,
        task_adapter=_MeteredTaskAdapter("task/model", api_base="https://other-compiler.test/v1"),
        reflection_lm=_MeteredFakeReflectionLM(),
        proposer=InstructionProposer(reflection_lm=_MeteredFakeReflectionLM()),
        metric_sha256=metric_sha,
        example_count=1,
        train_count=1,
        val_count=1,
    )

    assert canonical_sha(base) != canonical_sha(changed_scalar)
    assert canonical_sha(base) != canonical_sha(changed_model)
    assert canonical_sha(base) != canonical_sha(changed_endpoint)
    assert "compiler.test" not in repr(base)


# ---------------------------------------------------------------- production-action metric
def _metric_gold(**overrides: Any) -> Any:
    gold: dict[str, Any] = {
        "accepted_review": {
            "should_push": "should_push",
            "dimensions": {},
            "novelty": {"judgment": "uncertain", "duplicate_of": ""},
        },
        "production_judgment": None,
        "policy_metric": {
            "gate": {
                "grounded_assets": ["ABC"],
                "watchlist_symbols": [],
                "admission": "candidate",
            },
            "storyline": {"title": "Issuer files a material update", "family": "filing"},
            "seen": [],
            "told": [],
            **_frozen_policy_projection(),
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(gold.get(key), dict):
            gold[key] = {**gold[key], **value}
        else:
            gold[key] = value
    return CompileExample(
        case_id=str(gold.pop("case_id", "case-1")),
        cluster_id=str(gold.pop("cluster_id", "cluster-1")),
        context=_metric_context(),
        accepted_review=dict(gold["accepted_review"]),
        production_judgment=gold["production_judgment"],
        policy_metric=dict(gold["policy_metric"]),
        card_evidence_json=str(gold.pop("card_evidence_json", "")),
        source_title=str(gold.pop("source_title", "")),
        told_count=0,
    )


def _metric_context() -> TriageContext:
    return TriageContext.from_card(
        {
            "event_id": "event-metric",
            "evidence_version": 1,
            "evidence_sha256": "e" * 64,
            "focus_fact_id": "fact-metric",
            "leader_title": "Issuer files a material update",
            "opened_at_ms": 1_800_000_000_000,
            "storyline_key": "asset:ABC",
            "grounded_assets": ["ABC"],
            "asset_class": "equity",
            "admission": "candidate",
        },
        watchlist=(),
        told_rows=(),
        now_ms=1_800_000_000_000,
        queue_lag_ms=0,
    )


def _score(gold: Any, verdict: dict[str, Any], pred_name: str | None = None) -> Any:
    return accepted_review_metric(
        gold,
        CandidatePrediction(
            verdict=verdict,
            editorial=EditorialEnvelope.issue(editorial_origin="model", relevance=_relevance()),
        ),
        pred_name=pred_name,
    )


def test_metric_scores_the_production_action_not_the_models_intent() -> None:
    """`decision` is an intent `decide()` routinely overrides. A grounded restatement is dropped whatever the
    model asked for, so a metric reading `decision` would reward a card the reader never receives."""

    told = [
        {
            "i": 0,
            "event_id": "prior",
            "dir": "bullish",
            "headline_zh": "发行人提交重大更新，交付时间表整体推迟一个季度",
            "grounded_assets": ["ABC"],
        }
    ]
    gold = _metric_gold(
        accepted_review={"should_push": "should_hold"},
        policy_metric={"told": told, "seen": [dict(told[0], direction="bullish")], **_frozen_policy_projection()},
    )
    # The model asks to push; the policy drops it as a grounded restatement, which is what the reviewer wanted.
    restatement = _metric_verdict(decision="push", novelty="restatement", restates=0)
    assert _score(gold, restatement).score == 1.0

    # The same verdict scored against the old contract — the model's own `decision` — would have failed it.
    assert restatement["decision"] == "push"


def test_metric_sees_the_stale_source_withhold_production_applies() -> None:
    """#154: `decide()` can turn a push into `throttled` on artifact age.

    CLAUDE.md pins that `news learning baseline` runs literally the same metric object as production. If
    `source_age_s` is dropped from the projection the metric scores `push` where the reader got nothing, and the
    optimizer is rewarded for an action production would not have taken.
    """

    pushed = _metric_verdict(decision="push")
    fresh = _metric_gold(policy_metric={"gate": {"source_age_s": 2}})
    stale = _metric_gold(policy_metric={"gate": {"source_age_s": 16 * 3600}})

    assert _score(fresh, pushed).production_action == "push"
    assert _score(stale, pushed).production_action == "throttled"

    # And the reviewer's verdict is scored against that action, not the model's intent: a `should_hold` review
    # is satisfied by the stale withhold and failed by the fresh push.
    held = {"accepted_review": {"should_push": "should_hold"}}
    assert _score(_metric_gold(**held, policy_metric={"gate": {"source_age_s": 16 * 3600}}), pushed).score == 1.0
    assert _score(_metric_gold(**held, policy_metric={"gate": {"source_age_s": 2}}), pushed).score < 1.0


def test_metric_hard_gates_cannot_be_averaged_away_by_retention_anchors() -> None:
    gold = _metric_gold(
        accepted_review={
            "should_push": "must_push",
            "dimensions": {"headline_fidelity": "pass", "why_support": "pass", "direction": "pass"},
        },
        production_judgment=_judgment(),
    )
    # Four accepted dimensions agree, but the reader never gets a fact the reviewer marked `must_push`.
    missed = _metric_verdict(event_type="noise", magnitude=0, actionable=False, decision="drop")
    result = _score(gold, missed)
    assert result.score == 0.0
    assert "must receive" in result.feedback


def test_metric_rejects_an_ungrounded_primary_asset_outright() -> None:
    gold = _metric_gold()
    hallucinated = _metric_verdict(assets=[{"symbol": "XYZ", "role": "primary"}])
    result = _score(gold, hallucinated)
    assert result.score == 0.0 and "XYZ" in result.feedback


def test_factual_failure_must_be_repaired_against_evidence_not_merely_reworded() -> None:
    gold = _metric_gold(
        accepted_review={
            "should_push": "should_push",
            "dimensions": {"factual_fidelity": "fail"},
            "novelty": {"judgment": "uncertain", "duplicate_of": ""},
        },
        production_judgment=_judgment(),
    )
    gold = dataclasses.replace(
        gold, card_evidence_json="<trusted-test-evidence>issuer filed no update</trusted-test-evidence>"
    )
    changed = _metric_verdict(headline_zh="发行人已提交重大更新，交付时间表整体推迟一个季度")
    prediction = CandidatePrediction(
        verdict=changed,
        editorial=EditorialEnvelope.issue(editorial_origin="model", relevance=_relevance()),
    )
    rejecting = _EvidenceJudge(supported=False)
    accepting = _EvidenceJudge(supported=True)

    rejected = accepted_review_metric(gold, prediction, judge=rejecting)
    verified = accepted_review_metric(gold, prediction, judge=accepting)

    assert rejected.hard_gate == "factual_contradiction" and rejected.score == 0.0
    assert rejecting.evidence == [gold.card_evidence_json]
    assert verified.hard_gate == "" and verified.score == 1.0
    assert accepting.evidence == [gold.card_evidence_json]


def test_factual_failure_fails_closed_without_an_evidence_judge() -> None:
    gold = _metric_gold(
        accepted_review={
            "should_push": "should_push",
            "dimensions": {"factual_fidelity": "fail"},
            "novelty": {"judgment": "uncertain", "duplicate_of": ""},
        },
        production_judgment=_judgment(),
    )
    gold = dataclasses.replace(
        gold, card_evidence_json="<trusted-test-evidence>issuer filed no update</trusted-test-evidence>"
    )
    changed = _metric_verdict(headline_zh="任意改写不能证明事实已经修复，仍需对照原始证据核验")

    outcome = _score(gold, changed)

    assert outcome.hard_gate == "factual_contradiction"
    assert "could not be verified" in outcome.feedback


def test_metric_does_not_guess_a_failed_dimension_without_exact_gold() -> None:
    """A rejected value and no replacement value do not reveal the correct answer."""

    labelled = _metric_gold(
        accepted_review={"should_push": "uncertain", "dimensions": {"direction": "fail", "magnitude": "uncertain"}},
        production_judgment=_judgment(direction="neutral"),
    )
    changed = _score(labelled, _metric_verdict(direction="bullish"))
    unchanged = _score(labelled, _metric_verdict(direction="neutral"))
    # Identical, and identically unearned: the reviewer's rejection carries no correct value, so neither
    # answer enters the `semantics_novelty` denominator. The two cards are byte-identical apart from
    # `direction`, so whatever `reader_card_lint` says about them it says about both.
    assert changed.score == unchanged.score
    assert changed.component_scores["semantics_novelty"] is None
    assert changed.component_denominators["semantics_novelty"] == 0
    assert changed.dimension_outcomes[0] == ("direction", "not_scored_no_gold")


def test_metric_feedback_never_asks_a_predictor_to_repair_what_it_cannot_cause() -> None:
    gold = _metric_gold(
        accepted_review={
            "should_push": "should_push",
            "dimensions": {"asset_grounding": "fail", "why_support": "fail"},
            "novelty": {"judgment": "new_fact", "duplicate_of": ""},
        },
        production_judgment=_judgment(),
    )
    semantics = _score(gold, _metric_verdict(), "event_semantics").feedback
    card = _score(gold, _metric_verdict(), "reader_card").feedback
    assert "asset_grounding" in semantics and "why_support" not in semantics
    assert "why_support" in card and "asset_grounding" not in card


def test_reader_card_ignores_claimed_action_ownership_for_an_ordinary_action_mismatch() -> None:
    gold = _metric_gold(
        accepted_review={"should_push": "must_push"},
        policy_metric={"action_feedback_owner": "headline_duplicate"},
    )

    outcome = _score(gold, _metric_verdict(magnitude=1), "reader_card")

    assert outcome.hard_gate == "must_push_miss"
    assert outcome.production_rule == "trade_relevance_inconsistent"
    assert outcome.production_throttled_by == ""
    assert "must receive" not in outcome.feedback
    assert "No ReaderCard-owned correction" in outcome.feedback


def test_reader_card_gets_action_feedback_for_an_exact_seen_headline_duplicate() -> None:
    gold = _metric_gold(
        accepted_review={"should_push": "must_push"},
        policy_metric={
            "seen": [
                {
                    "event_id": "prior",
                    "headline_zh": "发行人提交重大更新，交付时间表整体推迟一个季度",
                    "direction": "bullish",
                    "grounded_assets": ["ABC"],
                    "assets": [{"symbol": "ABC", "role": "primary"}],
                }
            ]
        },
    )

    outcome = _score(gold, _metric_verdict(), "reader_card")

    assert outcome.hard_gate == "must_push_miss"
    assert outcome.production_action == "throttled"
    assert outcome.production_rule == "trade_relevance_realtime"
    assert outcome.production_throttled_by.endswith(":seen")
    assert "must receive" in outcome.feedback


def test_watchlist_objective_guard_action_never_becomes_event_semantics_feedback() -> None:
    gold = _metric_gold(
        accepted_review={
            "should_push": "must_hold",
            "dimensions": {},
            "novelty": {"judgment": "uncertain", "duplicate_of": ""},
        }
    )
    projection = dict(gold.policy_metric)
    projection["gate"] = {**dict(projection["gate"]), "watchlist_symbols": ["ABC"]}
    gold = dataclasses.replace(gold, policy_metric=projection)
    editorial = EditorialEnvelope.issue(
        editorial_origin="model",
        relevance=_relevance(
            reader_value="background",
            tradability="contextual",
            channels=[],
            affected_markets=[],
        ),
    )
    prediction = CandidatePrediction(verdict=_metric_verdict(), editorial=editorial)

    outcome = accepted_review_metric(gold, prediction, pred_name="event_semantics")

    assert outcome.hard_gate == "must_hold_send"
    assert outcome.objective_guard == "watchlist"
    assert "must not receive" not in outcome.feedback
    assert "policy resolved" not in outcome.feedback
    assert "code-owned objective guard" in outcome.feedback


def test_metric_receipt_binds_the_weights_the_policy_and_the_rubric() -> None:
    receipt = _metric_receipt(accepted_review_metric, review_rubric_version="news_review_v4")
    assert receipt["weights"] == {
        "final_action": 0.45,
        "trade_relevance": 0.35,
        "semantics_novelty": 0.10,
        "reader_card": 0.10,
        "reader_card_lint": 0.10,
    }
    assert receipt["action_source"]["policy"] == "tracefold.news.triage_rules.decide"
    assert receipt["action_source"]["operational_controls"].startswith("none_")
    # The receipt no longer carries a policy *value*: the policy that scores an example travels with that
    # example and is verified against its own SHA, so a receipt value could only ever disagree with it.
    assert "per_example" in receipt["action_source"]["policy_values"]
    assert receipt["review_rubric_version"]
    source_units = receipt["implementation"]["source_unit_sha256"]
    assert set(source_units) == {
        "tracefold.news.learning.metric",
        # #199: the corpus vocabulary the metric reads — dimension groups, exact gold, the frozen policy,
        # the production action — lives beside the Objective Plan now. The ruler commits to both files or
        # half of its definition can change without the receipt noticing.
        "tracefold.news.learning.objective",
        # #306 Phase 1: the deterministic card contract is a scored component and a hard gate, so its
        # bytes are part of what "better" means.
        "tracefold.news.learning.card_lint",
        "tracefold.news.models.base_symbol",
        "tracefold.news.events.storyline",
        "tracefold.news.triage_rules",
    }
    assert receipt["implementation"]["helper_source_root_sha256"] == canonical_sha(source_units)
    assert all(len(value) == 64 for value in source_units.values())
    assert "factual_contradiction" in receipt["hard_gates"]
    assert "factual_contradiction_unchanged" not in receipt["hard_gates"]
    # Reweighting, repointing at another policy, or moving rubric all move the receipt hash.
    assert canonical_sha({**receipt, "weights": {"final_action": 1.0}}) != canonical_sha(receipt)


def test_the_metric_scores_editorial_judgment_with_no_operational_input() -> None:
    """A card silenced for operational reasons would not be evidence that its editorial judgment was wrong.

    #137 removed the pause/mute plane, so there is nothing left for `decide()` to exclude — but the sealed
    projection must still carry no control state, or a future control plane could leak into the reward.
    """

    assert "control" not in _metric_gold().policy_metric


# ---------------------------------------------------------------- honest split
def _split_episode(cluster: str, case: str, should_push: str, order: int) -> Any:
    return DevelopmentEpisode.model_validate(
        {
            "case_id": case,
            "cluster_id": cluster,
            "stratum": "review_failure",
            "context": _episodes()[0].context,
            "accepted_review": {
                "should_push": should_push,
                "dimensions": {"factual_fidelity": "pass"},
                "novelty": {"judgment": "new_fact", "duplicate_of": ""},
            },
            "production_judgment": _judgment().model_dump(mode="json"),
            "policy_metric": {"seen": [], "told": [], **_frozen_policy_projection()},
        }
    )


def _balanced(cluster: str, index: int) -> list[Any]:
    return [
        _split_episode(cluster, f"{cluster}-push", "must_push", index),
        _split_episode(cluster, f"{cluster}-hold", "must_hold", index),
    ]


def test_split_is_disjoint_time_ordered_and_uses_one_fact_cluster_representative() -> None:
    episodes = [
        _split_episode("c1", "c1-push", "must_push", 0),
        _split_episode("c2", "c2-hold", "must_hold", 1),
        _split_episode("c3", "c3-push", "must_push", 2),
        _split_episode("c4", "c4-hold", "must_hold", 3),
        _split_episode("c5", "c5-push", "must_push", 4),
        _split_episode("c6", "c6-hold", "must_hold", 5),
    ]
    train, val, receipt = _honest_split(episodes)

    train_clusters = {episode.cluster_id for episode in train}
    val_clusters = {episode.cluster_id for episode in val}
    assert train_clusters.isdisjoint(val_clusters)
    assert {episode.case_id for episode in train}.isdisjoint({episode.case_id for episode in val})
    # Earliest clusters train, latest select. No shuffle, no seed.
    assert train_clusters == {"c1", "c2", "c3", "c4"} and val_clusters == {"c5", "c6"}
    assert receipt["train"]["cluster_n"] == 4 and receipt["development_selection"]["cluster_n"] == 2
    assert receipt["disjointness"]["shared_case_ids"] == 0
    assert len(receipt["train"]["cluster_root_sha256"]) == 64
    # Deterministic: the same episodes in any input order produce the same split.
    assert _honest_split(list(reversed(episodes)))[2] == receipt
    with pytest.raises(ValueError, match="split_requires_one_representative_per_cluster"):
        _honest_split(_balanced("duplicate", 0))


def test_split_fails_closed_when_a_half_cannot_detect_the_regressions_it_exists_for() -> None:
    # Every cluster is a push case, so neither half can catch a must-hold regression.
    episodes = [_split_episode(f"c{i}", f"c{i}-push", "must_push", i) for i in range(4)]
    with pytest.raises(ValueError, match="split_coverage_incomplete"):
        _honest_split(episodes)

    with pytest.raises(ValueError, match="split_requires_two_clusters"):
        _honest_split([_split_episode("only", "only-push", "must_push", 0)])


def test_retrieval_is_reported_separately_so_a_scalar_score_cannot_hide_a_recall_failure() -> None:
    """ "The model called it new" and "the model was never shown the card" are different defects."""

    context = _episodes()[0].context
    shown = context.model_copy(
        update={
            "told": context.told.model_copy(
                update={
                    "entries": (
                        ToldLedgerEntry(
                            i=0, event_id="prior", at_ms=1, ago_min=1, magnitude=1, direction="bullish", headline_zh="x"
                        ),
                    )
                }
            )
        }
    )

    def episode(ctx: Any, target: str) -> Any:
        return DevelopmentEpisode.model_validate(
            {
                "case_id": f"case-{target}-{id(ctx)}",
                "cluster_id": "c1",
                "stratum": "s",
                "context": ctx,
                "accepted_review": {"novelty": {"judgment": "restatement", "duplicate_of": target}},
                "policy_metric": {"seen": [{"event_id": "prior"}], "told": []},
            }
        )

    receipt = _retrieval_receipt([episode(shown, "prior"), episode(context, "prior")])
    assert receipt["accepted_restatements_in_window"] == 2
    assert receipt["target_recall_n"] == 1 and receipt["target_recall"] == 0.5
    assert receipt["selected_ranks"] == [0]

    # A target that was never in the bounded window is not a retrieval failure and is not counted.
    outside = DevelopmentEpisode.model_validate(
        {
            "case_id": "case-outside",
            "cluster_id": "c1",
            "stratum": "s",
            "context": context,
            "accepted_review": {"novelty": {"judgment": "restatement", "duplicate_of": "long-gone"}},
            "policy_metric": {"seen": [{"event_id": "prior"}], "told": []},
        }
    )
    assert _retrieval_receipt([outside])["accepted_restatements_in_window"] == 0


def test_an_open_primary_circuit_stops_the_run_rather_than_scoring_every_case_zero() -> None:
    """The compile route has one slot and no fallback, so an open breaker means the endpoint is down.

    `graph.judge` opens the primary breaker after three retryable failures and then refuses every call for
    60 seconds without touching a provider. Caught like any other per-example failure that would score
    `0.0` — indistinguishable on the Pareto front from a candidate that genuinely answered badly, and for
    every case in the window. Neither predecessor student had a breaker at all, so this is new surface.
    """

    from tracefold.news.learning.metric import bind_metric
    from tracefold.news.learning.optimizer import NewsGepaAdapter
    from tracefold.news.program.transport import PredictorAdapterError

    class _DownAdapter(_MeteredTaskAdapter):
        async def invoke(self, request: PredictorRequest, spec: PredictorSpec) -> PredictorResponse:
            raise PredictorAdapterError("news_program_provider_http_503", retryable=True)

    stable = load_stable_program_artifact()
    adapter = NewsGepaAdapter(
        adapter=_DownAdapter(),
        metric=bind_metric(_NoopJudge()),
        proposer=InstructionProposer(reflection_lm=_MeteredFakeReflectionLM()),
    )
    examples = [_compile_example(episode) for episode in _episodes()]
    seed = {name: stable.instruction_for(name) for name in ("event_semantics", "reader_card")}

    with pytest.raises(Exception, match="primary_circuit_open"):
        adapter.evaluate(examples, seed, capture_traces=True)


def test_the_reflection_role_keeps_its_metered_transport_retry() -> None:
    """One transient reset must not abort a multi-hour run.

    `raise_on_exception` is on, so a reflection failure propagates out of the engine. The predecessor
    routed both roles through the same retry; keeping only the task side would have been a silent
    regression rather than a decision.
    """

    class _FlakyReflectionLM(_MeteredFakeReflectionLM):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def __call__(self, prompt: Any) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("peer reset the connection")
            return super().__call__(prompt)

    inner = _FlakyReflectionLM()
    meter = _BudgetMeter(_budget(max_reflection_model_calls=4), imputed_call_cost_microusd=5)
    lm = _MeteredReflectionLM(inner, meter=meter)

    assert "A proposed replacement instruction." in lm(prompt="probe")
    assert inner.attempts == 2
    assert lm.transport_retries == 1
    # Both physical attempts are charged: a retry the budget could not see would not be a budget.
    assert meter.reflection_model_calls == 2


def test_a_provider_that_refuses_is_still_charged_to_the_cost_budget() -> None:
    """`before()` refuses a call the budget cannot afford; it does not accumulate anything itself.

    So an attempt that reached the provider and came back 429 or 503 has to be settled, or a run of them
    spends real provider-side work against a ledger that never moves and the usage receipt reports a run
    as within a budget it exceeded. There is no usage block on a status error, so the settle charges the
    operator's own declared per-call ceiling — the conservative direction, and the same one an unpriced
    success takes.
    """

    from tracefold.news.program.transport import PredictorAdapterError

    class _RefusingEndpoint(_MeteredTaskAdapter):
        async def invoke(self, request: PredictorRequest, spec: PredictorSpec) -> PredictorResponse:
            raise PredictorAdapterError("news_program_provider_http_429", retryable=True, provider_reached=True)

    meter = _BudgetMeter(_budget(max_task_model_calls=8), imputed_call_cost_microusd=5)
    adapter = _MeteredPredictorAdapter(_RefusingEndpoint(), meter=meter)
    spec = _spec()

    with pytest.raises(PredictorAdapterError, match="http_429"):
        asyncio.run(adapter.invoke(_fake_request(adapter, spec), spec))

    # Three attempts (the retry is metered too), each charged the declared per-call ceiling.
    assert meter.task_model_calls == 3
    assert meter.actual_cost_microusd == 15
    assert meter.imputed_cost_calls == 3


def test_a_request_that_never_arrived_is_not_charged() -> None:
    """The other half of the same rule: nothing answered, so nothing was billed.

    Charging here would invent spend, and the bound that matters for an unarrived request is the one
    `before()` already applied — it refuses to start a call the budget could not afford.
    """

    from tracefold.news.program.transport import PredictorAdapterError

    class _UnreachableEndpoint(_MeteredTaskAdapter):
        async def invoke(self, request: PredictorRequest, spec: PredictorSpec) -> PredictorResponse:
            raise PredictorAdapterError("news_program_transport_connecttimeout", retryable=True)

    meter = _BudgetMeter(_budget(max_task_model_calls=8), imputed_call_cost_microusd=5)
    adapter = _MeteredPredictorAdapter(_UnreachableEndpoint(), meter=meter)
    spec = _spec()

    with pytest.raises(PredictorAdapterError, match="connecttimeout"):
        asyncio.run(adapter.invoke(_fake_request(adapter, spec), spec))

    assert meter.task_model_calls == 3
    assert meter.actual_cost_microusd == 0
    assert adapter.transport_failures == 1


def test_the_growth_floor_rejects_a_merged_fat_candidate_before_any_provider_call() -> None:
    """#334: merge combines two lineages per predictor without ever calling `InstructionProposer`.

    Each component here grew within what a per-component allowance would forgive; their sum blows the
    shared envelope. The floor in `_program` is where every candidate must pass, so the merged one dies
    for a code the metric can report — and for zero provider spend.
    """

    from tracefold.news.learning.metric import bind_metric
    from tracefold.news.learning.optimizer import InstructionGrowthBudget, NewsGepaAdapter
    from tracefold.news.program.transport import ScriptedPredictorAdapter

    stable = load_stable_program_artifact()
    seeds = {name: stable.instruction_for(name) for name in ("event_semantics", "reader_card")}
    task = ScriptedPredictorAdapter([])
    adapter = NewsGepaAdapter(
        adapter=task,
        metric=bind_metric(_NoopJudge()),
        proposer=InstructionProposer(reflection_lm=_MeteredFakeReflectionLM()),
        growth_budget=InstructionGrowthBudget.from_seeds(seeds, max_growth_tokens=50),
    )
    merged = {name: text + " Lineage growth." * 10 for name, text in seeds.items()}

    batch = [_compile_example(episode) for episode in _episodes()][:1]
    result = adapter.evaluate(batch, merged, capture_traces=True)

    assert task.requests == [], "a budget-rejected candidate must not spend a single provider call"
    rollout = result.trajectories[0]
    assert rollout.prediction.instruction_rejected == "news_program_instruction_growth_budget"


def test_run_gepa_wires_the_default_budget_from_the_seed_instructions() -> None:
    """#334 acceptance: the production path is budgeted by default, and a refactor that drops the wiring
    must fail here rather than silently turning the budget into a no-op."""

    from tracefold.news.learning.optimizer import (
        INSTRUCTION_GROWTH_BUDGET_TOKENS,
        NewsGepaAdapter,
    )
    from tracefold.news.program.runtime import _estimated_tokens

    fake = _FakeGepaOptimize()
    _run(optimize_fn=fake)
    kwargs = fake.calls[-1]
    adapter = kwargs["adapter"]
    assert isinstance(adapter, NewsGepaAdapter)

    stable = load_stable_program_artifact()
    budget = adapter._growth_budget
    assert budget is not None
    assert budget.seed_tokens == {
        name: _estimated_tokens(stable.instruction_for(name)) for name in ("event_semantics", "reader_card")
    }
    assert budget.max_growth_tokens == INSTRUCTION_GROWTH_BUDGET_TOKENS == 800
    # One budget object, two enforcement points: the proposer teaches, the floor catches merge.
    assert adapter.propose_new_texts._budget is budget
