"""Small unit contracts around the taxonomy-only GEPA seam."""

from __future__ import annotations

from typing import Any, Literal, cast

import dspy  # type: ignore[import-untyped]
import pytest
from pydantic import BaseModel

from tests.support.news_judgment import trade_relevance
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.contracts import (
    REFLECTION_MAX_TOKENS,
    REFLECTION_TIMEOUT_SECONDS,
    ModelExecutionIdentity,
    OptimizationBudget,
)
from tracefold.news.learning.optimizer import (
    InstructionGrowthBudget,
    OptimizationBudgetExceeded,
    OptimizationRunTerminated,
    _BudgetMeter,
    _DspyTaxonomyMetric,
    _LearningEventSemantics,
    _MeteredLearningLM,
    gepa_metric_call_ceiling,
    optimizer_config_receipt,
    optimizer_constructor,
)
from tracefold.news.program.lm import LMOutputTruncatedError
from tracefold.news.program.signatures import EventSemantics


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


def _taxonomy(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "subject_codes": ["medtop:20000205"],
        "event_family": "product_service_change",
        "change_state": "announced",
        "assertion_status": "confirmed",
    }
    values.update(overrides)
    return values


def _semantics(taxonomy: dict[str, Any]) -> EventSemantics:
    return EventSemantics.model_validate(
        {
            "novelty": "new_fact",
            "restates": -1,
            "assets": [{"symbol": "TSLA", "role": "primary"}],
            "direction": "bullish",
            "scope": "single_name",
            "magnitude": 2,
            "confidence": 0.9,
            "audience": "us_equity",
            "taxonomy": taxonomy,
            "relevance": trade_relevance().model_dump(mode="json"),
        }
    )


def test_direct_metric_is_the_mean_of_the_four_taxonomy_axes() -> None:
    gold = dspy.Example(gold_taxonomy=_taxonomy())
    prediction = dspy.Prediction(semantics=_semantics(_taxonomy(event_family="other", assertion_status="rumor")))

    result = _DspyTaxonomyMetric(task_output_failure_score=-9.0)(gold, prediction)

    assert result.score == 0.5
    assert result.objective_scores == {
        "subject_codes_set_f1": 1.0,
        "event_family_accuracy": 0.0,
        "change_state_accuracy": 1.0,
        "assertion_status_accuracy": 0.0,
        "four_axis_exact_accuracy": 0.0,
    }


def test_optimizer_receipt_exposes_native_format_feedback_and_no_hidden_selector() -> None:
    task = _RoleLM("task")
    reflection = _RoleLM("reflection")
    constructor = optimizer_constructor(max_metric_calls=40, seed=456, train_count=8)

    assert constructor["reflection_minibatch_size"] == 6
    receipt = optimizer_config_receipt(
        constructor=constructor,
        task_lm=task,
        reflection_lm=reflection,
        growth_budget=InstructionGrowthBudget.from_seeds({"event_semantics": "seed"}),
        metric_sha256=canonical_sha({"metric": "taxonomy"}),
        example_count=12,
        train_count=8,
        val_count=4,
    )

    assert receipt["schema"] == "tracefold.news.compile_optimizer_config_receipt.v7"
    assert receipt["optimizer"]["evaluator"] == (
        "LearningEventSemantics(NativeNewsProgram.event_semantics) on one explicit task LM"
    )
    assert receipt["optimizer"]["add_format_failure_as_feedback"] is True
    assert receipt["instruction_proposer"] is None
    assert "component_selector" not in receipt
    assert set(receipt["model_identities"]) == {"task", "reflection"}
    assert (
        gepa_metric_call_ceiling(
            max_metric_calls=40,
            optimizer_config=receipt,
            expected_example_count=12,
        )
        == 50
    )


def test_shared_instruction_growth_budget_cannot_ratchet() -> None:
    budget = InstructionGrowthBudget.from_seeds({"event_semantics": "seed"}, max_growth_tokens=1)

    assert budget.over({"event_semantics": "seed"}) is None
    refusal = budget.over({"event_semantics": "x" * 100})
    assert refusal is not None
    assert refusal[0] == "news_program_instruction_growth_budget"
    assert "seed" in refusal[1]


