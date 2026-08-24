"""The one bounded GEPA optimization this repository runs.

Two planes need it. The trusted compiler runs it inside a sealed container, against a metered proxy,
to produce a candidate that may enter the release gate. The operator's experiment loop runs it in
process, against endpoints named on the command line, to answer "would this instruction have helped"
before anyone spends a container on it.

They must be the same algorithm. The moment they are not, the number an operator reads in the fast
loop stops predicting what a trusted compile maximizes — the exact failure `_project_episodes` already
exists to prevent on the corpus side, one layer up.

So this function owns the optimizer construction, the honest split, the reflective proposer and the
patch extraction, and it owns nothing else: no database, no credential, no container, no tariff, no
artifact writer, no promotion. Metering is the caller's business — the trusted compiler hands in LMs
that are already budget-metered, and the experiment hands in plain ones.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

import dspy  # type: ignore[import-untyped]
from pydantic import Field

from ...artifact_identity import canonical_sha
from ...program.artifact import ProgramStrategyArtifactV1, ProgramStrategyPatchV1
from ...program.dspy_adapter import DspyStrictJSONAdapter
from ...program.graph import DspyCompileProgram, extract_optimizer_patch
from ..metric import (
    DevelopmentEpisode,
    _compile_example,
    _ExactModel,
    _honest_split,
    _metric_receipt,
    _retrieval_receipt,
    bind_metric,
    production_decision,
)
from ..proposer import RulePackAwareProposer
from .security import ModelExecutionIdentity, gepa_metric_call_ceiling


class _Optimizer(Protocol):
    def compile(
        self,
        student: DspyCompileProgram,
        *,
        trainset: list[dspy.Example],
        teacher: None,
        valset: list[dspy.Example],
    ) -> dspy.Module: ...


OptimizerFactory = Callable[..., _Optimizer]


class GepaRunResult(_ExactModel):
    """Everything one optimization run produced, and nothing about who paid for it."""

    patch: ProgramStrategyPatchV1
    metric: dict[str, Any]
    optimizer_config: dict[str, Any]
    trajectory: dict[str, Any]
    checkpoint: dict[str, Any]
    split: dict[str, Any]
    retrieval: dict[str, Any]
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    metric_calls: int = Field(ge=0)
    train_count: int = Field(gt=0)
    val_count: int = Field(gt=0)


def run_gepa(
    *,
    base_program: ProgramStrategyArtifactV1,
    episodes: Sequence[DevelopmentEpisode],
    task_lm: dspy.LM,
    reflection_lm: dspy.LM,
    judge: Any,
    max_metric_calls: int,
    seed: int,
    review_rubric_version: str,
    optimizer_factory: OptimizerFactory = dspy.GEPA,
    student_factory: Callable[[ProgramStrategyArtifactV1], DspyCompileProgram] = DspyCompileProgram,
) -> GepaRunResult:
    """Optimize the two advisory instructions against accepted-review truth."""

    if judge is None:
        raise ValueError("news_program_compile_metric_judge_required")
    failure_clusters, target_dimensions = failure_scope(episodes)
    if not failure_clusters:
        raise ValueError("news_program_compile_no_verified_failure_clusters")
    train_episodes, val_episodes, split_receipt = _honest_split(episodes)
    train_examples = [_compile_example(episode) for episode in train_episodes]
    val_examples = [_compile_example(episode) for episode in val_episodes]
    retrieval_receipt = _retrieval_receipt(episodes)

    student = student_factory(base_program)
    if tuple(name for name, _ in student.named_predictors()) != ("event_semantics", "reader_card"):
        raise ValueError("news_program_compile_factory_topology_mismatch")

    metric = bind_metric(judge)
    metric_receipt = _metric_receipt(metric, review_rubric_version=review_rubric_version)
    constructor = optimizer_constructor(
        max_metric_calls=max_metric_calls,
        seed=seed,
        train_count=len(train_examples),
        proposer=RulePackAwareProposer(base_program),
    )
    config_receipt = optimizer_config_receipt(
        constructor=constructor,
        task_lm=task_lm,
        reflection_lm=reflection_lm,
        optimizer_factory=optimizer_factory,
        metric_sha256=canonical_sha(metric_receipt),
        example_count=len(train_examples) + len(val_examples),
        train_count=len(train_examples),
        val_count=len(val_examples),
    )
    optimizer = optimizer_factory(metric, reflection_lm=reflection_lm, **constructor)
    with dspy.context(
        lm=task_lm,
        adapter=DspyStrictJSONAdapter(use_native_function_calling=False),
        track_usage=True,
        disable_history=True,
    ):
        compiled = optimizer.compile(student, trainset=train_examples, teacher=None, valset=val_examples)
    if not isinstance(compiled, DspyCompileProgram):
        raise TypeError("news_program_compile_result_type_invalid")

    details = getattr(compiled, "detailed_results", None)
    metric_calls = int(getattr(details, "total_metric_calls", -1))
    ceiling = gepa_metric_call_ceiling(
        max_metric_calls=max_metric_calls,
        optimizer_config=config_receipt,
        expected_example_count=len(train_examples) + len(val_examples),
    )
    if metric_calls < 0 or metric_calls > ceiling:
        raise ValueError(
            "news_program_compile_metric_budget_unverifiable:"
            f"observed={metric_calls},requested={max_metric_calls},ceiling={ceiling}"
        )
    trajectory = trajectory_receipt(details)
    # Canonicalize before reading the checkpoint. Until `restore_empty_advisories` runs, a Predictor GEPA
    # left alone still holds DSPy's generated default rather than the empty advisory it stands for, and the
    # receipt would disagree with the patch and the shipped artifact for exactly that case.
    restore_empty_advisories(compiled)
    checkpoint = checkpoint_receipt(compiled)
    patch = extract_optimizer_patch(compiled, base_program)
    if (
        patch.event_semantics_instruction == base_program.event_semantics_instruction
        and patch.reader_card_instruction == base_program.reader_card_instruction
    ):
        raise ValueError("news_program_compile_no_program_change")
    return GepaRunResult(
        patch=patch,
        metric=metric_receipt,
        optimizer_config=config_receipt,
        trajectory=trajectory,
        checkpoint=checkpoint,
        split=split_receipt,
        retrieval=retrieval_receipt,
        failure_cluster_ids=failure_clusters,
        target_dimensions=target_dimensions,
        metric_calls=metric_calls,
        train_count=len(train_examples),
        val_count=len(val_examples),
    )


def optimizer_constructor(
    *,
    max_metric_calls: int,
    seed: int,
    train_count: int,
    proposer: RulePackAwareProposer,
) -> dict[str, Any]:
    """The one GEPA configuration both planes construct."""

    return {
        "auto": None,
        "max_full_evals": None,
        "max_metric_calls": max_metric_calls,
        # DSPy's default is 3, and 3 is too few for this metric. In the first real run every proposal was
        # skipped on an *exact* tie — 1.729166 vs 1.729166, 1.597917 vs 1.597917 — because a good advisory
        # here names recurring evidence patterns (a sentiment index, a comparison base, a crypto-linked
        # equity) that a 3-example sample almost never contains. The metric is also coarse, moving in steps
        # like 0 / 0.675 / 0.825 / 1.0, so ties are easy to hit and GEPA skips on a tie by rule. A wider
        # minibatch is what gives a real improvement room to show up as one.
        "reflection_minibatch_size": min(10, train_count),
        "candidate_selection_strategy": "pareto",
        "skip_perfect_score": True,
        "add_format_failure_as_feedback": True,
        # #143. The default proposer shows the reflection model only the mutable component, which for this
        # Program is an advisory slot whose code-owned baseline is empty. It was being asked to write a
        # whole instruction while blind to the nine RulePacks already in the prompt.
        "instruction_proposer": proposer,
        "component_selector": "round_robin",
        "use_merge": True,
        "max_merge_invocations": 5,
        "num_threads": 1,
        "failure_score": 0.0,
        "perfect_score": 1.0,
        "track_stats": True,
        "track_best_outputs": False,
        "log_dir": None,
        "use_wandb": False,
        "wandb_api_key": None,
        "wandb_init_kwargs": None,
        "warn_on_score_mismatch": True,
        "use_mlflow": False,
        "seed": seed,
        "gepa_kwargs": None,
    }


def optimizer_config_receipt(
    *,
    constructor: dict[str, Any],
    task_lm: dspy.LM,
    reflection_lm: dspy.LM,
    optimizer_factory: OptimizerFactory,
    metric_sha256: str,
    example_count: int,
    train_count: int,
    val_count: int,
) -> dict[str, Any]:
    import importlib.metadata

    return {
        "schema": "tracefold.news.compile_optimizer_config_receipt.v1",
        "optimizer": {
            "implementation": f"{optimizer_factory.__module__}.{optimizer_factory.__qualname__}",
            "dspy_version": importlib.metadata.version("dspy"),
            "gepa_version": importlib.metadata.version("gepa"),
        },
        "metric_sha256": metric_sha256,
        # The proposer is code, not a scalar. It is named below rather than serialized, so the receipt still
        # says exactly which one ran without trying to JSON-encode an object.
        "constructor_scalar_arguments": _json_scalars(constructor),
        # GEPA requires the named kwarg even when telemetry is disabled. A secret-shaped key is forbidden in
        # retained receipts, so record its exact absence as a scalar name instead of serializing the key.
        "omitted_unset_arguments": ["wandb_api_key"],
        "instruction_proposer": {
            "implementation": f"{type(constructor['instruction_proposer']).__module__}."
            f"{type(constructor['instruction_proposer']).__qualname__}"
            if constructor.get("instruction_proposer") is not None
            else None,
            "reads": "full rendered predictor instruction (sealed kernel + ordered RulePacks + authority seal)",
            "writes": "one advisory instruction body only",
        },
        "model_identities": {
            "task": require_model_identity(task_lm, role="task").model_dump(mode="json"),
            "reflection": require_model_identity(reflection_lm, role="reflection").model_dump(mode="json"),
        },
        "dspy_context": {
            "adapter": "DspyStrictJSONAdapter/native_function_calling_false",
            "track_usage": True,
            "disable_history": True,
        },
        "compile_call": {
            "teacher": None,
            "example_count": example_count,
            "trainset_count": train_count,
            "valset_count": val_count,
            "valset_identity": "disjoint_cluster_split",
        },
    }


def require_model_identity(lm: dspy.LM, *, role: str) -> ModelExecutionIdentity:
    """The identity this LM will answer under, or a refusal before anything is spent.

    Reconstructing one from the LM's own kwargs would attest nothing: the role contract — temperature,
    token ceiling, deadline — is exactly what an identity is for, so inferring it from the object it
    describes is circular.
    """

    identity = getattr(lm, "tracefold_compiler_endpoint_identity", None)
    if not isinstance(identity, ModelExecutionIdentity) or identity.role != role:
        raise ValueError("news_program_compile_endpoint_identity_unavailable")
    return identity


def failure_scope(episodes: Sequence[DevelopmentEpisode]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The clusters an accepted review says are wrong, and the dimensions they are wrong on."""

    clusters: set[str] = set()
    dimensions: set[str] = set()
    for episode in episodes:
        review = episode.accepted_review
        results = dict(review.get("dimensions") or {})
        failed = {str(key) for key, value in results.items() if value == "fail"}
        production_judgment = episode.production_judgment
        should_push = str(review.get("should_push") or "uncertain")
        production_action = (
            production_decision(production_judgment, episode.policy_metric) if production_judgment is not None else None
        )
        production_pushes = production_action is not None and production_action.final in {"push", "escalate"}
        decision_failed = (should_push in {"must_push", "should_push"} and not production_pushes) or (
            should_push in {"must_hold", "should_hold"} and production_pushes
        )
        novelty = str(dict(review.get("novelty") or {}).get("judgment") or "uncertain")
        production_novelty = production_judgment.verdict.novelty if production_judgment is not None else ""
        novelty_failed = novelty not in ("uncertain", production_novelty)
        correction = bool(str(review.get("expected_correction") or "").strip())
        if failed or decision_failed or novelty_failed or correction:
            clusters.add(episode.cluster_id)
            dimensions.update(failed)
            if decision_failed:
                dimensions.add("should_push")
            if novelty_failed:
                dimensions.add("novelty")
            if correction and not failed:
                dimensions.add("factual_fidelity")
    return tuple(sorted(clusters)), tuple(sorted(dimensions))


