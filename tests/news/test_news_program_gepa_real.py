"""The native public DSPy GEPA path over blind Gold, for each optimization target (#501, #651)."""

from __future__ import annotations

import copy
import hashlib
from typing import Any, cast

import dspy  # type: ignore[import-untyped]
import pytest
from dspy.teleprompt.gepa.gepa import DspyGEPAResult  # type: ignore[import-untyped]

from tests.support.news_judgment import news_taxonomy, scored_judgment
from tracefold.news.artifact_identity import canonical_json
from tracefold.news.learning.contracts import OptimizationBudget
from tracefold.news.learning.objective import DevelopmentEpisode, build_gepa_objective_plan
from tracefold.news.learning.optimizer import (
    GepaNoProgramChange,
    GepaRunResult,
    _BudgetMeter,
    _MeteredLearningLM,
    build_reflection_lm,
    build_task_lm,
    run_gepa,
)
from tracefold.news.program.artifact import load_stable_program_state
from tracefold.news.program.contracts import TriageContext
from tracefold.news.program.lm import LMCallLedger
from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION
from tracefold.news.taxonomy import ModelTaxonomyV1

_ADVISORY = "Classify taxonomy-target evidence as other with unknown state and assertion."
_PRODUCT_TAXONOMY = {
    "subject_codes": ["medtop:20000205"],
    "event_family": "product_service_change",
    "change_state": "announced",
    "assertion_status": "confirmed",
}
_OTHER_TAXONOMY = {
    "subject_codes": [],
    "event_family": "other",
    "change_state": "unknown",
    "assertion_status": "unknown",
}


def _answer(taxonomy: dict[str, Any], *, model: str) -> dspy.LMResponse:
    return dspy.LMResponse.from_text(
        canonical_json({"taxonomy": taxonomy}),
        model=model,
        usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
        cost=0,
    )


class _TaskLM(dspy.BaseLM):  # type: ignore[misc]
    """The seed labels every Event as a product change; the reflected instruction repairs the targets."""

    forward_contract = "typed_lm"

    def __init__(self) -> None:
        super().__init__("openai/scripted-task", cache=False, num_retries=0)
        self.requests: list[dspy.LMRequest] = []

    @property
    def supports_response_schema(self) -> bool:
        return True

    @property
    def supported_params(self) -> set[str]:
        return {"response_format"}

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        self.requests.append(request)
        rendered = str(request.messages)
        target = '"title":"taxonomy-target' in rendered
        taxonomy = _OTHER_TAXONOMY if target and _ADVISORY in rendered else _PRODUCT_TAXONOMY
        return _answer(taxonomy, model=self.model)


class _ReflectionLM(dspy.BaseLM):  # type: ignore[misc]
    forward_contract = "typed_lm"

    def __init__(self) -> None:
        super().__init__("openai/scripted-reflection", cache=False, num_retries=0)
        self.requests: list[dspy.LMRequest] = []

    @property
    def supports_response_schema(self) -> bool:
        return True

    @property
    def supported_params(self) -> set[str]:
        return {"response_format"}

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        self.requests.append(request)
        text = canonical_json({"new_instruction": _ADVISORY}) if request.config.response_format else _ADVISORY
        return dspy.LMResponse.from_text(
            text,
            model=self.model,
            usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            cost=0,
        )


class _CandidateTruncatedTaskLM(_TaskLM):
    """Stable completes; the reflected instruction truncates on one held-out target."""

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        rendered = str(request.messages)
        if _ADVISORY in rendered and '"title":"taxonomy-target 11"' in rendered:
            self.requests.append(request)
            response = dspy.LMResponse.from_text(
                "{",
                model=self.model,
                usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                cost=0.000003,
            )
            response.outputs[0] = response.output.model_copy(update={"finish_reason": "length", "truncated": True})
            return response
        return super().forward(request)


