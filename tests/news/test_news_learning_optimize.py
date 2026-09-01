"""Issue #456: the offline entry point refuses unready data before provider setup."""

from __future__ import annotations

from typing import cast

import dspy  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.contracts import DevelopmentDatasetRef, OptimizationBudget, PromptPatchV1
from tracefold.news.learning.optimizer import FrozenDevelopmentDataset, OptimizationConfig, optimize
from tracefold.news.program.artifact import load_stable_program_artifact

from .test_news_program_gepa_real import _corpus

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


def _budget() -> OptimizationBudget:
    return OptimizationBudget(
        max_metric_calls=40,
        max_task_model_calls=100,
        max_reflection_model_calls=10,
        max_cost_microusd=100_000,
        max_call_cost_microusd=1_000,
        max_wall_clock_seconds=3_600,
        seed=456,
    )


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
    assert "development_calibration_missing" in result.report.reasons
    assert result.report.model_identities == {}
    assert result.report.usage["task_model_calls"] == 0
    assert result.report.usage["reflection_model_calls"] == 0
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


def test_prompt_patch_write_set_remains_exactly_two_instructions() -> None:
    stable = load_stable_program_artifact()

    with pytest.raises(ValidationError):
        PromptPatchV1.model_validate(
            {
                "event_semantics_instruction": stable.event_semantics_instruction,
                "reader_card_instruction": stable.reader_card_instruction,
                "policy": {"similarity_max": 0.5},
            }
        )
