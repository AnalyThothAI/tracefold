"""Issue #456: the offline entry point refuses unready data before provider setup."""

from __future__ import annotations

from typing import Any, Literal, cast

import dspy  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning import optimizer as optimizer_module
from tracefold.news.learning.contracts import DevelopmentDatasetRef, OptimizationBudget, PromptPatchV1
from tracefold.news.learning.objective import build_gepa_objective_plan
from tracefold.news.learning.optimizer import (
    FrozenDevelopmentDataset,
    OptimizationConfig,
    OptimizationRunTerminated,
    build_reflection_lm,
    build_task_lm,
    optimize,
)
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.lm import LMCallContext, LMCallLedger, ScriptedLM
from tracefold.news.program.runtime import PROGRAM_VERSION

from .test_news_program_gepa_real import _corpus, _episode, _synthetic_compile

_DATASET_PAYLOAD = {
    "role": "development",
    "learning_epoch": "bundle_00000000",
    "counts": {},
    "cases": [],
}


def _dataset() -> FrozenDevelopmentDataset:
    episodes = _corpus()
    return FrozenDevelopmentDataset.bind(
        ref=DevelopmentDatasetRef(
            development_dataset_sha256=canonical_sha({"kind": "dataset", "payload": _DATASET_PAYLOAD}),
            episode_projection_root_sha256=canonical_sha([episode.model_dump(mode="json") for episode in episodes]),
            episode_count=len(episodes),
            learning_epoch="bundle_00000000",
            learning_epoch_started_at_ms=1,
            review_rubric_version="news_review_v6",
        ),
        episodes=episodes,
        dataset_payload=_DATASET_PAYLOAD,
        target_runtime_manifest_sha256="a" * 64,
    )


def _budget(**overrides: Any) -> OptimizationBudget:
    values: dict[str, Any] = {
        "max_metric_calls": 40,
        "max_task_model_calls": 100,
        "max_reflection_model_calls": 10,
        "max_cost_microusd": 100_000,
        "max_call_cost_microusd": 1_000,
        "max_wall_clock_seconds": 3_600,
        "seed": 456,
    }
    values.update(overrides)
    return OptimizationBudget(**values)


def _ready_dataset() -> FrozenDevelopmentDataset:
    episode_rows = []
    for index in range(1, 201):
        episode = _episode(index, target=index % 2 == 1)
        review = dict(episode.accepted_review)
        review["should_push"] = "must_push" if index % 2 else "should_hold"
        review["novelty"] = {"judgment": "new_fact", "duplicate_of": ""}
        episode_rows.append(episode.model_copy(update={"accepted_review": review}))
    episodes = tuple(episode_rows)
    payload = {
        **_DATASET_PAYLOAD,
        "counts": {
            "boundary_cluster_n": 30,
            "retention_cluster_n": 100,
            "negative_cluster_n": 50,
            "safety_cluster_n": 1,
            "stratum_n": 3,
        },
    }
    return FrozenDevelopmentDataset.bind(
        ref=DevelopmentDatasetRef(
            development_dataset_sha256=canonical_sha({"kind": "dataset", "payload": payload}),
            episode_projection_root_sha256=canonical_sha([episode.model_dump(mode="json") for episode in episodes]),
            episode_count=len(episodes),
            learning_epoch="bundle_00000000",
            learning_epoch_started_at_ms=1,
            review_rubric_version="news_review_v6",
        ),
        episodes=episodes,
        dataset_payload=payload,
        target_runtime_manifest_sha256="a" * 64,
    )


def _learning_models(*, role: Literal["task", "reflection"]) -> tuple[dspy.BaseLM, dspy.BaseLM, LMCallLedger]:
    truncated = dspy.LMResponse.from_text(
        "{",
        model=f"openai/{role}",
        usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        cost=0.000003,
    )
    truncated.outputs[0] = truncated.output.model_copy(update={"finish_reason": "length", "truncated": True})
    ledger = LMCallLedger()
    task = build_task_lm(
        model_name="openai/task",
        api_key="k",
        api_base="https://task.invalid/v1",
        timeout=20,
        max_tokens=1_200,
        ledger=ledger,
        delegate=ScriptedLM([truncated] if role == "task" else [], model="openai/task"),
    )
    reflection = build_reflection_lm(
        model_name="openai/reflection",
        api_key="k",
        api_base="https://reflection.invalid/v1",
        ledger=ledger,
        delegate=ScriptedLM([truncated] if role == "reflection" else [], model="openai/reflection"),
    )
    return task, reflection, ledger


