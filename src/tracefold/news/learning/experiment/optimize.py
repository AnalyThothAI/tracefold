"""Run the shared GEPA core over a frozen snapshot, in process, and produce nothing promotable.

What comes out is an `ExperimentCandidate`: a parent identity and two instructions, scored on the half
the optimizer never trained on. It is deliberately not a `CompileRecordV1` and cannot become one — the
only thing that produces a release candidate is the trusted compiler, in a sealed container, against a
metered proxy. A winner here is a reason to spend that container, not a substitute for it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...artifact_identity import canonical_sha
from ...program.artifact import ProgramStrategyArtifactV1
from ..compiler.gepa import GepaRunResult, run_gepa
from ..metric import DevelopmentEpisode
from .compare import baseline_cases
from .run import ExperimentCase

EXPERIMENT_CANDIDATE_SCHEMA: Literal["tracefold.news.experiment_candidate.v1"] = (
    "tracefold.news.experiment_candidate.v1"
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ExperimentCandidate(BaseModel):
    """A proposal an operator may look at, and no gate may accept."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["tracefold.news.experiment_candidate.v1"] = EXPERIMENT_CANDIDATE_SCHEMA
    # Named so nobody mistakes this for release evidence, in the type and in the JSON.
    promotable: Literal[False] = False
    run_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_program_sha256: str = Field(pattern=_SHA256_PATTERN)
    event_semantics_instruction: str
    reader_card_instruction: str
    task_model: dict[str, Any]
    reflection_model: dict[str, Any]
    metric_judge_model: dict[str, Any]
    optimizer: dict[str, Any]
    metric: dict[str, Any]
    split: dict[str, Any]
    trajectory: dict[str, Any]
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    metric_calls: int = Field(ge=0)
    train_count: int = Field(gt=0)
    val_count: int = Field(gt=0)
    experiment_candidate_sha256: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def issue(cls, **values: Any) -> ExperimentCandidate:
        draft = cls.model_construct(experiment_candidate_sha256="0" * 64, **values)
        payload = draft.model_dump(mode="json", exclude={"experiment_candidate_sha256"})
        return cls(**values, experiment_candidate_sha256=canonical_sha(payload))

    @model_validator(mode="after")
    def _identity_is_exact(self) -> ExperimentCandidate:
        expected = canonical_sha(self.model_dump(mode="json", exclude={"experiment_candidate_sha256"}))
        if self.experiment_candidate_sha256 != expected:
            raise ValueError("news_experiment_candidate_hash_mismatch")
        return self


def accepted_episodes(cases: Sequence[ExperimentCase]) -> tuple[DevelopmentEpisode, ...]:
    """Only cases a human accepted, projected exactly as the scoring side projects them.

    Through `baseline_cases`, not a second `model_validate`: the number this optimization maximizes and the
    number the comparison reports have to come off the same objects, which is the same reason `run_gepa` is
    shared one layer down.
    """

    return tuple(case.episode for case in baseline_cases(cases))


def optimize_snapshot(
    *,
    run_sha256: str,
    base_program: ProgramStrategyArtifactV1,
    cases: Sequence[ExperimentCase],
    task_lm: dspy.LM,
    reflection_lm: dspy.LM,
    judge: Any,
    max_metric_calls: int,
    seed: int,
    review_rubric_version: str,
) -> ExperimentCandidate:
    """Optimize against accepted truth only, through the same core a trusted compile runs."""

    episodes = accepted_episodes(cases)
    if not episodes:
        raise ValueError("news_experiment_optimize_requires_accepted_reviews")
    result: GepaRunResult = run_gepa(
        base_program=base_program,
        episodes=episodes,
        task_lm=task_lm,
        reflection_lm=reflection_lm,
        judge=judge,
        max_metric_calls=max_metric_calls,
        seed=seed,
        review_rubric_version=review_rubric_version,
    )
    identities = dict(result.optimizer_config["model_identities"])
    return ExperimentCandidate.issue(
        run_sha256=run_sha256,
        parent_program_sha256=base_program.program_sha256,
        event_semantics_instruction=result.patch.event_semantics_instruction,
        reader_card_instruction=result.patch.reader_card_instruction,
        task_model=dict(identities["task"]),
        reflection_model=dict(identities["reflection"]),
        metric_judge_model=dict(getattr(judge, "identity", {})),
        optimizer=result.optimizer_config,
        metric=result.metric,
        split=result.split,
        trajectory=result.trajectory,
        failure_cluster_ids=result.failure_cluster_ids,
        target_dimensions=result.target_dimensions,
        metric_calls=result.metric_calls,
        train_count=result.train_count,
        val_count=result.val_count,
    )


__all__ = ["EXPERIMENT_CANDIDATE_SCHEMA", "ExperimentCandidate", "accepted_episodes", "optimize_snapshot"]