def generated_default_instruction(predictor: dspy.Predict) -> str:
    """What DSPy writes into a signature when it is handed an empty instruction."""

    return str(predictor.signature.with_instructions("").instructions or "")


def restore_empty_advisories(compiled: DspyCompileProgram) -> None:
    """Map DSPy's auto-generated default instruction back to the empty advisory it stands for.

    The empty advisory cannot survive a GEPA round trip on its own. `Signature.with_instructions("")` does
    not store an empty instruction — DSPy substitutes ``"Given the fields `evidence_json`, produce the
    fields `semantics`."`` The baseline is empty, so GEPA's seed candidate is `""`, and the very first
    `build_program(seed)` rebuilt the student with that boilerplate in the advisory slot.

    Two things went wrong from there. The optimizer never evaluated the true baseline, and when the Pareto
    front kept the seed, `extract_optimizer_patch` read the boilerplate back out as a *learned* strategy —
    `news_program_compile_no_program_change` did not fire, because the text genuinely differs from the
    parent's empty string. A run that learned nothing produced a patch that looked like it had.

    One blank character is the canonical empty instruction (`with_instructions(" ")` stores `""`), which is
    how the factory builds the baseline in the first place.
    """

    for name in ("event_semantics", "reader_card"):
        predictor = getattr(compiled, name)
        if str(predictor.signature.instructions or "") == generated_default_instruction(predictor):
            predictor.signature = predictor.signature.with_instructions(" ")