class _CandidateInvalidTaskLM(_TaskLM):
    """Stable completes; the reflected instruction emits one typed-invalid held-out answer."""

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        rendered = str(request.messages)
        if _ADVISORY in rendered and '"title":"taxonomy-target 11"' in rendered:
            self.requests.append(request)
            return _answer({**_OTHER_TAXONOMY, "event_family": "whale"}, model=self.model)
        return super().forward(request)


def _episode(index: int, *, target: bool, **review_updates: Any) -> DevelopmentEpisode:
    """One accepted case. `target` means Stable's persisted label disagrees with the blind Gold."""

    opened_at_ms = 1_787_000_000_000 + index * 60_000
    title = f"taxonomy-{'target' if target else 'control'} {index}"
    card = {
        "event_id": f"{index:064d}",
        "evidence_version": 1,
        "evidence_sha256": "a" * 64,
        "focus_fact_id": f"{index:064d}",
        "leader_title": title,
        "leader_description": "",
        "leader_url": f"https://example.invalid/{index}",
        "reporting_origin": "wire",
        "dedupe_family": "general",
        "admission": "candidate",
        "queue_priority": "normal",
        "asset_class": "equity_or_commodity",
        "engine_type": "news",
        "ingest_mode": "live",
        "storyline_key": "asset:TSLA",
        "comparison_title": title,
        "raw_first_line": title,
        "grounded_assets": ["TSLA"],
        "watchlist_hits": [],
        "member_count": 1,
        "opened_at_ms": opened_at_ms,
        "expires_at_ms": opened_at_ms + 3_600_000,
        "last_member_at_ms": opened_at_ms,
        "macro_lexicon": False,
        "provenance": ["1018"],
        "trace_id": f"{index:032d}",
        "leader_item_id": f"{index:064d}",
        "provider_metadata": {},
    }
    stable_taxonomy = news_taxonomy(**_PRODUCT_TAXONOMY)
    review: dict[str, Any] = {
        "should_push": "uncertain",
        "dimensions": {},
        "novelty": {"judgment": "uncertain", "duplicate_of": ""},
        "expected": {},
        "expected_correction": "",
        # Audit metadata only (#501 D9): an owner column grants no optimization authority.
        "first_bad_owner_explicit": "taxonomy" if target else None,
        "first_bad_owner": "taxonomy" if target else None,
        "evidence_refs": ["source#taxonomy"] if target else [],
        "taxonomy": _OTHER_TAXONOMY if target else _PRODUCT_TAXONOMY,
        "taxonomy_review": {
            "label_source": "model_draft",
            "draft_author": "drafter-a+drafter-b",
            "review_role": "primary",
        },
    }
    review.update(review_updates)
    return DevelopmentEpisode(
        case_id=f"{index:064x}",
        cluster_id=f"{index:064x}",
        stratum="taxonomy_failure" if target else "taxonomy_control",
        context=TriageContext.from_card(card, watchlist=(), told_rows=[], now_ms=opened_at_ms, queue_lag_ms=0),
        accepted_review=review,
        production_judgment=scored_judgment(
            {
                "novelty": "new_fact",
                "restates": -1,
                "assets": [{"symbol": "TSLA", "role": "primary"}],
                "magnitude": 2,
                "direction": "bullish",
                "audience": "us_equity",
                "scope": "single_name",
                "confidence": 0.9,
                "headline_zh": "特斯拉发布产品",
                "why_zh": "产品变化影响交付预期",
            },
            taxonomy=stable_taxonomy,
            source_authority="reputable_secondary",
        ),
    )


def _corpus() -> tuple[DevelopmentEpisode, ...]:
    return tuple(_episode(index, target=index % 2 == 1) for index in range(1, 13))


def _models(
    task_delegate: _TaskLM | None = None,
) -> tuple[Any, Any, _TaskLM, _ReflectionLM, LMCallLedger]:
    task_delegate = task_delegate or _TaskLM()
    reflection_delegate = _ReflectionLM()
    ledger = LMCallLedger(max_calls_per_predictor=None, max_calls_per_route=None, max_calls_per_scope=None)
    task = build_task_lm(
        model_name=task_delegate.model,
        api_key="k",
        api_base="https://scripted-task.invalid/v1",
        timeout=20.0,
        max_tokens=400,
        ledger=ledger,
        delegate=task_delegate,
    )
    reflection = build_reflection_lm(
        model_name=reflection_delegate.model,
        api_key="k",
        api_base="https://scripted-reflection.invalid/v1",
        ledger=ledger,
        delegate=reflection_delegate,
    )
    return task, reflection, task_delegate, reflection_delegate, ledger