def test_truncated_reflection_writes_an_exact_terminal_usage_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role: Literal["reflection"] = "reflection"
    task, reflection, ledger = _learning_models(role=role)
    monotonic_values = iter((100.0, 100.0, 100.25))

    def terminate(**kwargs: Any) -> Any:
        lm = kwargs[f"{role}_lm"]
        request = dspy.LMRequest.from_call(
            model=lm.model,
            messages=[{"role": "user", "content": "classify"}],
        )
        with ledger.scope(LMCallContext(PROGRAM_VERSION, "a" * 64, "b" * 64)), pytest.raises(OptimizationRunTerminated):
            lm(request=request)
        raise RuntimeError("upstream evaluator exhausted its error limit")

    monkeypatch.setattr(optimizer_module, "run_gepa", terminate)
    result = optimize(
        _ready_dataset(),
        OptimizationConfig(
            task_lm=task,
            reflection_lm=reflection,
            budget=_budget(),
            now_ms=lambda: 1_800_000_000_000,
            monotonic=lambda: next(monotonic_values),
        ),
    )

    assert result.outcome == "REJECTED"
    assert result.report.reasons == (f"news_program_compile_{role}_model_output_truncated",)
    assert result.candidate is None
    assert result.report.metric is None
    assert result.report.optimizer is None
    assert result.report.gepa_public_result is None
    assert result.report.usage == {
        "schema": "tracefold.news.optimization_usage.v3",
        "task_model_calls": int(role == "task"),
        "reflection_model_calls": int(role == "reflection"),
        "task_cost_microusd": 3 if role == "task" else 0,
        "reflection_cost_microusd": 3 if role == "reflection" else 0,
        "task_input_tokens": 11 if role == "task" else 0,
        "task_output_tokens": 7 if role == "task" else 0,
        "task_cached_tokens": 0,
        "task_total_tokens": 18 if role == "task" else 0,
        "reflection_input_tokens": 11 if role == "reflection" else 0,
        "reflection_output_tokens": 7 if role == "reflection" else 0,
        "reflection_cached_tokens": 0,
        "reflection_total_tokens": 18 if role == "reflection" else 0,
        "total_tokens": 18,
        "wall_clock_ms": 250,
        "imputed_cost_calls": 0,
        "actual_cost_microusd": 3,
        # GEPA did not return its public result, so the exact number of completed metric evaluations is
        # unavailable. Physical model calls remain exact above; zero would falsely claim no evaluation ran.
        "metric_calls": None,
        "transport_failures": 0,
        "transport_retries": 0,
    }


def test_report_keeps_spend_that_exceeds_the_per_call_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    task, reflection, ledger = _learning_models(role="task")

    def terminate(**kwargs: Any) -> Any:
        lm = kwargs["task_lm"]
        request = dspy.LMRequest.from_call(
            model=lm.model,
            messages=[{"role": "user", "content": "classify"}],
        )
        with ledger.scope(LMCallContext(PROGRAM_VERSION, "a" * 64, "b" * 64)):
            lm(request=request)
        raise AssertionError("overspend must terminate the run")

    monkeypatch.setattr(optimizer_module, "run_gepa", terminate)
    result = optimize(
        _ready_dataset(),
        OptimizationConfig(
            task_lm=task,
            reflection_lm=reflection,
            budget=_budget(max_call_cost_microusd=2),
            now_ms=lambda: 1_800_000_000_000,
        ),
    )

    assert result.outcome == "REJECTED"
    assert result.report.reasons == ("news_program_compile_call_cost_reservation_exceeded",)
    assert result.candidate is None
    assert result.report.usage["task_model_calls"] == 1
    assert result.report.usage["task_total_tokens"] == 18
    assert result.report.usage["task_cost_microusd"] == 3
    assert result.report.usage["actual_cost_microusd"] == 3
    assert result.report.usage["transport_failures"] == 0


def _synthetic_result(aggregate_scores: tuple[float, float]) -> Any:
    dataset = _ready_dataset()
    plan = build_gepa_objective_plan(dataset.episodes)
    stable = dataset.parent_program.taxonomy_instruction
    val_count = len(plan.development_selection_episodes)
    rows = tuple(dict.fromkeys(range(val_count), score) for score in aggregate_scores)
    task, reflection, _ledger = _learning_models(role="reflection")
    return optimize(
        dataset,
        OptimizationConfig(
            task_lm=task,
            reflection_lm=reflection,
            budget=_budget(),
            compile_fn=_synthetic_compile(
                instructions=(stable, stable + "\n\nCandidate one."),
                aggregate_scores=aggregate_scores,
                validation_subscores=rows,
            ),
            now_ms=lambda: 1_800_000_000_000,
        ),
    )


def test_a_seed_that_stays_gepa_best_is_a_no_op_with_public_receipts() -> None:
    result = _synthetic_result((0.8, 0.5))

    assert result.outcome == "NO_OP"
    assert result.report.schema_version == "news_optimization_run_report_v4"
    assert result.report.reasons == ("news_program_compile_no_program_change",)
    assert result.report.metric is not None
    selection = result.report.metric["taxonomy_selection_score"]
    assert selection["gepa_best_index"] == 0
    assert selection["admitted"] is False
    assert selection["delta"]["taxonomy_overall"] == 0.0
    assert result.report.gepa_public_result is not None
    assert result.report.gepa_public_result["admitted"] is False
    assert result.candidate is None
    assert result.report.objective["schema"] == "tracefold.news.optimization_objective_summary.v4"
    assert "owner_distribution" not in result.report.objective
    assert result.report.objective["target_predictors"] == ["taxonomy"]


