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

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol, cast

import dspy  # type: ignore[import-untyped]
from pydantic import ValidationError

from ...artifact_identity import canonical_sha
from ...program.artifact import ProgramStrategyArtifactV1
from ...program.dspy_adapter import DspyStrictJSONAdapter, ExactMetadataDspyLM
from ...program.graph import DspyCompileProgram, extract_optimizer_patch
from ..metric import (
    _compile_example,
    _metric_receipt,
    bind_metric,
)
from ..objective import DevelopmentEpisode, build_gepa_objective_plan, retrieval_receipt
from ..proposer import RulePackAwareProposer
from .security import (
    REFLECTION_MAX_TOKENS,
    REFLECTION_TIMEOUT_SECONDS,
    GepaRunResult,
    ModelExecutionIdentity,
    gepa_metric_call_ceiling,
)

_OWNED_LM_KWARGS = frozenset(
    {"api_key", "api_base", "base_url", "cache", "num_retries", "temperature", "max_tokens", "timeout"}
)
# The task LM answers the Program's own signatures, so it keeps the production route's determinism: temperature
# 0 and the route's own token ceiling. The reflection LM does something else entirely — it reads a minibatch of
# failures and writes a whole new instruction — and DSPy's guidance for it is the opposite on every axis. Until
# #143 both were built from the task route's numbers, which capped a proposed instruction at 1,200 tokens (below
# what the advisory slot itself accepts) and gave a reflection call the 20 s route deadline.
_REFLECTION_TEMPERATURE = 1.0
_TASK_TEMPERATURE = 0


_ADVISORY_REJECTIONS = (
    "news_program_learned_strategy_too_large",
    "news_program_learned_strategy_unsafe",
    "news_program_learned_strategy_secret",
    "news_program_learned_strategy_unicode_noncanonical",
)


def _advisory_rejection_code(exc: BaseException) -> str | None:
    """Whether this failure is the advisory safety bound refusing a proposal, and which bound it was."""

    text = str(exc)
    return next((marker for marker in _ADVISORY_REJECTIONS if marker in text), None)


class _FeedbackCompileProgram(DspyCompileProgram):
    """The optimizer's student, with advisory rejections turned into scorable predictions.

    The bounds themselves are unchanged and still absolute — this subclass cannot widen them. What changes is
    where a rejection lands: raised out of `forward`, DSPy's evaluator caught it and recorded `failure_score`
    without ever calling the metric, so the reflection model was told its proposal scored zero and nothing
    about why. It then proposed text that tripped the same bound again. Returned as a prediction, the code
    reaches the metric and comes back as a repair instruction.

    It lives in the shared core, not beside the trusted compiler: `_rekey_trace` below is what makes GEPA
    able to propose anything at all, so a plane that ran `run_gepa` with the plain student would burn its
    whole budget on the seed and end in `no_program_change` while reporting the same algorithm. It is still
    optimizer-only — nothing in the production graph may learn to answer with `advisory_rejected`, and the
    module boundary is what keeps that true.
    (Until #193 the stated reason was that the Program package's source was hashed into the shipped
    Artifact, so editing it re-issued `program_sha256`. That is no longer so — the root commits to the
    factory id and the two instructions — but the placement is still right for the reason above.)
    """

    def forward(self, evidence_json: str, card_evidence_json: str, told_count: int) -> dspy.Prediction:
        trace = dspy.settings.trace
        before = len(trace) if isinstance(trace, list) else None
        try:
            return cast(dspy.Prediction, super().forward(evidence_json, card_evidence_json, told_count))
        except (ValidationError, ValueError) as exc:
            code = _advisory_rejection_code(exc)
            if code is None:
                raise
            return dspy.Prediction(semantics=None, card=None, verdict=None, advisory_rejected=code)
        finally:
            # In `finally`, not on the success path only. A schema rejection of the model's own output is the
            # most informative failure there is — it is exactly what `add_format_failure_as_feedback` exists to
            # surface — and leaving those entries keyed to the anonymous inner predictor drops them from the
            # reflective dataset, so the reflection model never sees the outputs it most needs to fix.
            if before is not None:
                self._rekey_trace(trace, before)

    def _rekey_trace(self, trace: list[Any], before: int) -> None:
        """Attribute the recorded calls to the two named Predictors GEPA is optimizing.

        `_OptimizerOwnedPredictor` does not answer the provider itself: it renders RulePacks plus the advisory
        into a fresh `dspy.Predict` and delegates. So the trace records that anonymous inner predictor, whose
        signature carries the full rendered instruction.

        GEPA matches traces to components with `t[0].signature.equals(module.signature)`, and the outer
        signature carries only the advisory. The two are never equal, so `make_reflective_dataset` found no
        instances, raised "No valid predictions found for any module", and the reflective loop could not
        propose anything at all — no matter how good the metric or the feedback was.

        The graph is exactly two serial calls in a fixed order, which is what makes positional re-keying exact
        rather than a guess.
        """

        named = [self.event_semantics, self.reader_card]
        for offset, entry in enumerate(trace[before:]):
            if offset >= len(named) or not isinstance(entry, tuple) or len(entry) != 3:
                continue
            trace[before + offset] = (named[offset], entry[1], entry[2])