def _synthetic_compile(
    *,
    instructions: tuple[str, ...],
    aggregate_scores: tuple[float, ...],
    validation_subscores: tuple[dict[int, float], ...],
) -> Any:
    def compile_result(student: dspy.Module, **_kwargs: Any) -> dspy.Module:
        candidates = []
        for instruction in instructions:
            candidate = copy.deepcopy(student)
            predictor = next(iter(dict(candidate.named_predictors()).values()))
            predictor.signature = predictor.signature.with_instructions(instruction)
            candidates.append(candidate)
        student.detailed_results = DspyGEPAResult(
            candidates=candidates,
            parents=[[None], *[[0] for _ in candidates[1:]]],
            val_aggregate_scores=list(aggregate_scores),
            val_subscores=[dict(scores) for scores in validation_subscores],
            val_aggregate_subscores=[{"four_axis_exact_accuracy": score} for score in aggregate_scores],
            per_val_instance_best_candidates={},
            discovery_eval_counts=list(range(len(candidates))),
            total_metric_calls=10,
        )
        return student

    return compile_result


def _run_synthetic_gepa(
    *,
    instructions: tuple[str, ...],
    aggregate_scores: tuple[float, ...],
    validation_subscores: tuple[dict[int, float], ...] | None = None,
    auto: str | None = None,
    target: str = "classification",
) -> GepaRunResult:
    task, reflection, _task, _reflection, _ledger = _models()
    val_count = len(build_gepa_objective_plan(_corpus()).development_selection_episodes)
    rows = validation_subscores or tuple(dict.fromkeys(range(val_count), score) for score in aggregate_scores)
    return run_gepa(
        base_program=load_stable_program_state(),
        episodes=_corpus(),
        task_lm=task,
        reflection_lm=reflection,
        target=target,
        auto=auto,
        max_metric_calls=None if auto else 40,
        seed=456,
        review_rubric_version=REVIEW_RUBRIC_VERSION,
        compile_fn=_synthetic_compile(
            instructions=instructions,
            aggregate_scores=aggregate_scores,
            validation_subscores=rows,
        ),
    )


def _candidates(predictor: str = "taxonomy") -> tuple[str, str, str]:
    stable = load_stable_program_state().instruction_for(predictor)
    return stable, stable + "\n\nCandidate one.", stable + "\n\nCandidate two."


def test_every_gold_case_is_an_optimizer_sample_whatever_its_owner_column_says() -> None:
    plan = build_gepa_objective_plan(_corpus())

    assert plan.blocking_reasons == ()
    assert len(plan.optimizer_cluster_ids) == 12
    assert plan.stable_exact_n == 6 and plan.stable_mismatch_n == 6
    assert plan.target_predictors == ("taxonomy",)
    assert len(plan.train_episodes) == 8 and len(plan.development_selection_episodes) == 4


