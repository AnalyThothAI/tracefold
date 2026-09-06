"""Small unit contracts around the taxonomy-only GEPA seam (#501)."""

from __future__ import annotations

from typing import Any, Literal, cast

import dspy  # type: ignore[import-untyped]
import pytest
from pydantic import BaseModel

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.contracts import (
    REFLECTION_MAX_TOKENS,
    REFLECTION_TIMEOUT_SECONDS,
    ModelExecutionIdentity,
    OptimizationBudget,
)
from tracefold.news.learning.optimizer import (
    OptimizationBudgetExceeded,
    OptimizationRunTerminated,
    _BudgetMeter,
    _DspyTaxonomyMetric,
    _LearningTaxonomy,
    _MeteredLearningLM,
    gepa_metric_call_ceiling,
    optimizer_config_receipt,
    optimizer_constructor,
    resolve_auto_metric_calls,
)
from tracefold.news.program.lm import LMOutputTruncatedError
from tracefold.news.taxonomy import EVENT_FAMILY_DEFINITIONS, ModelTaxonomyV1


class _RoleLM(dspy.BaseLM):  # type: ignore[misc]
    forward_contract = "typed_lm"

    def __init__(self, role: Literal["task", "reflection"]) -> None:
        super().__init__(f"openai/{role}", cache=False, num_retries=0)
        max_tokens = REFLECTION_MAX_TOKENS if role == "reflection" else 1_000
        timeout = REFLECTION_TIMEOUT_SECONDS if role == "reflection" else 30
        self.tracefold_compiler_endpoint_identity = ModelExecutionIdentity.issue(
            role=role,
            model=f"openai/{role}",
            api_base=f"https://{role}.invalid/v1",
            max_output_tokens=max_tokens,
            timeout_seconds=timeout,
            temperature=0 if role == "task" else 1,
            model_kwargs={},
        )

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        del request
        raise AssertionError("provider call not expected")


class _TruncatedRoleLM(_RoleLM):
    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        del request
        raise LMOutputTruncatedError("[task] news_program_lm_output_truncated")


class _OtherConfig(BaseModel):
    value: int


class _UnrelatedValidationPredictor(dspy.Module):  # type: ignore[misc]
    def forward(self, *, evidence_json: str) -> dspy.Prediction:
        del evidence_json
        _OtherConfig.model_validate({"value": "not-an-integer"})
        raise AssertionError("validation must fail")


class _TaxonomyInvalidPredictor(dspy.Module):  # type: ignore[misc]
    def forward(self, *, evidence_json: str) -> dspy.Prediction:
        del evidence_json
        ModelTaxonomyV1.model_validate(
            {"event_family": "whale", "change_state": "unknown", "assertion_status": "unknown"}
        )
        raise AssertionError("validation must fail")


def _taxonomy(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "subject_codes": ["medtop:20000205"],
        "event_family": "product_service_change",
        "change_state": "announced",
        "assertion_status": "confirmed",
    }
    values.update(overrides)
    return values


def _budget(**overrides: Any) -> OptimizationBudget:
    values: dict[str, Any] = {
        "max_metric_calls": 10,
        "max_task_model_calls": 1,
        "max_reflection_model_calls": 1,
        "max_cost_microusd": 10,
        "max_call_cost_microusd": 10,
        "max_wall_clock_seconds": 60,
        "seed": 456,
    }
    values.update(overrides)
    return OptimizationBudget(**values)


def test_direct_metric_is_the_mean_of_the_four_taxonomy_axes() -> None:
    gold = dspy.Example(gold_taxonomy=_taxonomy())
    prediction = dspy.Prediction(
        taxonomy=ModelTaxonomyV1.model_validate(_taxonomy(event_family="other", assertion_status="rumor"))
    )

    result = _DspyTaxonomyMetric()(gold, prediction)

    assert result.score == 0.5
    assert result.objective_scores == {
        "subject_codes_set_f1": 1.0,
        "event_family_accuracy": 0.0,
        "change_state_accuracy": 1.0,
        "assertion_status_accuracy": 0.0,
        "four_axis_exact_accuracy": 0.0,
    }


def test_metric_feedback_quotes_the_codebook_definitions_and_the_matching_precedence_rule() -> None:
    gold = dspy.Example(gold_taxonomy=_taxonomy(change_state="effective", assertion_status="confirmed"))
    prediction = dspy.Prediction(taxonomy=_taxonomy(change_state="reported", assertion_status="claimed"))

    feedback = _DspyTaxonomyMetric()(gold, prediction).feedback

    assert "change_state: expected=effective (the change is live, completed or legally in force," in feedback
    assert "predicted=reported (a published measurement" in feedback
    assert "rule (change_state): reported is narrow" in feedback
    assert "rule (assertion_status): confirmed does not require a recognized source_authority" in feedback
    assert "source_authority=" not in feedback