def test_budget_meter_reserves_before_a_physical_call() -> None:
    meter = _BudgetMeter(
        OptimizationBudget(
            max_metric_calls=10,
            max_task_model_calls=1,
            max_reflection_model_calls=1,
            max_cost_microusd=10,
            max_call_cost_microusd=10,
            max_wall_clock_seconds=60,
            seed=456,
        ),
        imputed_call_cost_microusd=10,
    )

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
    meter = _BudgetMeter(
        OptimizationBudget(
            max_metric_calls=10,
            max_task_model_calls=1,
            max_reflection_model_calls=1,
            max_cost_microusd=10,
            max_call_cost_microusd=2,
            max_wall_clock_seconds=60,
            seed=456,
        ),
        imputed_call_cost_microusd=2,
    )
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
    meter = _BudgetMeter(
        OptimizationBudget(
            max_metric_calls=10,
            max_task_model_calls=1,
            max_reflection_model_calls=1,
            max_cost_microusd=10,
            max_call_cost_microusd=10,
            max_wall_clock_seconds=60,
            seed=456,
        ),
        imputed_call_cost_microusd=10,
    )
    meter.remember_terminal(OptimizationRunTerminated("news_program_compile_task_provider_unavailable"))

    with pytest.raises(OptimizationRunTerminated, match="news_program_compile_task_provider_unavailable"):
        meter.before("task")

    assert meter.task_model_calls == 0


def test_unreceipted_task_truncation_remains_a_run_termination() -> None:
    task = _TruncatedRoleLM("task")
    meter = _BudgetMeter(
        OptimizationBudget(
            max_metric_calls=10,
            max_task_model_calls=1,
            max_reflection_model_calls=1,
            max_cost_microusd=10,
            max_call_cost_microusd=10,
            max_wall_clock_seconds=60,
            seed=456,
        ),
        imputed_call_cost_microusd=10,
    )
    metered = _MeteredLearningLM(task, meter=meter, role="task")

    with pytest.raises(OptimizationRunTerminated, match="news_program_compile_task_model_output_truncated"):
        metered(messages=[{"role": "user", "content": "classify"}])

    assert meter.task_model_calls == 1
    assert meter.task_total_tokens == 0
    assert meter.task_cost_microusd == 10
    assert meter.imputed_cost_calls == 1
    assert isinstance(meter.first_terminal_error, OptimizationRunTerminated)
    assert metered.transport_failures == 0


def test_task_output_failure_score_makes_one_incomplete_output_lose_to_a_complete_candidate() -> None:
    constructor = optimizer_constructor(max_metric_calls=40, seed=456, train_count=8)
    failure_score = -9.0
    metric = _DspyTaxonomyMetric(task_output_failure_score=failure_score)

    result = metric(
        dspy.Example(gold_taxonomy=_taxonomy()),
        dspy.Prediction(task_output_failure="news_program_compile_task_model_output_truncated"),
    )

    assert constructor["failure_score"] == 0.0
    assert result.score == failure_score
    assert failure_score + 7 < 0
    assert set(result.objective_scores.values()) == {failure_score}


def test_reflection_minibatch_never_exceeds_the_trainset() -> None:
    constructor = optimizer_constructor(max_metric_calls=40, seed=456, train_count=4)

    assert constructor["reflection_minibatch_size"] == 4


def test_typed_invalid_task_output_keeps_the_existing_zero_score() -> None:
    result = _DspyTaxonomyMetric(task_output_failure_score=-9.0)(
        dspy.Example(gold_taxonomy=_taxonomy()),
        dspy.Prediction(
            task_output_failure="news_program_compile_task_model_output_invalid",
            task_output_feedback="Typed EventSemantics is invalid: scope",
        ),
    )

    assert result.score == 0.0
    assert result.feedback == "Typed EventSemantics is invalid: scope"
    assert set(result.objective_scores.values()) == {0.0}


def test_learning_wrapper_propagates_unrelated_pydantic_validation() -> None:
    wrapper = _LearningEventSemantics(cast(dspy.Predict, _UnrelatedValidationPredictor()))

    with pytest.raises(ValueError, match="OtherConfig"):
        wrapper(evidence_json="evidence")