@pytest.mark.parametrize(
    ("target", "predictor"),
    [
        pytest.param("classification", "taxonomy", id="classification"),
        pytest.param("understanding", "event_semantics", id="understanding"),
        pytest.param("explanation", "reader_card", id="explanation"),
    ],
)
def test_gepa_best_strictly_above_the_seed_advances_only_the_target_predictor(target: str, predictor: str) -> None:
    """#651: one assembly, three targets, and only the target Predictor's state moves."""

    stable_state = load_stable_program_state()
    stable, candidate_one, candidate_two = _candidates(predictor)

    result = _run_synthetic_gepa(
        instructions=(stable, candidate_one, candidate_two),
        aggregate_scores=(0.5, 0.7, 0.9),
        target=target,
    )

    assert result.target == target
    assert result.state.changed_predictors(stable_state) == (predictor,)
    assert result.state.instruction_for(predictor) == candidate_two
    for other in ("event_semantics", "taxonomy", "reader_card"):
        if other != predictor:
            assert result.state.predictor_document(other) == stable_state.predictor_document(other)
    selection = result.metric["target_selection_score"]
    assert selection["schema"] == "tracefold.news.target_selection_score.v4"
    assert selection["gepa_best_index"] == 2
    assert selection["admitted"] is True
    assert selection["gepa_best_instruction_valid"] is True
    assert selection["gepa_best_demo_n"] == 0
    assert selection["delta"]["target_overall"] == 0.4
    assert result.metric["predictor_change"]["target"] == target
    assert result.metric["predictor_change"]["predictor"] == predictor
    assert result.public_result["gepa_best_index"] == 2
    assert result.public_result["admitted"] is True
    assert result.optimizer_cluster_ids == build_gepa_objective_plan(_corpus()).optimizer_cluster_ids


def test_gepa_demos_travel_with_the_winning_candidate() -> None:
    """#651: a GEPA winner that attaches few-shot demos is a candidate, not a refusal."""

    stable_state = load_stable_program_state()
    stable, candidate_one, _unused = _candidates()
    demo = {"evidence_json": "<evidence>", "taxonomy": dict(_OTHER_TAXONOMY)}

    def compile_with_demos(student: dspy.Module, **_kwargs: Any) -> dspy.Module:
        val_count = len(build_gepa_objective_plan(_corpus()).development_selection_episodes)
        candidates = []
        for index, instruction in enumerate((stable, candidate_one)):
            candidate = copy.deepcopy(student)
            predictor = next(iter(dict(candidate.named_predictors()).values()))
            predictor.signature = predictor.signature.with_instructions(instruction)
            if index:
                predictor.demos = [dict(demo)]
            candidates.append(candidate)
        student.detailed_results = DspyGEPAResult(
            candidates=candidates,
            parents=[[None], [0]],
            val_aggregate_scores=[0.5, 0.9],
            val_subscores=[dict.fromkeys(range(val_count), score) for score in (0.5, 0.9)],
            val_aggregate_subscores=[{"four_axis_exact_accuracy": score} for score in (0.5, 0.9)],
            per_val_instance_best_candidates={},
            discovery_eval_counts=[0, 1],
            total_metric_calls=10,
        )
        return student

    task, reflection, _task, _reflection, _ledger = _models()
    result = run_gepa(
        base_program=stable_state,
        episodes=_corpus(),
        task_lm=task,
        reflection_lm=reflection,
        max_metric_calls=40,
        seed=456,
        review_rubric_version=REVIEW_RUBRIC_VERSION,
        compile_fn=compile_with_demos,
    )

    assert result.state.demos_for("taxonomy") == (demo,)
    assert result.state.changed_predictors(stable_state) == ("taxonomy",)
    assert result.metric["target_selection_score"]["gepa_best_demo_n"] == 1
    assert result.metric["predictor_change"]["taxonomy"]["after_demo_n"] == 1


@pytest.mark.parametrize(
    ("aggregate_scores", "expected_best"),
    [
        pytest.param((0.8, 0.5), 0, id="seed-is-best"),
        pytest.param((0.5, 0.5), 0, id="tie-is-not-strictly-better"),
    ],
)
def test_gepa_best_not_strictly_above_the_seed_is_a_no_op(
    aggregate_scores: tuple[float, float], expected_best: int
) -> None:
    stable, candidate, _unused = _candidates()

    with pytest.raises(GepaNoProgramChange) as caught:
        _run_synthetic_gepa(instructions=(stable, candidate), aggregate_scores=aggregate_scores)

    result = caught.value.result
    assert result.metric["target_selection_score"]["gepa_best_index"] == expected_best
    assert result.metric["target_selection_score"]["admitted"] is False
    assert result.state.instruction_for("taxonomy") == stable
    assert result.state.changed_predictors(load_stable_program_state()) == ()
    assert result.public_result["admitted"] is False


