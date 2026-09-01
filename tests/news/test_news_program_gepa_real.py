"""Issue #456: exercise the native, taxonomy-only public DSPy GEPA path."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest
from dspy.teleprompt.gepa.gepa import DspyGEPAResult  # type: ignore[import-untyped]

from tests.support.news_judgment import news_taxonomy, scored_judgment, trade_relevance
from tracefold.news.artifact_identity import canonical_json
from tracefold.news.learning.contracts import OptimizationBudget
from tracefold.news.learning.objective import DevelopmentEpisode, build_gepa_objective_plan
from tracefold.news.learning.optimizer import (
    GepaNoProgramChange,
    _BudgetMeter,
    _MeteredLearningLM,
    build_reflection_lm,
    build_task_lm,
    run_gepa,
)
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.contracts import TriageContext
from tracefold.news.program.lm import LMCallLedger
from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION

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
_SEMANTICS = {
    "novelty": "new_fact",
    "restates": -1,
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "magnitude": 2,
    "direction": "bullish",
    "audience": "us_equity",
    "scope": "single_name",
    "confidence": 0.9,
    "relevance": trade_relevance().model_dump(mode="json"),
}


class _TaskLM(dspy.BaseLM):  # type: ignore[misc]
    """The seed emits malformed target output; the reflected instruction repairs it."""

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
        if target and _ADVISORY not in rendered:
            text = "not-json"
        else:
            taxonomy = _OTHER_TAXONOMY if target else _PRODUCT_TAXONOMY
            text = canonical_json({"semantics": {**_SEMANTICS, "taxonomy": taxonomy}})
        return dspy.LMResponse.from_text(
            text,
            model=self.model,
            usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            cost=0,
        )


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
        self.requests.append(request)
        rendered = str(request.messages)
        if _ADVISORY in rendered and '"title":"taxonomy-target 11"' in rendered:
            response = dspy.LMResponse.from_text(
                "{",
                model=self.model,
                usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                cost=0.000003,
            )
            response.outputs[0] = response.output.model_copy(update={"finish_reason": "length", "truncated": True})
            return response
        target = '"title":"taxonomy-target' in rendered
        taxonomy = _OTHER_TAXONOMY if target and _ADVISORY in rendered else _PRODUCT_TAXONOMY
        return dspy.LMResponse.from_text(
            canonical_json({"semantics": {**_SEMANTICS, "taxonomy": taxonomy}}),
            model=self.model,
            usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            cost=0,
        )


class _CandidateInvalidTaskLM(_TaskLM):
    """Stable completes; the reflected instruction emits one typed-invalid held-out answer."""

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        self.requests.append(request)
        rendered = str(request.messages)
        target = '"title":"taxonomy-target' in rendered
        taxonomy = _OTHER_TAXONOMY if target and _ADVISORY in rendered else _PRODUCT_TAXONOMY
        semantics = {**_SEMANTICS, "taxonomy": taxonomy}
        if _ADVISORY in rendered and '"title":"taxonomy-target 11"' in rendered:
            semantics = {**semantics, "scope": "world"}
        return dspy.LMResponse.from_text(
            canonical_json({"semantics": semantics}),
            model=self.model,
            usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            cost=0,
        )


def _episode(index: int, *, target: bool) -> DevelopmentEpisode:
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
    stable_taxonomy = news_taxonomy(**_PRODUCT_TAXONOMY, source_authority="reputable_secondary")
    return DevelopmentEpisode(
        case_id=f"{index:064x}",
        cluster_id=f"{index:064x}",
        stratum="taxonomy_failure" if target else "taxonomy_control",
        context=TriageContext.from_card(card, watchlist=(), told_rows=[], now_ms=opened_at_ms, queue_lag_ms=0),
        accepted_review={
            "should_push": "uncertain",
            "dimensions": {},
            "novelty": {"judgment": "uncertain", "duplicate_of": ""},
            "expected": {},
            "expected_correction": "",
            "first_bad_owner_explicit": "taxonomy" if target else None,
            "first_bad_owner": "taxonomy" if target else None,
            "evidence_refs": ["source#taxonomy"] if target else [],
            "taxonomy": _OTHER_TAXONOMY if target else _PRODUCT_TAXONOMY,
        },
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
        ),
    )


def _corpus() -> tuple[DevelopmentEpisode, ...]:
    return tuple(_episode(index, target=index % 2 == 1) for index in range(1, 13))


def _models() -> tuple[Any, Any, _TaskLM, _ReflectionLM]:
    task_delegate = _TaskLM()
    reflection_delegate = _ReflectionLM()
    ledger = LMCallLedger(max_calls_per_predictor=None, max_calls_per_route=None, max_calls_per_scope=None)
    task = build_task_lm(
        model_name="openai/scripted-task",
        api_key="k",
        api_base="https://scripted-task.invalid/v1",
        timeout=20.0,
        max_tokens=1_200,
        ledger=ledger,
        delegate=task_delegate,
    )
    reflection = build_reflection_lm(
        model_name="openai/scripted-reflection",
        api_key="k",
        api_base="https://scripted-reflection.invalid/v1",
        ledger=ledger,
        delegate=reflection_delegate,
    )
    return task, reflection, task_delegate, reflection_delegate


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


def _selection_fixture() -> tuple[Any, set[int], str, str, str]:
    plan = build_gepa_objective_plan(_corpus())
    controls = {
        index
        for index, episode in enumerate(plan.development_selection_episodes)
        if episode.case_id in set(plan.control_case_ids)
    }
    stable_instruction = load_stable_program_artifact().event_semantics_instruction
    return (
        plan,
        controls,
        stable_instruction,
        stable_instruction + "\n\nCandidate one.",
        stable_instruction + "\n\nCandidate two.",
    )


def test_candidate_zero_truncation_refuses_the_run_instead_of_becoming_the_quality_baseline() -> None:
    task, reflection, _task_delegate, _reflection_delegate = _models()
    plan, _controls, stable, candidate, _unused = _selection_fixture()
    val_count = len(plan.development_selection_episodes)
    rows = [dict.fromkeys(range(val_count), 1.0) for _ in range(2)]
    rows[0][0] = float(-(len(plan.train_episodes) + 1))

    with pytest.raises(ValueError, match=r"^news_program_compile_candidate_zero_incomplete$"):
        run_gepa(
            base_program=load_stable_program_artifact(),
            episodes=_corpus(),
            task_lm=task,
            reflection_lm=reflection,
            max_metric_calls=40,
            seed=456,
            review_rubric_version=REVIEW_RUBRIC_VERSION,
            compile_fn=_synthetic_compile(
                instructions=(stable, candidate),
                aggregate_scores=(0.2, 0.8),
                validation_subscores=tuple(rows),
            ),
        )


def test_candidate_controls_must_be_gold_correct_not_merely_better_than_candidate_zero() -> None:
    task, reflection, _task_delegate, _reflection_delegate = _models()
    plan, controls, stable, candidate, _unused = _selection_fixture()
    val_count = len(plan.development_selection_episodes)
    rows = [dict.fromkeys(range(val_count), 1.0) for _ in range(2)]
    for index in controls:
        rows[0][index] = 0.5
        rows[1][index] = 0.75

    with pytest.raises(GepaNoProgramChange) as caught:
        run_gepa(
            base_program=load_stable_program_artifact(),
            episodes=_corpus(),
            task_lm=task,
            reflection_lm=reflection,
            max_metric_calls=40,
            seed=456,
            review_rubric_version=REVIEW_RUBRIC_VERSION,
            compile_fn=_synthetic_compile(
                instructions=(stable, candidate),
                aggregate_scores=(0.5, 0.8),
                validation_subscores=tuple(rows),
            ),
        )

    selection = caught.value.result.metric["taxonomy_selection_score"]
    assert selection["gepa_best_control_failure_n"] == len(controls)
    assert selection["tracefold_admitted_candidate_index"] is None


def test_tracefold_admits_the_highest_qualified_public_candidate_not_gepa_best() -> None:
    task, reflection, _task_delegate, _reflection_delegate = _models()
    plan, controls, stable, candidate_one, candidate_two = _selection_fixture()
    val_count = len(plan.development_selection_episodes)
    rows = [dict.fromkeys(range(val_count), 1.0) for _ in range(3)]
    rows[1][next(iter(controls))] = 0.0

    result = run_gepa(
        base_program=load_stable_program_artifact(),
        episodes=_corpus(),
        task_lm=task,
        reflection_lm=reflection,
        max_metric_calls=40,
        seed=456,
        review_rubric_version=REVIEW_RUBRIC_VERSION,
        compile_fn=_synthetic_compile(
            instructions=(stable, candidate_one, candidate_two),
            aggregate_scores=(0.5, 0.9, 0.8),
            validation_subscores=tuple(rows),
        ),
    )

    assert result.patch.event_semantics_instruction == candidate_two
    assert result.public_result["gepa_best_index"] == 1
    assert result.public_result["tracefold_admitted_index"] == 2
    assert result.metric["taxonomy_selection_score"]["gepa_best_control_failure_n"] == 1


def test_real_gepa_uses_one_native_predict_and_returns_public_trajectory() -> None:
    task, reflection, task_delegate, reflection_delegate = _models()
    stable = load_stable_program_artifact()

    result = run_gepa(
        base_program=stable,
        episodes=_corpus(),
        task_lm=task,
        reflection_lm=reflection,
        max_metric_calls=40,
        seed=456,
        review_rubric_version=REVIEW_RUBRIC_VERSION,
    )

    assert result.patch.event_semantics_instruction == _ADVISORY
    assert result.metric["schema"] == "tracefold.news.taxonomy_gepa_metric.v3"
    assert result.patch.reader_card_instruction == stable.reader_card_instruction
    assert result.metric["taxonomy_selection_score"]["delta"]["taxonomy_overall"] > 0
    assert result.metric["taxonomy_selection_score"]["tracefold_admitted_control_failure_n"] == 0
    change = result.metric["instruction_change"]
    assert change["event_semantics"]["changed"] is True
    assert change["event_semantics"]["estimated_token_growth"] < 0
    assert _ADVISORY in change["event_semantics"]["unified_diff"]
    assert change["reader_card"] == {
        "instruction_sha256": hashlib.sha256(stable.reader_card_instruction.encode()).hexdigest(),
        "unchanged": True,
    }
    assert result.public_result["candidate_count"] >= 2
    assert result.public_result["gepa_best_index"] != 0
    assert result.public_result["tracefold_admitted_index"] != 0
    assert result.public_result["validation_aggregate_objective_scores"]
    assert task_delegate.requests
    assert all("semantics_json" not in str(request.messages) for request in task_delegate.requests)
    assert reflection_delegate.requests
    reflection_text = str([request.messages for request in reflection_delegate.requests])
    assert "parse the output as per the expected output format" in reflection_text
    assert "not-json" in reflection_text


def test_run_gepa_rejects_a_non_native_detailed_result() -> None:
    task, reflection, task_delegate, reflection_delegate = _models()

    def fake_compile(student: dspy.Module, **_kwargs: Any) -> dspy.Module:
        student.detailed_results = object()
        return student

    with pytest.raises(ValueError, match="news_program_compile_detailed_results_missing"):
        run_gepa(
            base_program=load_stable_program_artifact(),
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


def test_candidate_task_truncation_is_scored_unsafe_without_terminating_gepa() -> None:
    ledger = LMCallLedger(max_calls_per_predictor=None, max_calls_per_route=None, max_calls_per_scope=None)
    task_delegate = _CandidateTruncatedTaskLM()
    reflection_delegate = _ReflectionLM()
    task = build_task_lm(
        model_name=task_delegate.model,
        api_key="k",
        api_base="https://scripted-task.invalid/v1",
        timeout=20.0,
        max_tokens=1_200,
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

    with pytest.raises(GepaNoProgramChange) as caught:
        run_gepa(
            base_program=load_stable_program_artifact(),
            episodes=_corpus(),
            task_lm=metered_task,
            reflection_lm=metered_reflection,
            max_metric_calls=40,
            seed=456,
            review_rubric_version=REVIEW_RUBRIC_VERSION,
        )

    result = caught.value.result
    assert result.public_result["candidate_count"] >= 2
    assert result.public_result["gepa_best_index"] == 0
    assert result.public_result["tracefold_admitted_index"] is None
    assert any(
        score < 0
        for candidate_scores in result.public_result["validation_subscores"][1:]
        for score in candidate_scores.values()
    )
    assert result.patch.event_semantics_instruction == load_stable_program_artifact().event_semantics_instruction
    assert meter.task_model_calls > 1
    assert meter.imputed_cost_calls == 0
    assert meter.first_terminal_error is None
    assert metered_task.transport_failures == 0
    truncated_indexes = [
        index
        for index, receipt in enumerate(ledger.receipts)
        if receipt.error_code == "news_program_lm_output_truncated"
    ]
    assert len(truncated_indexes) == 1
    truncated_index = truncated_indexes[0]
    truncated = ledger.receipts[truncated_index]
    assert truncated.terminal_disposition == "provider_success"
    assert (truncated.input_tokens, truncated.output_tokens, truncated.total_tokens) == (11, 7, 18)
    assert truncated.provider_cost_microusd == 3
    assert any(receipt.model_binding == "task" for receipt in ledger.receipts[truncated_index + 1 :])


def test_candidate_typed_invalid_output_keeps_gepa_batch_aligned() -> None:
    ledger = LMCallLedger(max_calls_per_predictor=None, max_calls_per_route=None, max_calls_per_scope=None)
    task_delegate = _CandidateInvalidTaskLM()
    reflection_delegate = _ReflectionLM()
    task = build_task_lm(
        model_name=task_delegate.model,
        api_key="k",
        api_base="https://scripted-task.invalid/v1",
        timeout=20.0,
        max_tokens=1_200,
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

    result = run_gepa(
        base_program=load_stable_program_artifact(),
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
    assert task_delegate.requests[invalid_index + 1 :]