def test_metric_feedback_names_missing_and_extra_subjects_with_their_glossary_labels() -> None:
    gold = dspy.Example(gold_taxonomy=_taxonomy(subject_codes=["medtop:20000178"]))
    prediction = dspy.Prediction(taxonomy=_taxonomy(subject_codes=["medtop:20001279"], event_family="other"))

    feedback = _DspyTaxonomyMetric()(gold, prediction).feedback

    assert "missing subjects: medtop:20000178 (corporate earnings)" in feedback
    assert "extra subjects: medtop:20001279 (cryptocurrency)" in feedback
    assert f"predicted=other ({EVENT_FAMILY_DEFINITIONS['other']})" in feedback


def test_optimizer_receipt_records_native_budget_and_disabled_format_feedback() -> None:
    task = _RoleLM("task")
    reflection = _RoleLM("reflection")
    constructor = optimizer_constructor(max_metric_calls=40, seed=456, train_count=8)

    assert constructor["reflection_minibatch_size"] == 6
    assert constructor["max_metric_calls"] == 40 and "auto" not in constructor
    receipt = optimizer_config_receipt(
        constructor=constructor,
        resolved_metric_calls=40,
        task_lm=task,
        reflection_lm=reflection,
        metric_sha256=canonical_sha({"metric": "taxonomy"}),
        example_count=12,
        train_count=8,
        val_count=4,
    )

    assert receipt["schema"] == "tracefold.news.compile_optimizer_config_receipt.v8"
    assert receipt["optimizer"]["evaluator"] == "LearningTaxonomy(NativeNewsProgram.taxonomy) on one explicit task LM"
    assert receipt["optimizer"]["add_format_failure_as_feedback"] is False
    assert receipt["instruction_proposer"] is None
    assert receipt["admission"] == "gepa_best_idx_strictly_above_seed"
    assert "instruction_growth_budget" not in receipt
    assert receipt["constructor_scalar_arguments"]["auto"] is None
    assert receipt["constructor_scalar_arguments"]["max_metric_calls"] == 40
    assert set(receipt["model_identities"]) == {"task", "reflection"}
    assert (
        gepa_metric_call_ceiling(
            max_metric_calls=40,
            optimizer_config=receipt,
            expected_example_count=12,
        )
        == 50
    )


def test_auto_light_passes_through_and_resolves_to_dspys_own_budget() -> None:
    constructor = optimizer_constructor(auto="light", seed=456, train_count=8)
    resolved = resolve_auto_metric_calls("light", val_count=4)

    assert constructor["auto"] == "light" and "max_metric_calls" not in constructor
    assert resolved == dspy.GEPA.auto_budget(None, num_preds=1, num_candidates=6, valset_size=4)
    receipt = optimizer_config_receipt(
        constructor=constructor,
        resolved_metric_calls=resolved,
        task_lm=_RoleLM("task"),
        reflection_lm=_RoleLM("reflection"),
        metric_sha256=canonical_sha({"metric": "taxonomy"}),
        example_count=12,
        train_count=8,
        val_count=4,
    )
    assert receipt["constructor_scalar_arguments"]["auto"] == "light"
    assert receipt["constructor_scalar_arguments"]["max_metric_calls"] == resolved
    assert gepa_metric_call_ceiling(max_metric_calls=resolved, optimizer_config=receipt, expected_example_count=12) == (
        resolved + 4 + 6
    )


def test_budget_forms_are_exactly_one_of_auto_or_max_metric_calls() -> None:
    with pytest.raises(ValueError, match="exactly_one_of_auto_or_max_metric_calls"):
        optimizer_constructor(seed=456, train_count=8)
    with pytest.raises(ValueError, match="exactly_one_of_auto_or_max_metric_calls"):
        optimizer_constructor(auto="light", max_metric_calls=40, seed=456, train_count=8)
    with pytest.raises(ValueError, match="news_program_compile_auto_budget_unknown"):
        resolve_auto_metric_calls("extreme", val_count=4)
    with pytest.raises(ValueError, match="exactly_one_of_auto_or_max_metric_calls"):
        _budget(max_metric_calls=None)
    with pytest.raises(ValueError, match="exactly_one_of_auto_or_max_metric_calls"):
        _budget(auto="light")
    assert _budget(auto="medium", max_metric_calls=None).auto == "medium"