def test_selection_never_replays_controls_or_a_growth_budget() -> None:
    """#501 §9: no per-control replay, no per-objective check, no growth budget in selection."""

    stable, candidate, _unused = _candidates()
    val_count = len(build_gepa_objective_plan(_corpus()).development_selection_episodes)
    rows = (dict.fromkeys(range(val_count), 1.0), dict.fromkeys(range(val_count), 0.0))
    rows[1][0] = 1.0  # every other selection example regresses; the aggregate still decides

    result = _run_synthetic_gepa(
        instructions=(stable, candidate + "\n" + ("x" * 4_000)),
        aggregate_scores=(0.5, 0.9),
        validation_subscores=rows,
    )

    assert result.state.instruction_for("taxonomy") != stable
    selection = result.metric["target_selection_score"]
    assert set(selection) == {
        "schema",
        "candidate_0",
        "gepa_best_index",
        "gepa_best",
        "gepa_best_instruction_valid",
        "gepa_best_demo_n",
        "admitted",
        "delta",
    }


def test_an_oversized_best_candidate_is_a_no_op_not_a_crash() -> None:
    stable, _candidate, _unused = _candidates()

    with pytest.raises(GepaNoProgramChange) as caught:
        _run_synthetic_gepa(instructions=(stable, "y" * 40_000), aggregate_scores=(0.5, 0.9))

    assert caught.value.result.metric["target_selection_score"]["gepa_best_instruction_valid"] is False
    assert caught.value.result.metric["target_selection_score"]["admitted"] is False


def test_auto_light_resolves_to_dspys_own_budget_and_the_receipt_records_it() -> None:
    stable, _candidate, candidate_two = _candidates()

    result = _run_synthetic_gepa(instructions=(stable, candidate_two), aggregate_scores=(0.5, 0.9), auto="light")

    scalars = result.optimizer_config["constructor_scalar_arguments"]
    val_count = len(build_gepa_objective_plan(_corpus()).development_selection_episodes)
    expected = dspy.GEPA.auto_budget(None, num_preds=1, num_candidates=6, valset_size=val_count)
    assert scalars["auto"] == "light"
    assert scalars["max_metric_calls"] == expected
    assert result.optimizer_config["optimizer"]["add_format_failure_as_feedback"] is False


def test_real_gepa_uses_one_native_taxonomy_predict_and_returns_public_trajectory() -> None:
    task, reflection, task_delegate, reflection_delegate, _ledger = _models()
    stable = load_stable_program_state()

    result = run_gepa(
        base_program=stable,
        episodes=_corpus(),
        task_lm=task,
        reflection_lm=reflection,
        max_metric_calls=40,
        seed=456,
        review_rubric_version=REVIEW_RUBRIC_VERSION,
    )

    assert result.target == "classification"
    assert result.state.instruction_for("taxonomy") == _ADVISORY
    assert result.state.changed_predictors(stable) == ("taxonomy",)
    assert result.metric["schema"] == "tracefold.news.taxonomy_gepa_metric.v4"
    assert result.metric["target_selection_score"]["delta"]["target_overall"] > 0
    assert result.metric["target_selection_score"]["admitted"] is True
    change = result.metric["predictor_change"]
    assert change["schema"] == "tracefold.news.predictor_state_change.v1"
    assert change["taxonomy"]["changed"] is True
    assert change["taxonomy"]["estimated_token_growth"] < 0
    assert _ADVISORY in change["taxonomy"]["unified_diff"]
    for predictor in ("event_semantics", "reader_card"):
        assert change[predictor] == {
            "instruction_sha256": hashlib.sha256(stable.instruction_for(predictor).encode()).hexdigest(),
            "demo_n": 0,
            "unchanged": True,
        }
    assert result.public_result["schema"] == "tracefold.news.dspy_gepa_public_result.v3"
    assert result.public_result["candidate_count"] >= 2
    assert result.public_result["gepa_best_index"] != 0
    assert result.public_result["validation_aggregate_objective_scores"]
    assert task_delegate.requests
    rendered = str([request.messages for request in task_delegate.requests])
    assert "event_status" not in rendered and "semantics_json" not in rendered
    assert reflection_delegate.requests
    reflection_text = str([request.messages for request in reflection_delegate.requests])
    # The metric's feedback quotes the codebook, so the reflection model reads the rule the seed states.
    assert "expected=other (" in reflection_text
    assert "predicted=product_service_change (" in reflection_text
    assert "rule (event_family):" in reflection_text