def trajectory_receipt(details: Any) -> dict[str, Any]:
    import math

    if details is None:
        raise ValueError("news_program_compile_trajectory_missing")
    scores = [float(value) for value in list(getattr(details, "val_aggregate_scores", ()) or ())]
    if any(not math.isfinite(score) for score in scores):
        raise TypeError("news_program_compile_nonfinite_receipt_value")
    return {
        "schema": "tracefold.news.compile_trajectory_receipt.v1",
        "parents": _json_scalars({"parents": list(getattr(details, "parents", ()) or ())})["parents"],
        "val_aggregate_scores": scores,
        "discovery_eval_counts": [int(value) for value in list(getattr(details, "discovery_eval_counts", ()) or ())],
        "total_metric_calls": int(getattr(details, "total_metric_calls", -1)),
        "num_full_val_evals": int(getattr(details, "num_full_val_evals", 0) or 0),
        "seed": int(getattr(details, "seed", 0) or 0),
        "best_idx": int(getattr(details, "best_idx", 0) or 0),
    }


def checkpoint_receipt(program: DspyCompileProgram) -> dict[str, Any]:
    return {
        "schema": "tracefold.news.compile_checkpoint_receipt.v2",
        "factory": program.artifact.factory_id,
        # The advisory text itself, not a digest of it: this receipt is the record of what the run produced,
        # and the winner's two instructions are already carried by the patch beside it.
        "predictors": {
            name: {"instruction": str(predictor.signature.instructions or "")}
            for name, predictor in program.named_predictors()
        },
    }


def _json_scalars(value: Any) -> Any:
    from ..metric import _json_safe

    if isinstance(value, dict):
        omitted = {"instruction_proposer", "wandb_api_key"}
        return {key: _json_safe(item) for key, item in value.items() if key not in omitted}
    return _json_safe(value)


__all__ = [
    "GepaRunResult",
    "OptimizerFactory",
    "checkpoint_receipt",
    "failure_scope",
    "generated_default_instruction",
    "optimizer_config_receipt",
    "optimizer_constructor",
    "require_model_identity",
    "restore_empty_advisories",
    "run_gepa",
    "trajectory_receipt",
]