class GepaNoProgramChange(ValueError):
    """The optimizer kept the seed: a complete run that learned nothing.

    A `ValueError` whose message is the code this has always raised, so every existing caller and every
    existing assertion is unchanged. What it adds is the run itself: "no candidate" is a terminal answer,
    and the one entry point has to be able to publish the metric, split and trajectory that produced it
    rather than an empty report (#202 §5).
    """

    def __init__(self, result: GepaRunResult) -> None:
        super().__init__("news_program_compile_no_program_change")
        self.result = result


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
    student_factory: Callable[[ProgramStrategyArtifactV1], DspyCompileProgram] = _FeedbackCompileProgram,
) -> GepaRunResult:
    """Optimize the two advisory instructions against accepted-review truth."""

    if judge is None:
        raise ValueError("news_program_compile_metric_judge_required")
    # One Objective Plan, built here rather than by each caller, so the corpus this optimization sees is the
    # corpus `readiness`, the dataset-bound baseline and `CandidateEvaluator` re-derive from the same frozen
    # episodes. Until #199 this function scoped its targets with an owner-blind "did any review say anything
    # is wrong" rule and then split *every* episode — so a retrieval miss became an instruction to repair,
    # and cases nobody had blamed on the Prompt still reached the reflective minibatch as low scores.
    plan = build_gepa_objective_plan(episodes)
    if not plan.target_failure_cluster_ids:
        raise ValueError("news_program_compile_no_verified_failure_clusters")
    if not plan.control_cluster_ids:
        raise ValueError("news_program_compile_no_correct_control_clusters")
    if plan.split is None:
        # Verbatim: the plan records the exact code `_honest_split` refused with, so this stays the failure
        # the caller has always seen rather than a translation of it.
        raise ValueError(plan.split_error or "news_program_compile_objective_split_unavailable")
    if plan.blocking_reasons:
        raise ValueError("news_program_compile_objective_blocked:" + ",".join(plan.blocking_reasons))
    split_receipt = plan.split
    train_examples = [_compile_example(episode) for episode in plan.train_episodes]
    val_examples = [_compile_example(episode) for episode in plan.development_selection_episodes]
    retrieval = retrieval_receipt(episodes)

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
    result = GepaRunResult(
        patch=patch,
        metric=metric_receipt,
        optimizer_config=config_receipt,
        trajectory=trajectory,
        checkpoint=checkpoint,
        split=split_receipt,
        retrieval=retrieval,
        failure_cluster_ids=plan.target_failure_cluster_ids,
        target_dimensions=plan.target_dimensions,
        metric_calls=metric_calls,
        train_count=len(train_examples),
        val_count=len(val_examples),
    )
    if (
        patch.event_semantics_instruction == base_program.event_semantics_instruction
        and patch.reader_card_instruction == base_program.reader_card_instruction
    ):
        raise GepaNoProgramChange(result)
    return result


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


def build_compile_lm(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    timeout: float,
    max_tokens: int,
    role: Literal["task", "reflection"] = "task",
    model_kwargs: Mapping[str, Any] | None = None,
) -> dspy.LM:
    """Build one of the two roles `run_gepa` consumes, stamped with the identity it will be held to.

    It lives beside `require_model_identity` because that function is the only reader of what this writes:
    a role whose stamp is attached anywhere else is a role nothing verified.
    """

    extras = dict(model_kwargs or {})
    overlap = _OWNED_LM_KWARGS.intersection(extras)
    if overlap:
        raise ValueError(f"news_program_compile_model_kwargs_owned:{','.join(sorted(overlap))}")
    reflection = role == "reflection"
    lm = ExactMetadataDspyLM(
        str(model_name),
        api_key=str(api_key),
        api_base=str(api_base),
        # The reflection role's budget is exact, not a floor. `ModelExecutionIdentity` holds the role to
        # these values, so a `max()` here would silently accept a caller's larger timeout or token ceiling
        # and then fail the identity contract that is supposed to attest them.
        timeout=REFLECTION_TIMEOUT_SECONDS if reflection else float(timeout),
        max_tokens=REFLECTION_MAX_TOKENS if reflection else int(max_tokens),
        temperature=_REFLECTION_TEMPERATURE if reflection else _TASK_TEMPERATURE,
        cache=False,
        # Stays zero. `_BudgetedLM` counts every physical attempt against the operator's budget, and a retry
        # hidden inside the provider client would be a request the receipt never saw. The retry that #143 adds
        # lives one layer up, where it is metered.
        num_retries=0,
        **extras,
    )
    lm.tracefold_compiler_endpoint_identity = ModelExecutionIdentity.issue(
        role="reflection" if reflection else "task",
        model=str(model_name),
        api_base=str(api_base),
        max_output_tokens=lm.kwargs["max_tokens"],
        timeout_seconds=lm.kwargs["timeout"],
        temperature=lm.kwargs["temperature"],
        model_kwargs=extras,
    )
    return lm


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
    "GepaNoProgramChange",
    "GepaRunResult",
    "OptimizerFactory",
    "_FeedbackCompileProgram",
    "build_compile_lm",
    "checkpoint_receipt",
    "generated_default_instruction",
    "optimizer_config_receipt",
    "optimizer_constructor",
    "require_model_identity",
    "restore_empty_advisories",
    "run_gepa",
    "trajectory_receipt",
]