def test_run_gepa_requires_exactly_one_budget_form() -> None:
    task, reflection, _task, _reflection, _ledger = _models()

    for budget in ({}, {"auto": "light", "max_metric_calls": 40}):
        with pytest.raises(ValueError, match="exactly_one_of_auto_or_max_metric_calls"):
            run_gepa(
                base_program=load_stable_program_state(),
                episodes=_corpus(),
                task_lm=task,
                reflection_lm=reflection,
                seed=456,
                review_rubric_version=REVIEW_RUBRIC_VERSION,
                **budget,
            )


def test_run_gepa_rejects_a_non_native_detailed_result() -> None:
    task, reflection, task_delegate, reflection_delegate, _ledger = _models()

    def fake_compile(student: dspy.Module, **_kwargs: Any) -> dspy.Module:
        student.detailed_results = object()
        return student

    with pytest.raises(ValueError, match="news_program_compile_detailed_results_missing"):
        run_gepa(
            base_program=load_stable_program_state(),
            episodes=_corpus(),
            task_lm=task,
            reflection_lm=reflection,
            max_metric_calls=40,
            seed=456,
            review_rubric_version=REVIEW_RUBRIC_VERSION,
            compile_fn=fake_compile,
        )

    assert task_delegate.requests == []
    assert reflection_delegate.requests == []


def test_candidate_task_truncation_scores_zero_and_keeps_the_batch_aligned() -> None:
    task, reflection, task_delegate, _reflection_delegate, ledger = _models(_CandidateTruncatedTaskLM())
    meter = _BudgetMeter(
        OptimizationBudget(
            max_metric_calls=40,
            max_task_model_calls=100,
            max_reflection_model_calls=10,
            max_cost_microusd=100_000,
            max_call_cost_microusd=1_000,
            max_wall_clock_seconds=3_600,
            seed=456,
        ),
        imputed_call_cost_microusd=1_000,
    )
    metered_task = _MeteredLearningLM(task, meter=meter, role="task")
    metered_reflection = _MeteredLearningLM(reflection, meter=meter, role="reflection")

    try:
        result = run_gepa(
            base_program=load_stable_program_state(),
            episodes=_corpus(),
            task_lm=metered_task,
            reflection_lm=metered_reflection,
            max_metric_calls=40,
            seed=456,
            review_rubric_version=REVIEW_RUBRIC_VERSION,
        )
    except GepaNoProgramChange as caught:
        result = caught.result

    truncated_validation_index = next(
        index
        for index, episode in enumerate(build_gepa_objective_plan(_corpus()).development_selection_episodes)
        if episode.case_id == f"{11:064x}"
    )
    subscores = result.public_result["validation_subscores"]
    assert result.public_result["candidate_count"] >= 2
    # Native `failure_score`, never a sentinel below the real scale (#501 D5).
    assert all(score >= 0.0 for candidate_scores in subscores for score in candidate_scores.values())
    assert any(candidate_scores[str(truncated_validation_index)] == 0.0 for candidate_scores in subscores[1:])
    assert meter.first_terminal_error is None
    assert metered_task.transport_failures == 0
    truncated_indexes = [
        index
        for index, receipt in enumerate(ledger.receipts)
        if receipt.error_code == "news_program_lm_output_truncated"
    ]
    assert len(truncated_indexes) == 1
    truncated = ledger.receipts[truncated_indexes[0]]
    assert truncated.terminal_disposition == "provider_success"
    assert (truncated.input_tokens, truncated.output_tokens, truncated.total_tokens) == (11, 7, 18)
    assert truncated.provider_cost_microusd == 3
    # The batch stayed aligned: the run kept asking after the truncated answer.
    assert any(receipt.model_binding == "task" for receipt in ledger.receipts[truncated_indexes[0] + 1 :])
    assert task_delegate.requests