def test_budget_meter_reserves_before_a_physical_call() -> None:
    meter = _BudgetMeter(_budget(), imputed_call_cost_microusd=10)

    meter.before("task")
    meter.after(
        "task",
        dspy.LMResponse.from_text(
            "{}",
            model="openai/task",
            usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            cost=0,
        ),
    )
    assert meter.task_input_tokens == 7
    assert meter.task_output_tokens == 3
    assert meter.task_total_tokens == 10
    with pytest.raises(OptimizationBudgetExceeded, match="news_program_compile_task_model_call_budget_exhausted"):
        meter.before("task")


def test_budget_meter_records_an_answer_before_rejecting_its_reported_cost() -> None:
    meter = _BudgetMeter(_budget(max_call_cost_microusd=2), imputed_call_cost_microusd=2)
    response = dspy.LMResponse.from_text(
        "{}",
        model="openai/task",
        usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        cost=0.000003,
    )

    meter.before("task")
    with pytest.raises(OptimizationBudgetExceeded, match="news_program_compile_call_cost_reservation_exceeded"):
        meter.after("task", response)

    assert meter.task_model_calls == 1
    assert meter.task_total_tokens == 18
    assert meter.task_cost_microusd == 3
    assert meter.actual_cost_microusd == 3


def test_budget_meter_refuses_every_call_after_a_terminal_error() -> None:
    meter = _BudgetMeter(_budget(), imputed_call_cost_microusd=10)
    meter.remember_terminal(OptimizationRunTerminated("news_program_compile_task_provider_unavailable"))

    with pytest.raises(OptimizationRunTerminated, match="news_program_compile_task_provider_unavailable"):
        meter.before("task")

    assert meter.task_model_calls == 0


def test_unreceipted_task_truncation_remains_a_run_termination() -> None:
    task = _TruncatedRoleLM("task")
    meter = _BudgetMeter(_budget(), imputed_call_cost_microusd=10)
    metered = _MeteredLearningLM(task, meter=meter, role="task")

    with pytest.raises(OptimizationRunTerminated, match="news_program_compile_task_model_output_truncated"):
        metered(messages=[{"role": "user", "content": "classify"}])

    assert meter.task_model_calls == 1
    assert meter.task_total_tokens == 0
    assert meter.task_cost_microusd == 10
    assert meter.imputed_cost_calls == 1
    assert isinstance(meter.first_terminal_error, OptimizationRunTerminated)
    assert metered.transport_failures == 0


def test_truncated_and_invalid_task_output_score_the_native_failure_score() -> None:
    """#501 D5: no sentinel below the real scale; a failure is `failure_score`, which is 0.0."""

    constructor = optimizer_constructor(max_metric_calls=40, seed=456, train_count=8)
    metric = _DspyTaxonomyMetric()

    truncated = metric(
        dspy.Example(gold_taxonomy=_taxonomy()),
        dspy.Prediction(task_output_failure="news_program_compile_task_model_output_truncated"),
    )
    invalid = metric(
        dspy.Example(gold_taxonomy=_taxonomy()),
        dspy.Prediction(
            task_output_failure="news_program_compile_task_model_output_invalid",
            task_output_feedback="Typed ModelTaxonomyV1 is invalid: event_family",
        ),
    )

    assert constructor["failure_score"] == 0.0
    assert truncated.score == 0.0 and "output truncated" in truncated.feedback
    assert set(truncated.objective_scores.values()) == {0.0}
    assert invalid.score == 0.0
    assert invalid.feedback == "Typed ModelTaxonomyV1 is invalid: event_family"
    assert set(invalid.objective_scores.values()) == {0.0}


def test_reflection_minibatch_never_exceeds_the_trainset() -> None:
    constructor = optimizer_constructor(max_metric_calls=40, seed=456, train_count=4)

    assert constructor["reflection_minibatch_size"] == 4


def test_learning_wrapper_converts_only_its_own_typed_failure() -> None:
    wrapper = _LearningTaxonomy(cast(dspy.Predict, _TaxonomyInvalidPredictor()))

    prediction = wrapper(evidence_json="evidence")

    assert prediction.task_output_failure == "news_program_compile_task_model_output_invalid"
    assert "ModelTaxonomyV1" in prediction.task_output_feedback


def test_learning_wrapper_propagates_unrelated_pydantic_validation() -> None:
    wrapper = _LearningTaxonomy(cast(dspy.Predict, _UnrelatedValidationPredictor()))

    with pytest.raises(ValueError, match="OtherConfig"):
        wrapper(evidence_json="evidence")
