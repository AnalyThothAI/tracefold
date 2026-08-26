"""The one offline optimization this repository runs, and the only thing that produces a Prompt candidate.

Before #202 this was five modules and a container platform. The trusted compiler sealed a corpus, launched
a sealed image against a metered proxy sidecar under a seccomp policy, and produced a `CompileRecordV1`
carrying a three-party build attestation and a tariff; the experiment loop ran the same `run_gepa()` in
process and produced an `ExperimentCandidate` marked `promotable=False`. Both optimized the same two
strings, and the whole platform existed to prove *where* those two strings came from.

The generator was never the authority for them. What makes a candidate trustworthy is downstream of
generation — a bounded write-set, a parent bound to the active stable, a frozen dataset, an independent
evaluation, future holdout, shadow, canary and a human promotion — and none of it needs to know where the
text came from. The sandbox threat model it was built for was "the optimizer might return code"; GEPA
returns a typed patch of two instructions, and `dspy.GEPA` cannot write anything else (`build_program` only
assigns `pred.signature.with_instructions(...)`). If dynamic code generation ever returns, §6.3 says that
is a new Issue with a new threat model, not a platform kept warm for a hypothetical.

So this module owns generation, whole: the role identities every call answers under, the budget every
physical call is metered against, the Objective Plan that decides what GEPA may see, the reflective
proposer, the run itself, and the terminal answer. It owns nothing downstream: no database handle, no
artifact writer, no registration, no canary, no promotion. A run ends in `NO_OP`, `REJECTED` or `ADVANCE`,
and `ADVANCE` is a candidate, not a release.
"""

from __future__ import annotations

import importlib.metadata
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..artifact_identity import canonical_sha
from ..program.artifact import (
    ProgramStrategyArtifactV1,
    ProgramStrategyPatchV1,
    load_stable_program_artifact,
    render_predictor_instruction,
    validate_learned_instruction,
)
from ..program.dspy_adapter import (
    DspyStrictJSONAdapter,
    ExactMetadataDspyLM,
    ExactProviderCallCapture,
    ExactProviderMetadata,
    PredictorAdapterError,
    _is_retryable_exception,
)
from ..program.graph import DspyCompileProgram, extract_optimizer_patch
from ..program.runtime import PredictorName
from .contracts import (
    METRIC_JUDGE_MAX_TOKENS,
    METRIC_JUDGE_TIMEOUT_SECONDS,
    REFLECTION_MAX_TOKENS,
    REFLECTION_TIMEOUT_SECONDS,
    DevelopmentDatasetRef,
    ModelExecutionIdentity,
    OptimizationBudget,
    OptimizationResult,
    OptimizationRunReport,
    OptimizerRole,
    PromptCandidateV1,
    PromptPatchV1,
)
from .metric import _compile_example, _json_safe, _metric_receipt, bind_metric
from .objective import (
    DevelopmentEpisode,
    GepaObjectivePlan,
    build_gepa_objective_plan,
    optimizer_population_identity,
    retrieval_receipt,
)

OBJECTIVE_SUMMARY_SCHEMA = "tracefold.news.optimization_objective_summary.v2"
USAGE_SCHEMA = "tracefold.news.optimization_usage.v1"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- the metric-call ceiling (was `compiler/security.py`) -----------------------------------------


def gepa_metric_call_ceiling(
    *,
    max_metric_calls: int,
    optimizer_config: Mapping[str, Any],
    expected_example_count: int,
) -> int:
    """Return GEPA's sealed end-of-step metric ceiling.

    GEPA checks ``max_metric_calls`` between steps. A started step can consume one
    reflection minibatch and, when accepted, one full validation pass before it
    stops. Those widths are trustworthy only when they are bound to the complete
    train/validation split retained in the optimizer receipt.
    """

    constructor = optimizer_config.get("constructor_scalar_arguments")
    compile_call = optimizer_config.get("compile_call")
    if not isinstance(constructor, Mapping) or not isinstance(compile_call, Mapping):
        raise ValueError("news_program_compile_optimizer_metric_budget_invalid")
    requested = constructor.get("max_metric_calls")
    minibatch = constructor.get("reflection_minibatch_size")
    example_count = compile_call.get("example_count")
    train_count = compile_call.get("trainset_count")
    val_count = compile_call.get("valset_count")
    values = (requested, minibatch, example_count, train_count, val_count, max_metric_calls, expected_example_count)
    if any(type(value) is not int for value in values):
        raise ValueError("news_program_compile_optimizer_metric_budget_invalid")
    requested = cast(int, requested)
    minibatch = cast(int, minibatch)
    example_count = cast(int, example_count)
    train_count = cast(int, train_count)
    val_count = cast(int, val_count)
    if (
        requested != max_metric_calls
        or max_metric_calls <= 0
        or expected_example_count <= 0
        or example_count != expected_example_count
        or train_count <= 0
        or train_count > example_count
        or val_count <= 0
        or val_count > example_count
        or train_count + val_count != example_count
        or minibatch <= 0
        or minibatch > train_count
    ):
        raise ValueError("news_program_compile_optimizer_metric_budget_invalid")
    return max_metric_calls + val_count + minibatch