def test_candidate_typed_invalid_output_keeps_gepa_batch_aligned() -> None:
    task, reflection, task_delegate, _reflection_delegate, _ledger = _models(_CandidateInvalidTaskLM())

    result = run_gepa(
        base_program=load_stable_program_state(),
        episodes=_corpus(),
        task_lm=task,
        reflection_lm=reflection,
        max_metric_calls=40,
        seed=456,
        review_rubric_version=REVIEW_RUBRIC_VERSION,
    )

    assert result.public_result["gepa_best_index"] != 0
    invalid_validation_index = next(
        index
        for index, episode in enumerate(build_gepa_objective_plan(_corpus()).development_selection_episodes)
        if episode.case_id == f"{11:064x}"
    )
    best_index = result.public_result["gepa_best_index"]
    assert result.public_result["validation_subscores"][best_index][str(invalid_validation_index)] == 0
    invalid_index = next(
        index
        for index, request in enumerate(task_delegate.requests)
        if _ADVISORY in str(request.messages) and '"title":"taxonomy-target 11"' in str(request.messages)
    )
    # The batch stayed aligned: the run kept asking after the typed-invalid answer.
    assert task_delegate.requests[invalid_index + 1 :]


_SEMANTICS_ANSWER: dict[str, Any] = {
    "novelty": "new_fact",
    "restates": -1,
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "direction": "bullish",
    "scope": "single_name",
    "magnitude": 2,
    "confidence": 0.9,
    "audience": "us_equity",
    "relevance": {
        "impact_breadth": "single_name",
        "tradability": "direct",
        "surprise": "expected",
        "development_delta": "new_fact",
        "channels": ["us_equity"],
        "affected_markets": ["us_equity"],
        "reader_value": "actionable",
    },
}
_CARD_ANSWER: dict[str, Any] = {"headline_zh": "特斯拉发布产品", "why_zh": "产品变化影响交付预期。"}


class _FixedAnswerTaskLM(dspy.BaseLM):  # type: ignore[misc]
    """One scripted typed answer for whichever Predictor the target names. No network, no provider."""

    forward_contract = "typed_lm"

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("openai/scripted-task", cache=False, num_retries=0)
        self._payload = payload
        self.requests: list[dspy.LMRequest] = []

    @property
    def supports_response_schema(self) -> bool:
        return True

    @property
    def supported_params(self) -> set[str]:
        return {"response_format"}

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        self.requests.append(request)
        return dspy.LMResponse.from_text(
            canonical_json(self._payload),
            model=self.model,
            usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            cost=0,
        )


def _graded_corpus() -> tuple[DevelopmentEpisode, ...]:
    """The same corpus with accepted semantics and card Gold, so the non-taxonomy rulers can separate."""

    return tuple(
        _episode(
            index,
            target=index % 2 == 1,
            novelty={"judgment": "progression" if index % 2 else "new_fact", "duplicate_of": ""},
            dimensions={"asset_grounding": "fail", "headline_fidelity": "fail", "why_support": "fail"},
            expected={
                "assets": [{"symbol": "NVDA" if index % 2 else "TSLA", "role": "primary"}],
                "headline_zh": "英伟达发布产品" if index % 2 else "特斯拉发布产品",
                "why_zh": "产品变化影响交付预期。",
            },
        )
        for index in range(1, 13)
    )