def test_a_strictly_better_gepa_best_advances_with_only_the_taxonomy_instruction_changed() -> None:
    result = _synthetic_result((0.5, 0.8))

    assert result.outcome == "ADVANCE"
    assert result.candidate is not None
    stable = load_stable_program_artifact()
    assert result.candidate.patch.taxonomy_instruction != stable.taxonomy_instruction
    assert result.candidate.patch.event_semantics_instruction == stable.event_semantics_instruction
    assert result.candidate.patch.reader_card_instruction == stable.reader_card_instruction
    assert result.candidate.budget["auto"] is None
    assert result.candidate.budget["max_metric_calls"] == 40


def test_auto_budget_is_carried_into_the_candidate_and_the_optimizer_receipt() -> None:
    dataset = _ready_dataset()
    plan = build_gepa_objective_plan(dataset.episodes)
    stable = dataset.parent_program.taxonomy_instruction
    val_count = len(plan.development_selection_episodes)
    task, reflection, _ledger = _learning_models(role="reflection")

    result = optimize(
        dataset,
        OptimizationConfig(
            task_lm=task,
            reflection_lm=reflection,
            budget=_budget(auto="light", max_metric_calls=None),
            compile_fn=_synthetic_compile(
                instructions=(stable, stable + "\n\nCandidate one."),
                aggregate_scores=(0.5, 0.8),
                validation_subscores=tuple(dict.fromkeys(range(val_count), score) for score in (0.5, 0.8)),
            ),
            now_ms=lambda: 1_800_000_000_000,
        ),
    )

    assert result.outcome == "ADVANCE"
    assert result.report.optimizer is not None
    scalars = result.report.optimizer["constructor_scalar_arguments"]
    assert scalars["auto"] == "light"
    assert scalars["max_metric_calls"] == dspy.GEPA.auto_budget(
        None, num_preds=1, num_candidates=6, valset_size=val_count
    )
    assert result.report.budget["auto"] == "light" and result.report.budget["max_metric_calls"] is None


def test_unready_development_profile_is_a_zero_provider_call_terminal_report() -> None:
    touched = False

    def forbidden_compile(*_args: object, **_kwargs: object) -> dspy.Module:
        nonlocal touched
        touched = True
        raise AssertionError("compile must not run")

    result = optimize(
        _dataset(),
        OptimizationConfig(
            task_lm=cast(dspy.BaseLM, object()),
            reflection_lm=cast(dspy.BaseLM, object()),
            budget=_budget(),
            compile_fn=forbidden_compile,
            now_ms=lambda: 1_800_000_000_000,
        ),
    )

    assert result.outcome == "REJECTED"
    assert touched is False
    assert result.report.objective["compilable"] is True
    assert result.report.objective["development_profile"]["ready"] is False
    assert "development_boundary_cluster_n_insufficient" in result.report.reasons
    assert not any("calibration" in reason for reason in result.report.reasons)
    assert result.report.model_identities == {}
    assert result.report.usage["schema"] == "tracefold.news.optimization_usage.v3"
    assert result.report.usage["task_model_calls"] == 0
    assert result.report.usage["reflection_model_calls"] == 0
    assert result.report.usage["metric_calls"] == 0
    assert result.report.metric is None
    assert result.candidate is None


def test_dataset_ref_cannot_name_a_different_episode_projection() -> None:
    dataset = _dataset()
    bad_ref = dataset.ref.model_copy(update={"episode_projection_root_sha256": "f" * 64})

    with pytest.raises(ValueError, match="news_learning_optimize_dataset_projection_root_mismatch"):
        FrozenDevelopmentDataset(
            ref=bad_ref,
            episodes=dataset.episodes,
            parent_program=dataset.parent_program,
            target_runtime_manifest_sha256=dataset.target_runtime_manifest_sha256,
            dataset_payload=dataset.dataset_payload,
        )


def test_prompt_patch_write_set_remains_exactly_three_instructions() -> None:
    stable = load_stable_program_artifact()
    instructions = {
        "event_semantics_instruction": stable.event_semantics_instruction,
        "taxonomy_instruction": stable.taxonomy_instruction,
        "reader_card_instruction": stable.reader_card_instruction,
    }

    assert PromptPatchV1.model_validate(instructions).changes(stable) is False
    with pytest.raises(ValidationError):
        PromptPatchV1.model_validate({**instructions, "policy": {"similarity_max": 0.5}})
    with pytest.raises(ValidationError):
        PromptPatchV1.model_validate(
            {key: value for key, value in instructions.items() if key != "taxonomy_instruction"}
        )