class GepaRunResult(_ExactModel):
    """Everything one optimization run produced, and nothing about who paid for it.

    Both planes produce one of these: the trusted compiler inside its container, and the operator's
    experiment loop in process. It lives here rather than beside `run_gepa` so that the host — which must
    never import DSPy — can hold the same object the runner produced instead of a second model of it.

    Three documents used to restate these ten fields: the runner's own result, the receipts the host
    parsed out of the container, and the compile record built around them, with a field-by-field copy
    between each pair. They are one object now, carried whole.
    """

    patch: ProgramStrategyPatchV1
    metric: dict[str, Any]
    optimizer_config: dict[str, Any]
    trajectory: dict[str, Any]
    checkpoint: dict[str, Any]
    # The two things a scalar score cannot answer: was the winner picked on examples it never trained
    # on, and did the model see the card it was supposed to recognise. Both were computed and validated
    # inside the container and then dropped before the host saw them, so the documented proof never
    # reached any receipt.
    split: dict[str, Any]
    retrieval: dict[str, Any]
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    metric_calls: int = Field(ge=0)
    train_count: int = Field(gt=0)
    val_count: int = Field(gt=0)


# --- the reflective proposer (was `proposer.py`) --------------------------------------------------

_BRIEF = """You are amending ONE bounded advisory block inside a larger, code-owned prompt.

Below is the COMPLETE prompt that is actually sent for the `{component}` Predictor, exactly as the runtime
renders it. You may NOT change any of it except the single section marked `# LEARNEDSTRATEGY`.

===== BEGIN RENDERED PROMPT (READ-ONLY EXCEPT THE LEARNEDSTRATEGY SECTION) =====
{rendered}
===== END RENDERED PROMPT =====

Rules for what you write:
- Write ONLY the replacement body of the LEARNEDSTRATEGY section. Do not repeat the QualityKernel, the
  RulePacks, the authority seal, or the schema. They are already in the prompt and re-stating them wastes the
  budget and can only introduce contradictions.
- The RulePacks above are authoritative and you cannot weaken, reinterpret or override them. Write guidance
  that resolves cases the RulePacks leave genuinely ambiguous, or that names concrete recurring evidence
  patterns the feedback below shows the model is getting wrong.
- Be specific to this domain and this failure set. Prefer stated correct values, named instruments, and
  concrete decision boundaries over general advice about being careful.
- Do not include URLs, template braces, credentials, or any instruction to ignore or outrank other sections;
  such text is rejected outright and the candidate scores zero.
"""