@pytest.mark.parametrize(
    ("target", "payload_key", "payload"),
    [
        pytest.param("understanding", "semantics", _SEMANTICS_ANSWER, id="understanding"),
        pytest.param("explanation", "card", _CARD_ANSWER, id="explanation"),
    ],
)
def test_real_gepa_runs_end_to_end_for_every_target_without_a_network(
    target: str, payload_key: str, payload: dict[str, Any]
) -> None:
    """#651: the same stock `dspy.GEPA.compile` drives each target against scripted typed doubles."""

    task, reflection, task_delegate, _reflection_delegate, _ledger = _models(
        cast(Any, _FixedAnswerTaskLM({payload_key: payload}))
    )
    stable = load_stable_program_state()
    corpus = _graded_corpus()

    try:
        result = run_gepa(
            base_program=stable,
            episodes=corpus,
            task_lm=task,
            reflection_lm=reflection,
            target=target,
            max_metric_calls=12,
            seed=456,
            review_rubric_version=REVIEW_RUBRIC_VERSION,
        )
    except GepaNoProgramChange as caught:
        result = caught.result

    predictor = {"understanding": "event_semantics", "explanation": "reader_card"}[target]
    assert result.target == target
    assert result.metric["predictor_change"]["predictor"] == predictor
    assert result.state.changed_predictors(stable) in ((), (predictor,))
    assert result.public_result["candidate_count"] >= 1
    assert result.public_result["validation_aggregate_objective_scores"]
    assert task_delegate.requests
    rendered = str([request.messages for request in task_delegate.requests])
    # Each target asks exactly its own Predictor's frozen question. ReaderCard is the only one handed the
    # accepted semantics view; EventSemantics is the only one shown the told ledger. Both comparisons are
    # written as `is` so a failure prints a boolean rather than diffing a multi-megabyte transcript.
    assert ("semantics_json" in rendered) is (target == "explanation")
    assert ("event_status" in rendered) is (target == "understanding")


class _TypedInvalidModule(dspy.Module):  # type: ignore[misc]
    """Raises the exact typed failure a candidate produces when its JSON does not validate."""

    def forward(self, evidence_json: str) -> dspy.Prediction:
        del evidence_json
        ModelTaxonomyV1.model_validate({})
        raise AssertionError("news_program_test_typed_failure_not_raised")


def test_the_learning_student_is_what_keeps_a_typed_failure_scoreable_on_dspy_331() -> None:
    """Reproduce the upstream limit `_LearningStudent` exists for, against the installed dspy 3.3.1.

    GEPA evaluates a candidate through `bootstrap_trace.bootstrap_trace_data`, whose `patched_forward`
    handles `AdapterParseError` and re-raises everything else. `Evaluate` then records the example as an
    error, its prediction is not the `(prediction, trace)` tuple the caller unpacks, and that row is
    dropped — so the batch GEPA gets back is shorter than the one it submitted. The wrapper turns the same
    failure into an ordinary Prediction the metric scores at `failure_score`.
    """

    from dspy.teleprompt.bootstrap_trace import bootstrap_trace_data

    from tracefold.news.learning.optimizer import _ClassificationMetric, _LearningStudent

    example = dspy.Example(
        evidence_json="<tracefold-untrusted-event-json-v1>\n{}\n</tracefold-untrusted-event-json-v1>",
        gold_taxonomy=dict(_OTHER_TAXONOMY),
    ).with_inputs("evidence_json")

    def trajectories(program: dspy.Module) -> list[dict[str, Any]]:
        return bootstrap_trace_data(
            program=program,
            dataset=[example],
            metric=_ClassificationMetric(),
            raise_on_error=False,
            capture_failed_parses=True,
            failure_score=0.0,
            format_failure_score=0.0,
        )

    naked = trajectories(_TypedInvalidModule())
    wrapped = trajectories(_LearningStudent(cast(dspy.Predict, _TypedInvalidModule()), output_type=ModelTaxonomyV1))

    # Native behaviour drops the row: GEPA would then index past the end of a shortened batch.
    assert naked == []
    # The wrapper keeps it, scored at the native `failure_score`, with the typed failure as feedback.
    assert len(wrapped) == 1
    assert wrapped[0]["score"].score == 0.0
    assert "ModelTaxonomyV1 is invalid" in wrapped[0]["score"].feedback