class RulePackAwareProposer:
    """A `ProposalFn` that prefixes GEPA's own proposal prompt with the rendered, read-only context."""

    def __init__(self, artifact: ProgramStrategyArtifactV1) -> None:
        self._artifact = artifact
        predictor_names: tuple[PredictorName, ...] = ("event_semantics", "reader_card")
        self._rendered: dict[str, str] = {
            name: render_predictor_instruction(name, artifact.instruction_for(name)) for name in predictor_names
        }
        self.calls = 0
        self.components_seen: list[str] = []
        self.rejections: list[str] = []

    def context_for(self, component: str) -> str:
        """The read-only brief for one component. Exposed so a test can assert the RulePacks reach the model."""

        rendered = self._rendered.get(component)
        if rendered is None:
            raise ValueError(f"news_program_proposer_unknown_component:{component}")
        return _BRIEF.format(component=component, rendered=rendered)

    def __call__(
        self,
        candidate: Mapping[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: Sequence[str],
    ) -> dict[str, str]:
        from gepa.strategies.instruction_proposal import InstructionProposalSignature

        updated: dict[str, str] = {}
        for component in components_to_update:
            examples = list(reflective_dataset.get(component) or ())
            if not examples:
                continue
            self.calls += 1
            self.components_seen.append(component)
            current = str(candidate.get(component) or "").strip()
            # The advisory slot's own content is what GEPA is editing; the brief above is the surrounding
            # prompt it must not duplicate.
            doc = (
                f"{self.context_for(component)}\n"
                "===== CURRENT LEARNEDSTRATEGY BODY (this is what you are replacing) =====\n"
                f"{current or '(empty — no advisory has been learned yet)'}\n"
                "===== END CURRENT LEARNEDSTRATEGY BODY ====="
            )
            proposal = InstructionProposalSignature.run(
                lm=_reflect,
                input_dict={
                    "current_instruction_doc": doc,
                    "dataset_with_feedback": examples,
                    "prompt_template": None,
                },
            )
            text = str(proposal.get("new_instruction") or "").strip()
            rejection = _advisory_rejection(component, text)
            if rejection is not None:
                # Validate here, where the model that wrote the text is still in the loop.
                #
                # A rejected advisory is rejected *before* any provider call, so nothing reaches
                # `dspy.settings.trace`; GEPA's `make_reflective_dataset` finds no instances for the component
                # and the whole iteration is silently skipped. The metric's repair instruction is real but
                # unreachable in that path — it can only be delivered by asking again, here, with the code.
                self.rejections.append(rejection)
                retry = InstructionProposalSignature.run(
                    lm=_reflect,
                    input_dict={
                        "current_instruction_doc": (
                            f"{doc}\n\n===== YOUR PREVIOUS PROPOSAL WAS REJECTED =====\n"
                            f"Code-owned advisory safety rejected it: {rejection}.\n"
                            "Rewrite it without URLs, template braces, credential-shaped text, or any claim of "
                            "authority over the QualityKernel, the RulePacks or the schema, and keep it under "
                            "8192 bytes."
                        ),
                        "dataset_with_feedback": examples,
                        "prompt_template": None,
                    },
                )
                text = str(retry.get("new_instruction") or "").strip()
                if _advisory_rejection(component, text) is not None:
                    continue
            if text:
                updated[component] = text
        return updated


def _advisory_rejection(component: str, text: str) -> str | None:
    """The exact code the advisory bounds would refuse this text with, or `None` if it is acceptable."""

    if not text:
        return None
    try:
        validate_learned_instruction(text)
    except ValueError as exc:
        message = str(exc)
        for marker in (
            "news_program_learned_strategy_too_large",
            "news_program_learned_strategy_unsafe",
            "news_program_learned_strategy_secret",
            "news_program_learned_strategy_unicode_noncanonical",
        ):
            if marker in message:
                return marker
        return "news_program_learned_strategy_rejected"
    return None


def _reflect(prompt: Any) -> str:
    """Call whichever LM GEPA has put in context.

    DSPy invokes a custom proposer inside `dspy.context(lm=reflection_lm)`, so resolving the LM here rather
    than capturing one at construction is what keeps the budgeted proxy — and therefore the metered call
    count — in the path. Output shapes mirror DSPy's own `stripped_lm_call`.
    """

    lm = dspy.settings.lm
    if lm is None:
        raise ValueError("news_program_proposer_reflection_lm_missing")
    raw = lm(prompt) if isinstance(prompt, str) else lm(messages=prompt)
    first = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
    if isinstance(first, Mapping):
        if "text" not in first:
            raise ValueError("news_program_proposer_reflection_output_invalid")
        return str(first["text"])
    return str(first)


# --- the bounded GEPA run (was `compiler/gepa.py`) ------------------------------------------------

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


def build_optimizer_lm(
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
    if isinstance(value, dict):
        omitted = {"instruction_proposer", "wandb_api_key"}
        return {key: _json_safe(item) for key, item in value.items() if key not in omitted}
    return _json_safe(value)


# --- metering, the frozen dataset, and the one entry point ----------------------------------------

# One transient 5xx from a single-slot local server is not evidence that a candidate is bad, but with
# `num_retries=0` GEPA scored it as `failure_score` and moved on. The production route keeps retries off.
_NUM_RETRIES = 2

# The failures that are answers about this corpus rather than defects in this code. Everything else
# propagates: laundering a bug into `REJECTED` would retire the traceback that identifies it, and an
# operator reading a terminal report would see a corpus verdict where there was a broken build.
_REJECTION_PREFIXES = (
    "news_program_compile_no_verified_failure_clusters",
    "news_program_compile_no_correct_control_clusters",
    "news_program_compile_objective_blocked",
    "news_program_compile_objective_split_unavailable",
    "news_program_compile_split_",
    "news_program_compile_metric_budget_unverifiable",
    "news_program_compile_trajectory_missing",
    "news_program_compile_nonfinite_receipt_value",
    "news_program_learned_strategy_",
)
_NO_OP_CODE = "news_program_compile_no_program_change"


class OptimizationBudgetExceeded(RuntimeError):
    """Raised before another model call, or after a provider reports overspend."""


class _WorstCaseRates(Protocol):
    """What the meter needs from a rate table, without naming the trusted compiler's own.

    Structural on purpose: #202 deletes the tariff along with the proxy that reserved against it, and the
    meter has to outlive both. The offline job charges an unpriced call at the operator's declared per-call
    ceiling instead (`imputed_call_cost_microusd`), which needs no rate table at all.
    """

    def worst_case_cost_microusd(
        self, *, role: Literal["task", "reflection", "metric_judge"], request_bytes: int, max_output_tokens: int
    ) -> int: ...


class _MeteredBudget(Protocol):
    @property
    def max_task_model_calls(self) -> int: ...

    @property
    def max_reflection_model_calls(self) -> int: ...

    @property
    def max_cost_microusd(self) -> int: ...

    @property
    def max_call_cost_microusd(self) -> int: ...


class _BudgetMeter:
    def __init__(
        self,
        budget: _MeteredBudget,
        *,
        tariff: _WorstCaseRates | None = None,
        imputed_call_cost_microusd: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_wall_clock_seconds: float | None = None,
    ) -> None:
        self.budget = budget
        # Neither the local llama.cpp endpoint nor DeepSeek returns a price litellm can resolve, so
        # `provider_cost_microusd` is `None` for every endpoint this project actually uses and the meter used
        # to fail closed on the first call. The trusted compiler answered that with the proxy's own worst-case
        # tariff; the offline job answers it with the ceiling the operator already had to declare for a single
        # call. Both over-charge, which is the safe direction: the budget stops the run early, not late.
        self.tariff = tariff
        self.imputed_call_cost_microusd = imputed_call_cost_microusd
        self.task_model_calls = 0
        self.reflection_model_calls = 0
        self.task_cost_microusd = 0
        self.reflection_cost_microusd = 0
        self.actual_cost_microusd = 0
        self.imputed_cost_calls = 0
        self._role: Literal["task", "reflection"] = "task"
        self._monotonic = monotonic
        self._max_wall_clock_seconds = max_wall_clock_seconds
        self._started_monotonic = monotonic()

    @property
    def total_model_calls(self) -> int:
        return self.task_model_calls + self.reflection_model_calls

    @property
    def elapsed_seconds(self) -> float:
        return self._monotonic() - self._started_monotonic

    def before(self, role: Literal["task", "reflection"]) -> None:
        # The wall clock is checked here, before the call, for the same reason the cost reservation is: the
        # only bound worth having is one that stops the next request rather than reporting the last one.
        if self._max_wall_clock_seconds is not None and self.elapsed_seconds >= self._max_wall_clock_seconds:
            raise OptimizationBudgetExceeded("news_learning_optimize_wall_clock_exhausted")
        used = self.task_model_calls if role == "task" else self.reflection_model_calls
        limit = self.budget.max_task_model_calls if role == "task" else self.budget.max_reflection_model_calls
        if used >= limit:
            raise OptimizationBudgetExceeded(f"news_program_compile_{role}_model_call_budget_exhausted")
        if self.actual_cost_microusd + self.budget.max_call_cost_microusd > self.budget.max_cost_microusd:
            raise OptimizationBudgetExceeded("news_program_compile_cost_reservation_exhausted")
        self._role = role
        if role == "task":
            self.task_model_calls += 1
        else:
            self.reflection_model_calls += 1

    def _cost(self, metadata: ExactProviderMetadata) -> int:
        if metadata.provider_cost_microusd is not None:
            return metadata.provider_cost_microusd
        if self.tariff is not None:
            self.imputed_cost_calls += 1
            # Charged at the trusted worst-case rate, from tokens the provider did report.
            return self.tariff.worst_case_cost_microusd(
                role=self._role,
                request_bytes=metadata.input_tokens,
                max_output_tokens=max(1, metadata.output_tokens),
            )
        if self.imputed_call_cost_microusd is not None:
            self.imputed_cost_calls += 1
            return self.imputed_call_cost_microusd
        raise OptimizationBudgetExceeded("news_program_compile_provider_cost_unavailable")

    def after(self, metadata: ExactProviderMetadata) -> None:
        cost = self._cost(metadata)
        if cost > self.budget.max_call_cost_microusd:
            raise OptimizationBudgetExceeded("news_program_compile_call_cost_reservation_exceeded")
        self.actual_cost_microusd += cost
        if self._role == "task":
            self.task_cost_microusd += cost
        else:
            self.reflection_cost_microusd += cost
        if self.actual_cost_microusd > self.budget.max_cost_microusd:
            raise OptimizationBudgetExceeded("news_program_compile_cost_budget_exceeded")


def _is_transport_failure(exc: BaseException) -> bool:
    """The Program's own classifier, so "the provider did not answer" means one thing in both planes."""

    return _is_retryable_exception(exc)


class _BudgetedLM(dspy.BaseLM):  # type: ignore[misc]
    """Transparent LM proxy that makes every provider attempt observable."""

    def __init__(self, lm: dspy.LM, *, role: Literal["task", "reflection"], meter: _BudgetMeter) -> None:
        if getattr(lm, "cache", True) is not False:
            raise ValueError("news_program_compile_lm_cache_must_be_disabled")
        if int(getattr(lm, "num_retries", -1)) != 0:
            raise ValueError("news_program_compile_lm_hidden_retries_must_be_zero")
        if not callable(getattr(lm, "observe_exact_call", None)):
            raise ValueError("news_program_compile_lm_exact_metadata_seam_required")
        self._lm = lm
        self._role = role
        self._meter = meter
        self.transport_failures = 0
        self.transport_retries = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._lm, name)

    def _attempt(self, call: Callable[[], Any]) -> Any:
        """One metered attempt, retried only on transport-class failures.

        The retry is here rather than in the provider client because `_BudgetedLM` is what proves the budget:
        every physical request has to pass `before`/`after`. Without it, one transient 5xx from the single-slot
        local server made GEPA score that example as `failure_score` — indistinguishable, on the Pareto front,
        from a candidate that genuinely answered badly.
        """

        last: BaseException | None = None
        for attempt in range(_NUM_RETRIES + 1):
            self._meter.before(self._role)
            with self._lm.observe_exact_call() as capture:
                try:
                    output = call()
                except BaseException as exc:
                    # A transport failure means the provider never returned a response, so nothing recorded
                    # usage or cost and `_require_call_metadata` would raise here — aborting the run and
                    # masking the original error. `before()` has already reserved this attempt's worst-case
                    # cost, so the budget stays bounded without a settle-up the provider never supplied.
                    transport = _is_transport_failure(exc)
                    if not transport:
                        self._meter.after(_require_call_metadata(capture))
                        raise
                    if attempt == _NUM_RETRIES:
                        self.transport_failures += 1
                        raise
                    self.transport_retries += 1
                    last = exc
                    continue
            self._meter.after(_require_call_metadata(capture))
            return output
        raise last if last is not None else RuntimeError("news_program_compile_lm_retry_invariant")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._attempt(lambda: self._lm(*args, **kwargs))

    async def acall(self, *args: Any, **kwargs: Any) -> Any:
        # The async path keeps one metered attempt: DSPy's GEPA adapter drives this synchronously, and a second
        # retry implementation nobody exercises is a place for the budget proof to rot.
        self._meter.before(self._role)
        with self._lm.observe_exact_call() as capture:
            try:
                output = await self._lm.acall(*args, **kwargs)
            except BaseException:
                self._meter.after(_require_call_metadata(capture))
                raise
        self._meter.after(_require_call_metadata(capture))
        return output


def _require_call_metadata(capture: ExactProviderCallCapture) -> ExactProviderMetadata:
    try:
        return capture.require_exactly_one()
    except PredictorAdapterError as exc:
        raise OptimizationBudgetExceeded("news_program_compile_provider_metadata_unavailable") from exc


@dataclass(frozen=True)
class FrozenDevelopmentDataset:
    """One immutable corpus, bound to the Program it will be optimized against.

    The ref and the episodes travel together and are checked against each other, so "which dataset was this"
    is answerable from the retained report alone. `bind` is the only constructor that consults the active
    stable Program: a candidate whose parent is not the running stable is refused here, before a budget is
    spent, rather than at registration after one has been.
    """

    ref: DevelopmentDatasetRef
    episodes: tuple[DevelopmentEpisode, ...]
    parent_program: ProgramStrategyArtifactV1
    target_runtime_manifest_sha256: str
    dataset_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.episodes:
            raise ValueError("news_learning_optimize_dataset_empty")
        if len(self.episodes) != self.ref.episode_count:
            raise ValueError("news_learning_optimize_dataset_episode_count_mismatch")
        projection = canonical_sha([episode.model_dump(mode="json") for episode in self.episodes])
        if projection != self.ref.episode_projection_root_sha256:
            raise ValueError("news_learning_optimize_dataset_projection_root_mismatch")
        # The durable artifact identity, recomputed rather than trusted. A matching projection root beside
        # an unrelated `development_dataset_sha256` would produce a candidate naming a dataset it was never
        # built from, and the evaluator that later loads that SHA would score a different corpus — or fail
        # to find one — while the report claimed the run was dataset-bound.
        if self.ref.development_dataset_sha256 != canonical_sha({"kind": "dataset", "payload": self.dataset_payload}):
            raise ValueError("news_learning_optimize_dataset_artifact_hash_mismatch")

    @classmethod
    def bind(
        cls,
        *,
        ref: DevelopmentDatasetRef,
        episodes: Sequence[DevelopmentEpisode],
        dataset_payload: Mapping[str, Any],
        target_runtime_manifest_sha256: str,
        parent_program: ProgramStrategyArtifactV1 | None = None,
    ) -> FrozenDevelopmentDataset:
        parent = parent_program or load_stable_program_artifact()
        active = load_stable_program_artifact()
        if parent.program_sha256 != active.program_sha256:
            raise ValueError("news_learning_optimize_parent_must_be_active_stable")
        return cls(
            ref=ref,
            episodes=tuple(episodes),
            parent_program=parent,
            target_runtime_manifest_sha256=target_runtime_manifest_sha256,
            dataset_payload=dict(dataset_payload),
        )


@dataclass(frozen=True)
class OptimizationConfig:
    """Everything the offline job is allowed to hold: three endpoints, a budget and a clock.

    Deliberately not a database session, a repository, a canary handle or an artifact root. The list of
    fields is the list of powers this job has.
    """

    task_lm: dspy.LM
    reflection_lm: dspy.LM
    judge: Any
    budget: OptimizationBudget
    optimizer_factory: OptimizerFactory = dspy.GEPA
    # Injected so a terminal report is byte-reproducible under test; production passes the wall clock.
    now_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    monotonic: Callable[[], float] = time.monotonic


def objective_summary(plan: GepaObjectivePlan, *, episode_projection_root_sha256: str = "") -> dict[str, Any]:
    """What the Objective Plan decided, in the shape a candidate and a report both carry.

    Per-case dispositions are not here: `readiness` publishes those, and a candidate that embedded them
    would grow with the corpus while saying nothing a reader could act on. What survives is the membership
    a later evaluation has to reproduce exactly.
    """

    return {
        "schema": OBJECTIVE_SUMMARY_SCHEMA,
        "plan_schema": plan.schema_version,
        # Which projection of the corpus this plan was built from. The frozen dataset pins the case set;
        # the reviews behind those cases can still be edited, so registration re-projects and compares
        # this rather than a count (#202 PR-B).
        "episode_projection_root_sha256": episode_projection_root_sha256,
        "case_n": plan.case_n,
        "cluster_n": plan.cluster_n,
        "target_case_ids": list(plan.target_case_ids),
        "target_failure_cluster_ids": list(plan.target_failure_cluster_ids),
        "control_case_ids": list(plan.control_case_ids),
        "control_cluster_ids": list(plan.control_cluster_ids),
        "optimizer_case_ids": list(plan.optimizer_case_ids),
        **optimizer_population_identity(plan),
        "excluded_case_ids": list(plan.excluded_case_ids),
        "exclusion_reasons": dict(plan.exclusion_reasons),
        "target_predictors": list(plan.target_predictors),
        "target_dimensions": list(plan.target_dimensions),
        "exact_gold_coverage": dict(plan.exact_gold_coverage),
        "owner_distribution": dict(plan.owner_distribution),
        "blocking_reasons": list(plan.blocking_reasons),
        # The halves the winner was picked on. Registration re-derives the plan from the frozen corpus and
        # compares this, which is the one thing in the summary a second party can disagree with — the rest
        # is membership the re-derivation reproduces by construction.
        "split": dict(plan.split or {}),
    }


def plan_blockers(plan: GepaObjectivePlan) -> tuple[str, ...]:
    """Why this corpus cannot be optimized, in the same words `run_gepa` refuses with.

    Checked here so the refusal is a terminal report rather than a traceback, and so it costs nothing: this
    runs before any endpoint is touched, which is the same answer `readiness` gives with zero model calls.
    """

    reasons: list[str] = []
    if not plan.target_failure_cluster_ids:
        reasons.append("news_program_compile_no_verified_failure_clusters")
    if not plan.control_cluster_ids:
        reasons.append("news_program_compile_no_correct_control_clusters")
    if plan.split is None:
        reasons.append(plan.split_error or "news_program_compile_objective_split_unavailable")
    reasons.extend(plan.blocking_reasons)
    return tuple(reasons)


def require_judge_identity(judge: Any, *, budget: OptimizationBudget) -> dict[str, Any]:
    """The judge's complete role contract, and its own admission ceiling bound to this budget.

    The metric calls the judge directly, so `_BudgetedLM` never sees those requests: the judge admits them
    itself, atomically, before each provider call. That is a real pre-call bound — but only if the ceiling
    it admits against is the one the operator declared here, which is what this checks. Without it a judge
    built with a larger ceiling could outspend `max_metric_judge_model_calls` and be discovered afterwards,
    which is a report, not a budget.

    The role binding is required for the same reason `require_model_identity` is required of the other two:
    a candidate that retains judge-derived scores without naming the endpoint and execution contract that
    produced them makes two runs judged by different models indistinguishable in provenance.
    """

    identity = dict(getattr(judge, "identity", {}) or {})
    execution = identity.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError("news_program_compile_metric_judge_identity_unavailable")
    binding = execution.get("role_binding")
    if not isinstance(binding, Mapping) or binding.get("role") != "metric_judge":
        raise ValueError("news_program_compile_metric_judge_identity_unavailable")
    ModelExecutionIdentity.model_validate(dict(binding))
    admitted = execution.get("max_model_calls")
    if type(admitted) is not int or admitted <= 0 or admitted > budget.max_metric_judge_model_calls:
        raise ValueError("news_program_compile_metric_judge_call_budget_unbound")
    return identity


def optimize(dataset: FrozenDevelopmentDataset, config: OptimizationConfig) -> OptimizationResult:
    """Run the one bounded GEPA optimization over a frozen corpus and return its terminal state."""

    if config.judge is None:
        raise ValueError("news_program_compile_metric_judge_required")
    # Before anything is spent: the three roles answer under identities they were stamped with, or the run
    # does not start. Reconstructing an identity from the object it describes would attest nothing.
    task_identity = require_model_identity(config.task_lm, role="task")
    reflection_identity = require_model_identity(config.reflection_lm, role="reflection")
    judge_identity = require_judge_identity(config.judge, budget=config.budget)
    identities = {
        "task": task_identity.model_dump(mode="json"),
        "reflection": reflection_identity.model_dump(mode="json"),
        "metric_judge": judge_identity,
    }
    # The wall clock is checked before each call, not during one: a request already in flight runs to its
    # own attested deadline, and clamping that deadline would break the very role contract
    # `ModelExecutionIdentity` exists to attest. So the worst case is `max_wall_clock_seconds` plus one
    # call, and a budget that cannot even bound one call is not a bound — a 60 s budget that still waits
    # 300 s for a reflection response would be a number, not a deadline. Refused here, before anything is
    # spent, against the deadlines these three roles actually carry.
    longest_call_seconds = max(
        float(task_identity.timeout_seconds),
        float(reflection_identity.timeout_seconds),
        float(judge_identity["execution"].get("timeout_seconds") or 0.0),
    )
    if config.budget.max_wall_clock_seconds < longest_call_seconds:
        raise ValueError(f"news_learning_optimize_wall_clock_below_call_deadline:{longest_call_seconds:g}")
    started_at_ms = config.now_ms()
    plan = build_gepa_objective_plan(dataset.episodes)
    objective = objective_summary(plan, episode_projection_root_sha256=dataset.ref.episode_projection_root_sha256)
    blockers = plan_blockers(plan)
    if blockers:
        return _terminal(
            "REJECTED",
            dataset=dataset,
            config=config,
            objective=objective,
            identities=identities,
            usage=_usage(meter=None, judge=config.judge, metric_calls=0, budgeted=(), budget=config.budget),
            reasons=blockers,
            started_at_ms=started_at_ms,
        )

    meter = _BudgetMeter(
        config.budget,
        imputed_call_cost_microusd=config.budget.max_call_cost_microusd,
        monotonic=config.monotonic,
        max_wall_clock_seconds=config.budget.max_wall_clock_seconds,
    )
    task_lm = _BudgetedLM(config.task_lm, role="task", meter=meter)
    reflection_lm = _BudgetedLM(config.reflection_lm, role="reflection", meter=meter)
    budgeted = (task_lm, reflection_lm)
    run: GepaRunResult | None = None
    reasons: tuple[str, ...] = ()
    outcome: Literal["NO_OP", "REJECTED", "ADVANCE"] = "ADVANCE"
    try:
        run = run_gepa(
            base_program=dataset.parent_program,
            episodes=dataset.episodes,
            task_lm=task_lm,
            reflection_lm=reflection_lm,
            judge=config.judge,
            max_metric_calls=config.budget.max_metric_calls,
            seed=config.budget.seed,
            review_rubric_version=dataset.ref.review_rubric_version,
            optimizer_factory=config.optimizer_factory,
        )
    except GepaNoProgramChange as exc:
        # A complete run that kept the seed. The receipts are the run's, not an empty stand-in.
        run, outcome, reasons = exc.result, "NO_OP", (_NO_OP_CODE,)
    except OptimizationBudgetExceeded as exc:
        outcome, reasons = "REJECTED", (str(exc),)
    except ValueError as exc:
        code = str(exc)
        if code.startswith(_REJECTION_PREFIXES):
            outcome, reasons = "REJECTED", (code,)
        else:
            raise

    metric_calls = run.metric_calls if run is not None else 0
    usage = _usage(meter=meter, judge=config.judge, metric_calls=metric_calls, budgeted=budgeted, budget=config.budget)
    # The split and the recall receipt are corpus facts, known before the first call. A run the budget cut
    # short still publishes them, so "which halves was this scored on" is answerable for every terminal
    # state past the pre-flight — only the run's own outputs are absent when there was no run.
    receipts: dict[str, Mapping[str, Any]] = {
        "split": plan.split or {},
        "retrieval": retrieval_receipt(dataset.episodes),
    }
    if run is not None:
        receipts |= {
            "split": run.split,
            "retrieval": run.retrieval,
            "metric": run.metric,
            "optimizer": run.optimizer_config,
            "trajectory": run.trajectory,
            "checkpoint": run.checkpoint,
        }
    if run is None:
        return _terminal(
            outcome,
            dataset=dataset,
            config=config,
            objective=objective,
            identities=identities,
            usage=usage,
            reasons=reasons,
            started_at_ms=started_at_ms,
            receipts=receipts,
        )
    if outcome != "ADVANCE":
        return _terminal(
            outcome,
            dataset=dataset,
            config=config,
            objective=objective,
            identities=identities,
            usage=usage,
            reasons=reasons,
            started_at_ms=started_at_ms,
            receipts=receipts,
        )

    spent = _overspend(usage, budget=config.budget, elapsed_seconds=meter.elapsed_seconds)
    if spent:
        return _terminal(
            "REJECTED",
            dataset=dataset,
            config=config,
            objective=objective,
            identities=identities,
            usage=usage,
            reasons=spent,
            started_at_ms=started_at_ms,
            receipts=receipts,
        )

    try:
        patch = PromptPatchV1.of(run.patch)
    except ValueError as exc:
        return _terminal(
            "REJECTED",
            dataset=dataset,
            config=config,
            objective=objective,
            identities=identities,
            usage=usage,
            reasons=(str(exc),),
            started_at_ms=started_at_ms,
            receipts=receipts,
        )
    if not patch.changes(dataset.parent_program):
        return _terminal(
            "NO_OP",
            dataset=dataset,
            config=config,
            objective=objective,
            identities=identities,
            usage=usage,
            reasons=(_NO_OP_CODE,),
            started_at_ms=started_at_ms,
            receipts=receipts,
        )

    candidate = PromptCandidateV1.issue(
        parent_program_sha256=dataset.parent_program.program_sha256,
        development_dataset_sha256=dataset.ref.development_dataset_sha256,
        target_runtime_manifest_sha256=dataset.target_runtime_manifest_sha256,
        patch=patch,
        objective_summary=objective,
        optimizer=run.optimizer_config,
        model_identities=identities,
        budget=config.budget.model_dump(mode="json"),
        usage=usage,
        created_at_ms=started_at_ms,
    )
    return _terminal(
        "ADVANCE",
        dataset=dataset,
        config=config,
        objective=objective,
        identities=identities,
        usage=usage,
        reasons=(),
        started_at_ms=started_at_ms,
        candidate=candidate,
        receipts=receipts,
    )


def _terminal(
    outcome: Literal["NO_OP", "REJECTED", "ADVANCE"],
    *,
    dataset: FrozenDevelopmentDataset,
    config: OptimizationConfig,
    objective: Mapping[str, Any],
    identities: Mapping[str, Any],
    usage: Mapping[str, Any],
    reasons: Sequence[str],
    started_at_ms: int,
    candidate: PromptCandidateV1 | None = None,
    receipts: Mapping[str, Mapping[str, Any]] | None = None,
) -> OptimizationResult:
    produced = dict(receipts or {})
    report = OptimizationRunReport.issue(
        outcome=outcome,
        dataset=dataset.ref,
        parent_program_sha256=dataset.parent_program.program_sha256,
        target_runtime_manifest_sha256=dataset.target_runtime_manifest_sha256,
        objective=dict(objective),
        split=produced.get("split"),
        retrieval=produced.get("retrieval"),
        metric=produced.get("metric"),
        optimizer=produced.get("optimizer"),
        trajectory=produced.get("trajectory"),
        checkpoint=produced.get("checkpoint"),
        model_identities=dict(identities),
        budget=config.budget.model_dump(mode="json"),
        usage=dict(usage),
        reasons=tuple(reasons),
        started_at_ms=started_at_ms,
        completed_at_ms=config.now_ms(),
        candidate_sha256=candidate.candidate_sha256 if candidate is not None else None,
    )
    return OptimizationResult(outcome=outcome, report=report, candidate=candidate)


def _usage(
    *,
    meter: _BudgetMeter | None,
    judge: Any,
    metric_calls: int,
    budgeted: Sequence[_BudgetedLM],
    budget: OptimizationBudget,
) -> dict[str, Any]:
    stats = dict(getattr(judge, "stats", {}) or {})
    judge_calls = int(stats.get("model_calls", 0))
    # Charged the same way an unpriced task call is: neither endpoint this project runs on returns a
    # resolvable price, so a judge that reports zero cost for real calls would make the run's total spend
    # look free and let `_overspend` pass a run that was not within budget.
    reported = int(stats.get("actual_cost_microusd", 0))
    imputed = judge_calls * budget.max_call_cost_microusd
    judge_cost = max(reported, imputed) if judge_calls else reported
    return {
        "schema": USAGE_SCHEMA,
        "task_model_calls": meter.task_model_calls if meter else 0,
        "reflection_model_calls": meter.reflection_model_calls if meter else 0,
        "task_cost_microusd": meter.task_cost_microusd if meter else 0,
        "reflection_cost_microusd": meter.reflection_cost_microusd if meter else 0,
        "imputed_cost_calls": meter.imputed_cost_calls if meter else 0,
        "metric_judge_attempts": int(stats.get("attempts", 0)),
        "metric_judge_model_calls": judge_calls,
        "metric_judge_failures": int(stats.get("failures", 0)),
        "metric_judge_cost_microusd": judge_cost,
        "metric_judge_cost_imputed": judge_calls > 0 and judge_cost > reported,
        "actual_cost_microusd": (meter.actual_cost_microusd if meter else 0) + judge_cost,
        "metric_calls": metric_calls,
        "transport_failures": sum(lm.transport_failures for lm in budgeted),
        "transport_retries": sum(lm.transport_retries for lm in budgeted),
    }


def _overspend(usage: Mapping[str, Any], *, budget: OptimizationBudget, elapsed_seconds: float) -> tuple[str, ...]:
    """The bounds the meter cannot stop mid-call: the judge's own spend, and the clock.

    The judge is metered by the metric, not by `_BudgetedLM`, so its calls only become visible once the run
    returns. A run that finished over budget still gets a complete terminal report — it just does not get a
    candidate.
    """

    reasons: list[str] = []
    if int(usage["metric_judge_model_calls"]) > budget.max_metric_judge_model_calls:
        reasons.append("news_program_compile_metric_judge_call_budget_exceeded")
    if int(usage["actual_cost_microusd"]) > budget.max_cost_microusd:
        reasons.append("news_program_compile_cost_budget_exceeded")
    if elapsed_seconds > budget.max_wall_clock_seconds:
        reasons.append("news_learning_optimize_wall_clock_exhausted")
    return tuple(reasons)


__all__ = [
    "METRIC_JUDGE_MAX_TOKENS",
    "METRIC_JUDGE_TIMEOUT_SECONDS",
    "OBJECTIVE_SUMMARY_SCHEMA",
    "REFLECTION_MAX_TOKENS",
    "REFLECTION_TIMEOUT_SECONDS",
    "USAGE_SCHEMA",
    "FrozenDevelopmentDataset",
    "GepaNoProgramChange",
    "GepaRunResult",
    "ModelExecutionIdentity",
    "OptimizationBudgetExceeded",
    "OptimizationConfig",
    "OptimizerFactory",
    "OptimizerRole",
    "RulePackAwareProposer",
    "_FeedbackCompileProgram",
    "build_optimizer_lm",
    "checkpoint_receipt",
    "generated_default_instruction",
    "gepa_metric_call_ceiling",
    "objective_summary",
    "optimize",
    "optimizer_config_receipt",
    "optimizer_constructor",
    "plan_blockers",
    "require_model_identity",
    "restore_empty_advisories",
    "run_gepa",
    "trajectory_receipt",
]
