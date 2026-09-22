"""The one bounded offline optimizer that can produce a News Program candidate.

Public ``dspy.GEPA`` compiles exactly one native ``NativeNewsProgram`` Predictor against accepted Gold.
Which one is the run's *target*: ``classification`` optimizes ``taxonomy``, ``understanding`` optimizes
``event_semantics``, ``explanation`` optimizes ``reader_card``. There is one GEPA assembly, one budget
meter and one audited ledger for all three; what varies is the metric callable, which is injected as a
``TargetMetric``, and the frozen example each target renders from the same Objective Plan episodes.

The ``TargetMetric`` interface is stable and deliberately narrow: ``__call__(gold, pred, trace=None,
pred_name=None, pred_trace=None) -> dspy.Prediction(score, feedback, objective_scores)``. Refining what a
target *means* by "better" is a change to one metric object and its receipt, never to the assembly below.
The ``understanding`` and ``explanation`` rulers here are minimal and real — typed validity, accepted
asset/novelty agreement, and the deterministic ReaderCard lint with accepted-copy retention — and a
following unit sharpens their scoring semantics against this same interface.

Task and reflection calls share one audited ledger and one physical-call meter. Admission is GEPA's own
answer: the candidate at ``best_idx`` advances when its selection score is strictly above the seed's and
its instruction is valid, and otherwise the run is ``NO_OP``. The winner is that Predictor's native
``dump_state()`` — demos included — merged into the parent state for that Predictor alone; the other two
stay byte-identical. The module owns no persistence, activation, canary, or promotion authority. A run
ends in ``NO_OP``, ``REJECTED``, or ``ADVANCE``, and an ``ADVANCE`` candidate is still subject to every
downstream release gate, which is where a candidate that overfit the selection set is caught.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.metadata
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

import dspy  # type: ignore[import-untyped]
from dspy.teleprompt.gepa.gepa import AUTO_RUN_SETTINGS, DspyGEPAResult  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..artifact_identity import canonical_json, canonical_sha
from ..program.artifact import (
    NewsProgramStateV1,
    load_stable_program_state,
    render_model_evidence_json,
    validate_program_instruction,
)
from ..program.contracts import ReaderCardSemanticView, ScoredJudgment
from ..program.lm import (
    AuditedConfiguredLM,
    LMCallContext,
    LMCallLedger,
    LMCallReceipt,
    LMOutputTruncatedError,
    RuntimeModelIdentity,
    StructuredOutputMode,
    _usage_values,
    program_json_adapter,
)
from ..program.module import NativeNewsProgram
from ..program.runtime import PREDICTOR_NAMES, PROGRAM_VERSION, PredictorName, _estimated_tokens
from ..program.signatures import EventSemantics, ReaderCard
from ..taxonomy import ModelTaxonomyV1
from .contracts import (
    LEARNING_TARGETS,
    REFLECTION_MAX_TOKENS,
    REFLECTION_MINIBATCH_SIZE,
    REFLECTION_TIMEOUT_SECONDS,
    DevelopmentDatasetRef,
    LearningTarget,
    ModelExecutionIdentity,
    OptimizationBudget,
    OptimizationResult,
    OptimizationRunReport,
    OptimizerRole,
    PromptCandidateV1,
)
from .metric import _json_safe
from .objective import (
    TARGET_PREDICTORS,
    DevelopmentEpisode,
    GepaObjectivePlan,
    build_gepa_objective_plan,
    build_readiness_report,
    optimizer_population_identity,
    retrieval_receipt,
)
from .supervision import project_supervision
from .target_metrics import (
    TASK_OUTPUT_INVALID,
    TASK_OUTPUT_TRUNCATED,
    ZERO_OBJECTIVES,
    accepted_assets,
    accepted_duplicate_of,
    accepted_explanation,
    accepted_novelty,
    accepted_semantics,
    bind_target_metric,
    target_metric_receipt,
)

# v4 (#501): the population is `included`/`excluded`; no target/control split, no owner distribution.
# v5 (#651): the summary names the optimization `target`, because the same corpus now feeds three
# different Predictors and a candidate that does not say which one it moved is unreadable.
OBJECTIVE_SUMMARY_SCHEMA = "tracefold.news.optimization_objective_summary.v5"
# v3 (#456): `metric_calls` is null when GEPA terminates before returning its public result. The physical
# task/reflection call counters remain exact; zero is reserved for a preflight refusal that ran no metric.
USAGE_SCHEMA = "tracefold.news.optimization_usage.v3"

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
    """The typed candidate state and compact evidence produced by one public DSPy compile."""

    target: OptimizationTarget
    state: NewsProgramStateV1
    metric: dict[str, Any]
    optimizer_config: dict[str, Any]
    public_result: dict[str, Any]
    # The scalar cannot answer whether the winner was selected on examples it never trained on, so the
    # disjoint split and retrieval diagnostics remain public corpus evidence beside native GEPA state.
    split: dict[str, Any]
    retrieval: dict[str, Any]
    optimizer_cluster_ids: tuple[str, ...] = Field(min_length=1)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    metric_calls: int = Field(ge=0)
    train_count: int = Field(gt=0)
    val_count: int = Field(gt=0)


# --- the bounded GEPA run (was `compiler/gepa.py`) ------------------------------------------------

# The task route answers the Program's own schemas, so it keeps production's determinism: temperature 0 and
# the route's own token ceiling. The reflection role does something else entirely — it reads a minibatch of
# failures and writes a whole new instruction — and the guidance for it is the opposite on every axis. Until
# #143 both were built from the task route's numbers, which capped a proposed instruction below what the
# instruction bound itself accepts and gave a reflection call the 20 s route deadline.
_REFLECTION_TEMPERATURE = 1.0
_TASK_TEMPERATURE = 0
_OWNED_LM_KWARGS: Final[frozenset[str]] = frozenset(
    {
        "api_base",
        "api_key",
        "authorization",
        "base_url",
        "cache",
        "headers",
        "max_tokens",
        "model",
        "num_retries",
        "password",
        "secret",
        "structured_output",
        "temperature",
        "timeout",
        "token",
        "transport",
    }
)


# The bounds a proposal can still fail (#319 removed the marker and credential codes with the checks that
# raised them). Each one is a fact about the optimization loop rather than about a hostile text: a hash
# needs one encoding, every call pays for these bytes, and a Predictor with no prompt is not a Predictor.
_INSTRUCTION_REJECTIONS = (
    "news_program_instruction_too_large",
    "news_program_instruction_unicode_noncanonical",
    "news_program_instruction_empty",
)


def _instruction_rejection_code(exc: BaseException) -> str | None:
    """Whether this failure is the instruction safety bound refusing a proposal, and which bound it was."""

    text = str(exc)
    return next((marker for marker in _INSTRUCTION_REJECTIONS if marker in text), None)


class GepaNoProgramChange(ValueError):
    """The optimizer kept the seed: a complete run that learned nothing.

    A `ValueError` whose message is the code this has always raised, so every existing caller and every
    existing assertion is unchanged. What it adds is the compact run evidence needed to publish a complete
    terminal answer while official GEPA state remains the sole trajectory/checkpoint record.
    """

    def __init__(self, result: GepaRunResult) -> None:
        super().__init__("news_program_compile_no_program_change")
        self.result = result


# The two candidate-local task failures this module converts and `target_metrics` scores at zero.
_TASK_OUTPUT_FAILURE = TASK_OUTPUT_TRUNCATED
_TASK_OUTPUT_INVALID = TASK_OUTPUT_INVALID


# One vocabulary, defined beside the corpus contracts so a caller that only needs the names does not
# import DSPy to read them (#651 §9).
OptimizationTarget = LearningTarget

OPTIMIZATION_TARGETS: Final[tuple[OptimizationTarget, ...]] = LEARNING_TARGETS

# One target optimizes one Predictor. Nothing else in this module branches on the target name.


class TargetMetric(Protocol):
    """The whole interface a target's ruler exposes to the single native GEPA assembly.

    `dspy.GEPA` calls a metric as `(gold, pred, trace, pred_name, pred_trace)` and reads `score` and
    `feedback` off the returned `dspy.Prediction`; the optional `objective_scores` mapping is the per-axis
    breakdown GEPA aggregates into `val_aggregate_subscores` and this module publishes in the selection
    receipt. Everything else about a target — which Gold a case must carry, how a typed failure scores,
    what the feedback says — lives behind this call, so refining one target's scoring semantics is a change
    to one object rather than to the optimizer.
    """

    def __call__(
        self,
        gold: dspy.Example,
        pred: dspy.Prediction,
        trace: Any = None,
        pred_name: str | None = None,
        pred_trace: Any = None,
    ) -> dspy.Prediction: ...


class _LearningStudent(dspy.Module):  # type: ignore[misc]
    """One native Predict, plus the minimal accommodation of one reproduced DSPy 3.3.1 limit.

    Reproduced against the installed dspy 3.3.1 rather than assumed (#478, re-checked for #651). GEPA
    evaluates a candidate through `bootstrap_trace.bootstrap_trace_data`, whose `patched_forward` handles
    `AdapterParseError` and then re-raises everything else — `except LMError: raise`, and `except
    Exception: if not capture_crashes: raise`, with `capture_crashes` left at its default. So both of this
    Program's candidate-local task failures escape the wrapper:

    * `LMOutputTruncatedError` (an `LMError`) when the task model stops mid-JSON, and
    * a pydantic `ValidationError` when the typed output field does not validate.

    `Evaluate` then records the example as an error, its `prediction` is not the `(prediction, trace)`
    tuple the caller unpacks, and `bootstrap_trace_data` *drops* that row (`except ValueError: continue`).
    GEPA receives a trajectory list shorter than the batch it submitted and indexes past the end. The
    failure is therefore a crashed run, not a low score — which is the opposite of what a candidate-local
    output failure should mean.

    This wrapper is the narrowest fix: it preserves the one native Predict, converts only those two
    candidate-local failures into ordinary Predictions, and leaves the metric to score them at
    `failure_score`. Everything else still propagates, because a provider outage or a budget refusal is a
    run answer rather than a candidate quality.
    """

    def __init__(self, predictor: dspy.Predict, *, output_type: type[BaseModel]) -> None:
        super().__init__()
        self.predictor = predictor
        self.output_type = output_type

    def forward(self, **inputs: Any) -> dspy.Prediction:
        try:
            return self.predictor(**inputs)
        except LMOutputTruncatedError:
            return dspy.Prediction(task_output_failure=_TASK_OUTPUT_FAILURE)
        except ValidationError as exc:
            if exc.title != self.output_type.__name__:
                raise
            return dspy.Prediction(
                task_output_failure=_TASK_OUTPUT_INVALID,
                task_output_feedback=f"Typed {self.output_type.__name__} is invalid: {exc}",
            )


# --- the frozen example each target renders, and the plan that binds it to its ruler ---------------
#
# The rulers themselves left this module in #651 §8. Three metrics living beside the GEPA assembly is how
# the taxonomy comparison ended up implemented twice — once here and once in `learning/metric.py` — and a
# candidate could then be admitted by one number and reported by another. `learning/target_metrics.py` is
# the single owner; what stays here is the *question*: which frozen inputs and which accepted Gold each
# target's example carries, which is a property of the corpus rather than of the ruler.


def _classification_example(episode: DevelopmentEpisode) -> dspy.Example:
    gold = dict(episode.accepted_review or {}).get("taxonomy")
    if gold is None:
        raise ValueError("news_program_compile_taxonomy_gold_missing")
    return dspy.Example(
        evidence_json=render_model_evidence_json(episode.context.taxonomy_payload(), predictor="taxonomy"),
        gold_taxonomy=gold,
        applicable_targets=tuple(episode.applicable_targets),
        case_id=episode.case_id,
        cluster_id=episode.cluster_id,
    ).with_inputs("evidence_json")


def _understanding_example(episode: DevelopmentEpisode) -> dspy.Example:
    """The typed-semantics question plus every accepted fact about it, including what the model was shown.

    `gold_told_event_ids` is the frozen ledger in the exact order `restates` indexes, because a
    restatement's *target* is half its answer and a ruler that cannot see the ledger cannot check it.
    Only explicitly accepted duplicate targets can substitute for the exact target.
    """

    review = dict(episode.accepted_review or {})
    told_event_ids = tuple(str(entry.event_id) for entry in episode.context.told.entries)
    values: dict[str, Any] = {
        "evidence_json": render_model_evidence_json(
            episode.context.event_semantics_payload(), predictor="event_semantics"
        ),
        "applicable_targets": tuple(episode.applicable_targets),
        "gold_told_event_ids": told_event_ids,
        "case_id": episode.case_id,
        "cluster_id": episode.cluster_id,
    }
    review["supervision"] = project_supervision(
        review,
        episode.production_judgment.model_dump(mode="json") if episode.production_judgment else None,
        told_event_ids=told_event_ids,
    )
    values["gold_semantics"] = accepted_semantics(review)
    values["gold_novelty_exclusion"] = review["supervision"]["missing"].get("novelty")
    assets = accepted_assets(review)
    if assets is not None:
        values["gold_assets"] = assets
    novelty = accepted_novelty(review)
    if novelty is not None:
        values["gold_novelty"] = novelty
        values["gold_duplicate_of"] = accepted_duplicate_of(review)
        values["gold_duplicate_targets"] = tuple(review["supervision"]["labels"].get("duplicate_targets", ()))
    return dspy.Example(**values).with_inputs("evidence_json")


def _explanation_example(episode: DevelopmentEpisode) -> dspy.Example:
    """One frozen ReaderCard question: the bounded evidence plus the semantics the episode recorded.

    `semantics_json` is a ReaderCard input, not something the Predictor decides, so it comes from the
    episode's own recorded EventSemantics rather than from a live upstream call. An episode with no
    recorded judgment cannot pose this question and is refused here rather than being scored against an
    invented semantic view.
    """

    judgment = episode.production_judgment
    if judgment is None:
        raise ValueError("news_program_compile_reader_card_semantics_missing")
    review = dict(episode.accepted_review or {})
    explanation = accepted_explanation(review)
    evidence_json = render_model_evidence_json(episode.context.reader_card_payload(), predictor="reader_card")
    values: dict[str, Any] = {
        "evidence_json": evidence_json,
        "semantics_json": _recorded_semantics_json(judgment),
        "source_title": str(episode.context.evidence.title),
        "applicable_targets": tuple(episode.applicable_targets),
        "gold_key_facts": explanation["key_facts"],
        "gold_forbidden_claims": explanation["forbidden_claims"],
        "gold_error_types": explanation["error_types"],
        "case_id": episode.case_id,
        "cluster_id": episode.cluster_id,
    }
    return dspy.Example(**values).with_inputs("evidence_json", "semantics_json")


def _recorded_semantics_json(judgment: ScoredJudgment) -> str:
    """Re-render the episode's recorded semantics in exactly the view the Program feeds ReaderCard."""

    verdict = judgment.verdict
    if verdict.fact_kind is None:
        # A `news_judgment_v2` episode states no kind, and the view requires one: the ReaderCard
        # Predictor is being fed the semantics a v3 Program produces, so a v2 recording cannot be
        # rendered into it without inventing the field (#675 §1).
        raise ValueError("news_program_recorded_semantics_pre_v3")
    view = ReaderCardSemanticView(
        assets=verdict.assets,
        direction=verdict.direction,
        fact_kind=verdict.fact_kind,
        novelty=verdict.novelty,
        restates=verdict.restates,
        scope=verdict.scope,
    )
    return canonical_json(view.model_dump(mode="json"))


def cluster_event_index(episodes: Sequence[DevelopmentEpisode]) -> dict[str, str]:
    """Which connected fact cluster each frozen Event belongs to, for the restatement-target check."""

    return {str(episode.context.evidence.event_id): str(episode.cluster_id) for episode in episodes}


@dataclass(frozen=True)
class _TargetPlan:
    """Everything the single GEPA assembly needs to optimize one Predictor for one target."""

    target: OptimizationTarget
    predictor: PredictorName
    output_type: type[BaseModel]
    metric: TargetMetric
    example: Callable[[DevelopmentEpisode], dspy.Example]
    metric_receipt: dict[str, Any]
    zero_objectives: Callable[[], dict[str, float]]


def target_plan(
    target: OptimizationTarget,
    *,
    review_rubric_version: str,
    judge: Any = None,
    judge_calibration_receipt_sha256: str = "",
) -> _TargetPlan:
    """Resolve one target into its Predictor, ruler, example renderer and metric receipt.

    `judge` is the metric-judge route. Semantic explanation optimization requires it; an explicitly
    selected proxy experiment omits it. Every consumer records which ruler actually ran.
    """

    if target not in TARGET_PREDICTORS:
        raise ValueError(f"news_program_compile_target_unknown:{target}")
    metric = cast(TargetMetric, bind_target_metric(target, judge))
    receipt = target_metric_receipt(
        target,
        review_rubric_version=review_rubric_version,
        judge=judge,
        judge_calibration_receipt_sha256=judge_calibration_receipt_sha256,
    )
    if target == "classification":
        return _TargetPlan(
            target=target,
            predictor="taxonomy",
            output_type=ModelTaxonomyV1,
            metric=metric,
            example=_classification_example,
            metric_receipt=receipt,
            zero_objectives=ZERO_OBJECTIVES["classification"],
        )
    if target == "understanding":
        return _TargetPlan(
            target=target,
            predictor="event_semantics",
            output_type=EventSemantics,
            metric=metric,
            example=_understanding_example,
            metric_receipt=receipt,
            zero_objectives=ZERO_OBJECTIVES["understanding"],
        )
    return _TargetPlan(
        target=target,
        predictor="reader_card",
        output_type=ReaderCard,
        metric=metric,
        example=_explanation_example,
        metric_receipt=receipt,
        zero_objectives=ZERO_OBJECTIVES["explanation"],
    )


def _predictor_change_receipt(
    base_program: NewsProgramStateV1,
    *,
    target: OptimizationTarget,
    predictor: PredictorName,
    winner_document: Mapping[str, Any],
) -> dict[str, Any]:
    """What moved, per Predictor, in bytes a reviewer can read before promoting anything."""

    before = base_program.instruction_for(predictor)
    after = str(winner_document["signature"]["instructions"])
    before_demos = base_program.demos_for(predictor)
    after_demos = tuple(dict(demo) for demo in winner_document["demos"])
    receipt: dict[str, Any] = {
        "schema": "tracefold.news.predictor_state_change.v1",
        "target": target,
        "predictor": predictor,
        predictor: {
            "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
            "changed": after != before or after_demos != before_demos,
            "before_bytes": len(before.encode()),
            "after_bytes": len(after.encode()),
            "byte_growth": len(after.encode()) - len(before.encode()),
            "before_estimated_tokens": _estimated_tokens(before),
            "after_estimated_tokens": _estimated_tokens(after),
            "estimated_token_growth": _estimated_tokens(after) - _estimated_tokens(before),
            "before_demo_n": len(before_demos),
            "after_demo_n": len(after_demos),
            "unified_diff": "\n".join(
                difflib.unified_diff(
                    before.splitlines(),
                    after.splitlines(),
                    fromfile=f"stable/{predictor}",
                    tofile=f"winner/{predictor}",
                    lineterm="",
                )
            ),
        },
    }
    for other in PREDICTOR_NAMES:
        if other == predictor:
            continue
        receipt[other] = {
            "instruction_sha256": hashlib.sha256(base_program.instruction_for(other).encode()).hexdigest(),
            "demo_n": len(base_program.demos_for(other)),
            "unchanged": True,
        }
    return receipt


@dataclass(frozen=True)
class _GepaAdmission:
    selected_document: dict[str, Any]
    admitted: bool
    selection_receipt: dict[str, Any]
    public_result: dict[str, Any]


def _admit_public_gepa_candidates(
    *,
    run: DspyGEPAResult,
    base_document: Mapping[str, Any],
    val_count: int,
    metric_calls: int,
) -> _GepaAdmission:
    """GEPA's own winner, admitted when it is strictly better than the seed (#501 D4).

    No per-control replay, no per-objective check, no growth budget here: those are what the offline and
    holdout release gates already decide, and re-deciding them on the selection set only made ADVANCE
    unreachable — the v2 rule required every Stable-correct control to replay at exactly 1.0, which the
    seed itself did not satisfy.
    """

    scores = [float(value) for value in list(getattr(run, "val_aggregate_scores", ()) or ())]
    if not scores:
        raise ValueError("news_program_compile_selection_scores_invalid")
    if any(not math.isfinite(score) for score in scores):
        raise TypeError("news_program_compile_nonfinite_score")
    if len(run.candidates) != len(scores) or len(run.parents) != len(scores):
        raise ValueError("news_program_compile_public_result_invalid")
    objective_scores = run.val_aggregate_subscores
    if objective_scores is None or len(objective_scores) != len(scores):
        raise ValueError("news_program_compile_public_objective_scores_missing")
    val_subscores = run.val_subscores
    if len(val_subscores) != len(scores):
        raise ValueError("news_program_compile_public_validation_subscores_missing")
    expected_val_ids = set(range(val_count))
    if any(set(candidate_scores) != expected_val_ids for candidate_scores in val_subscores):
        raise ValueError("news_program_compile_public_validation_subscores_invalid")

    best = int(run.best_idx)
    if not 0 <= best < len(scores):
        raise ValueError("news_program_compile_public_result_invalid")
    best_document = _winning_predictor_document(run.candidates[best])
    best_instruction = str(best_document["signature"]["instructions"])
    instruction_valid = True
    try:
        validate_program_instruction(best_instruction)
    except ValueError as exc:
        if _instruction_rejection_code(exc) is None:
            raise
        instruction_valid = False
    # Demos count as a change: a candidate that keeps the seed instruction and attaches few-shot examples
    # is a different Program, which is exactly what the three-string write-set could not express.
    changed = best_document != dict(base_document)
    admitted = best != 0 and scores[best] > scores[0] and changed and instruction_valid
    selected_document = best_document if admitted else dict(base_document)
    baseline_objectives = {key: float(value) for key, value in objective_scores[0].items()}
    best_objectives = {key: float(value) for key, value in objective_scores[best].items()}
    selection = {
        "schema": "tracefold.news.target_selection_score.v4",
        "candidate_0": {"target_overall": scores[0], **baseline_objectives},
        "gepa_best_index": best,
        "gepa_best": {"target_overall": scores[best], **best_objectives},
        "gepa_best_instruction_valid": instruction_valid,
        "gepa_best_demo_n": len(best_document["demos"]),
        "admitted": admitted,
        "delta": {
            "target_overall": round(scores[best] - scores[0], 6),
            **{
                key: round(best_objectives.get(key, 0.0) - baseline_objectives.get(key, 0.0), 6)
                for key in sorted(set(baseline_objectives) | set(best_objectives))
            },
        },
    }
    return _GepaAdmission(
        selected_document=selected_document,
        admitted=admitted,
        selection_receipt=selection,
        public_result={
            "schema": "tracefold.news.dspy_gepa_public_result.v3",
            "candidate_count": len(run.candidates),
            "parents": run.parents,
            "validation_aggregate_scores": scores,
            "validation_subscores": [
                {str(key): float(value) for key, value in candidate_scores.items()}
                for candidate_scores in val_subscores
            ],
            "validation_aggregate_objective_scores": objective_scores,
            "gepa_best_index": best,
            "admitted": admitted,
            "total_metric_calls": metric_calls,
        },
    )


def resolve_auto_metric_calls(auto: str, *, val_count: int) -> int:
    """The metric-call budget `dspy.GEPA(auto=...)` will set for itself, computed the way it computes it.

    One Predictor is optimized, so ``num_preds`` is 1. Resolved before `compile` so the receipt and the
    end-of-step ceiling name the same number whether the compile ran or was injected.
    """

    if auto not in AUTO_RUN_SETTINGS:
        raise ValueError("news_program_compile_auto_budget_unknown")
    # `auto_budget` reads nothing from `self`; calling it unbound avoids building a throwaway optimizer.
    return int(
        dspy.GEPA.auto_budget(
            cast(Any, None),
            num_preds=1,
            num_candidates=AUTO_RUN_SETTINGS[auto]["n"],
            valset_size=val_count,
        )
    )


def run_gepa(
    *,
    base_program: NewsProgramStateV1,
    episodes: Sequence[DevelopmentEpisode],
    task_lm: dspy.BaseLM,
    reflection_lm: Any,
    seed: int,
    review_rubric_version: str,
    target: OptimizationTarget = "classification",
    judge: Any = None,
    explanation_protocol: Literal["semantic", "proxy"] = "semantic",
    judge_calibration_receipt_sha256: str = "",
    auto: str | None = None,
    max_metric_calls: int | None = None,
    compile_fn: Callable[..., dspy.Module] | None = None,
    gepa_log_dir: str | None = None,
) -> GepaRunResult:
    """Optimize exactly one Predictor — the one this `target` names — against accepted Gold."""

    if target == "explanation" and explanation_protocol == "semantic" and judge is None:
        raise ValueError("news_program_compile_metric_judge_required")
    if (auto is None) == (max_metric_calls is None):
        raise ValueError("news_program_compile_budget_requires_exactly_one_of_auto_or_max_metric_calls")
    if gepa_log_dir:
        log_path = Path(gepa_log_dir)
        if log_path.exists() and (not log_path.is_dir() or any(log_path.iterdir())):
            raise ValueError("news_program_compile_gepa_log_dir_not_empty")
    # One Objective Plan, built here rather than by each caller, so the corpus this optimization sees is the
    # corpus `readiness`, the dataset-bound baseline and `CandidateEvaluator` re-derive from the same frozen
    # episodes.
    plan = build_gepa_objective_plan(episodes, target)
    if not plan.optimizer_cluster_ids:
        raise ValueError(f"news_program_compile_no_labelled_clusters:{target}")
    if plan.split is None:
        # Verbatim: the plan records the exact code `_honest_split` refused with, so this stays the failure
        # the caller has always seen rather than a translation of it.
        raise ValueError(plan.split_error or "news_program_compile_objective_split_unavailable")
    if plan.blocking_reasons:
        raise ValueError("news_program_compile_objective_blocked:" + ",".join(plan.blocking_reasons))
    split_receipt = plan.split
    # The target consumes accepted duplicate alternatives, never split-group membership.
    resolved_target = target_plan(
        target,
        review_rubric_version=review_rubric_version,
        judge=judge,
        judge_calibration_receipt_sha256=judge_calibration_receipt_sha256,
    )
    train_examples = [resolved_target.example(episode) for episode in plan.train_episodes]
    val_examples = [resolved_target.example(episode) for episode in plan.development_selection_episodes]
    retrieval = retrieval_receipt(episodes)

    resolved_metric_calls = (
        int(max_metric_calls)
        if max_metric_calls is not None
        else resolve_auto_metric_calls(str(auto), val_count=len(val_examples))
    )
    constructor = optimizer_constructor(
        auto=auto,
        max_metric_calls=max_metric_calls,
        seed=seed,
        train_count=len(train_examples),
    )
    metric_errors: list[BaseException] = []

    def metric(
        gold: Any, pred: Any, trace: Any = None, pred_name: str | None = None, pred_trace: Any = None
    ) -> dspy.Prediction:
        if metric_errors:
            raise metric_errors[0]
        try:
            result = resolved_target.metric(gold, pred, trace, pred_name, pred_trace)
            if result.score is None:
                raise OptimizationRunTerminated(f"news_program_compile_metric_incomplete:{result.outcome}")
            return result
        except Exception as exc:
            metric_errors.append(exc)
            raise

    metric_receipt = resolved_target.metric_receipt
    student = _LearningStudent(
        getattr(NativeNewsProgram(base_program), resolved_target.predictor),
        output_type=resolved_target.output_type,
    )
    config_receipt = optimizer_config_receipt(
        constructor=constructor,
        target=resolved_target,
        resolved_metric_calls=resolved_metric_calls,
        task_lm=task_lm,
        reflection_lm=reflection_lm,
        metric_sha256=canonical_sha(metric_receipt),
        example_count=len(train_examples) + len(val_examples),
        train_count=len(train_examples),
        val_count=len(val_examples),
    )
    physical_stopper = getattr(task_lm, "stopper", None)

    def stopper(state: Any) -> bool:
        return bool(metric_errors) or (bool(physical_stopper(state)) if callable(physical_stopper) else False)

    optimizer = dspy.GEPA(
        metric=metric,
        reflection_lm=reflection_lm,
        instruction_proposer=None,
        # Off (#501 D7): dspy 3.3.1 renders format-failure feedback with a hard-coded ChatAdapter, which
        # describes a request shape this JSONAdapter program never sends.
        add_format_failure_as_feedback=False,
        log_dir=gepa_log_dir,
        gepa_kwargs={"stop_callbacks": stopper},
        **constructor,
    )
    scope_context = LMCallContext(
        program_version=PROGRAM_VERSION,
        program_sha256=base_program.program_sha256,
        context_sha256=canonical_sha(
            {
                "train": [example.case_id for example in train_examples],
                "val": [example.case_id for example in val_examples],
            }
        ),
    )
    task_ledger = getattr(task_lm, "ledger", None)
    reflection_ledger = getattr(reflection_lm, "ledger", None)
    if not isinstance(task_ledger, LMCallLedger) or reflection_ledger is not task_ledger:
        raise ValueError("news_program_compile_lm_ledger_mismatch")
    try:
        with task_ledger.scope(scope_context), dspy.context(lm=task_lm, adapter=program_json_adapter()):
            optimized = (compile_fn or optimizer.compile)(student, trainset=train_examples, valset=val_examples)
    finally:
        # DSPy may catch arbitrary metric exceptions and substitute failure_score.
        if metric_errors:
            raise metric_errors[0]

    # The learning wrapper translates only task-output truncation into a scored Prediction. A physical budget
    # refusal or systemic provider failure is a run answer, so reconcile it before looking at the returned
    # winner. The stopper merely avoids starting another GEPA step; this check is authoritative.
    raise_terminal = getattr(task_lm, "raise_if_terminal", None)
    if callable(raise_terminal):
        raise_terminal()

    run = getattr(optimized, "detailed_results", None)
    if not isinstance(run, DspyGEPAResult):
        raise ValueError("news_program_compile_detailed_results_missing")
    # In `auto` mode dspy fills `max_metric_calls` inside `compile`; the receipt already names the value
    # computed by the same formula, and the two must agree or the ceiling below is meaningless.
    filled = getattr(optimizer, "max_metric_calls", None)
    if isinstance(filled, int) and filled != resolved_metric_calls:
        raise ValueError(
            f"news_program_compile_metric_budget_unverifiable:auto={filled},resolved={resolved_metric_calls}"
        )

    reported_calls = getattr(run, "total_metric_calls", None)
    metric_calls = int(reported_calls) if isinstance(reported_calls, int) else -1
    ceiling = gepa_metric_call_ceiling(
        max_metric_calls=resolved_metric_calls,
        optimizer_config=config_receipt,
        expected_example_count=len(train_examples) + len(val_examples),
    )
    if metric_calls < 0 or metric_calls > ceiling:
        raise ValueError(
            "news_program_compile_metric_budget_unverifiable:"
            f"observed={metric_calls},requested={resolved_metric_calls},ceiling={ceiling}"
        )
    admission = _admit_public_gepa_candidates(
        run=run,
        base_document=base_program.predictor_document(resolved_target.predictor),
        val_count=len(val_examples),
        metric_calls=metric_calls,
    )
    # The one merge: this Predictor's native state replaces the parent's, and the other two are copied
    # byte for byte by `with_predictor_document`, which re-hashes the whole envelope.
    state = base_program.with_predictor_document(resolved_target.predictor, admission.selected_document)
    result = GepaRunResult(
        target=resolved_target.target,
        state=state,
        metric={
            **metric_receipt,
            "target_selection_score": admission.selection_receipt,
            "predictor_change": _predictor_change_receipt(
                base_program,
                target=resolved_target.target,
                predictor=resolved_target.predictor,
                winner_document=admission.selected_document,
            ),
        },
        optimizer_config=config_receipt,
        public_result=admission.public_result,
        split=split_receipt,
        retrieval=retrieval,
        optimizer_cluster_ids=plan.optimizer_cluster_ids,
        target_dimensions=plan.target_dimensions,
        metric_calls=metric_calls,
        train_count=len(train_examples),
        val_count=len(val_examples),
    )
    if not admission.admitted:
        raise GepaNoProgramChange(result)
    return result


def _winning_predictor_document(program: dspy.Module) -> dict[str, Any]:
    """Read the one public Predict from a GEPA candidate as its native state document.

    Demos are kept, not refused. Until #651 this raised on any demo, because the three-string write-set
    had nowhere to carry one — so every candidate GEPA produced with few-shot examples, the ones it is
    designed to produce, was a crash instead of a candidate.
    """

    predictors = dict(program.named_predictors())
    if len(predictors) != 1:
        raise ValueError("news_program_compile_result_type_invalid")
    predictor = next(iter(predictors.values()))
    if getattr(predictor, "lm", None) is not None:
        # A route baked into a candidate would be a second answer to "which endpoint runs this Predictor",
        # and the one that survives into a released image. Routes come from operator config only.
        raise ValueError("news_program_compile_result_carries_model_route")
    document = dict(predictor.dump_state())
    # Per-call scratch, which `Predict.reset()` empties and a released image never carries.
    document["traces"] = []
    document["train"] = []
    return document


def optimizer_constructor(
    *,
    seed: int,
    train_count: int,
    auto: str | None = None,
    max_metric_calls: int | None = None,
) -> dict[str, Any]:
    """The one public `dspy.GEPA` configuration this repository constructs.

    Exactly one of `auto` / `max_metric_calls`, passed through unchanged (#501 D6): `auto` is DSPy's own
    budget contract, and this repository adds no floor or preflight of its own on top of it.
    """

    if (auto is None) == (max_metric_calls is None):
        raise ValueError("news_program_compile_budget_requires_exactly_one_of_auto_or_max_metric_calls")
    budget: dict[str, Any] = {"auto": auto} if max_metric_calls is None else {"max_metric_calls": int(max_metric_calls)}
    return {
        **budget,
        # GEPA's default is 3, and 3 is too few for this metric. In the first real run every proposal was
        # skipped on an *exact* tie — 1.729166 vs 1.729166, 1.597917 vs 1.597917 — because a good instruction
        # here names recurring evidence patterns (a sentiment index, a comparison base, a crypto-linked
        # equity) that a 3-example sample almost never contains. The metric is also coarse, moving in steps
        # like 0 / 0.675 / 0.825 / 1.0, so ties are easy to hit and GEPA skips on a tie by rule. Six doubles
        # that signal while leaving the 32K-context thinking teacher room for its final proposal: a real
        # 10-example call consumed 22,782 input + 9,985 output tokens and ended exactly at 32,767.
        "reflection_minibatch_size": min(REFLECTION_MINIBATCH_SIZE, train_count),
        "candidate_selection_strategy": "pareto",
        "skip_perfect_score": True,
        "use_merge": True,
        "max_merge_invocations": 5,
        "num_threads": 1,
        "failure_score": 0.0,
        "perfect_score": 1.0,
        "track_stats": True,
        "track_best_outputs": False,
        "use_wandb": False,
        "use_mlflow": False,
        "seed": seed,
    }


def optimizer_config_receipt(
    *,
    constructor: dict[str, Any],
    target: _TargetPlan,
    resolved_metric_calls: int,
    task_lm: dspy.BaseLM,
    reflection_lm: Any,
    metric_sha256: str,
    example_count: int,
    train_count: int,
    val_count: int,
) -> dict[str, Any]:
    scalars = dict(_json_scalars(constructor))
    # `auto` resolves to a count inside dspy; the receipt carries both the declared mode and the number it
    # became, and the end-of-step ceiling reads the number.
    scalars.setdefault("auto", None)
    scalars["max_metric_calls"] = int(resolved_metric_calls)
    return {
        "schema": "tracefold.news.compile_optimizer_config_receipt.v9",
        "target": target.target,
        "target_predictor": target.predictor,
        "optimizer": {
            "implementation": "dspy.GEPA",
            "dspy_version": importlib.metadata.version("dspy"),
            "gepa_version": importlib.metadata.version("gepa"),
            "adapter": "tracefold.news.program.lm.program_json_adapter",
            "evaluator": f"LearningStudent(NativeNewsProgram.{target.predictor}) on one explicit task LM",
            "add_format_failure_as_feedback": False,
            "terminal_stopper": "shared_physical_lm_meter_system_failures_only",
            "upstream_fixed_arguments": {"display_progress_bar": True, "raise_on_exception": True},
        },
        "metric_sha256": metric_sha256,
        "constructor_scalar_arguments": scalars,
        "instruction_proposer": None,
        "admission": "gepa_best_idx_strictly_above_seed",
        "model_identities": {
            "task": require_model_identity(task_lm, role="task").model_dump(mode="json"),
            "reflection": require_model_identity(reflection_lm, role="reflection").model_dump(mode="json"),
        },
        "compile_call": {
            "teacher": None,
            "example_count": example_count,
            "trainset_count": train_count,
            "valset_count": val_count,
            "valset_identity": "disjoint_cluster_split",
        },
    }


def _build_learning_lm(
    *,
    role: Literal["task", "reflection", "metric_judge"],
    model_name: str,
    api_key: str,
    api_base: str,
    timeout: float,
    max_tokens: int,
    model_kwargs: Mapping[str, Any] | None = None,
    temperature: float,
    structured_output: StructuredOutputMode,
    ledger: LMCallLedger,
    delegate: dspy.BaseLM | None = None,
) -> AuditedConfiguredLM:
    extras = dict(model_kwargs or {})
    owned = sorted(key for key in extras if key.casefold() in _OWNED_LM_KWARGS)
    if owned:
        raise ValueError("news_program_compile_model_kwargs_owned:" + ",".join(owned))
    inner = delegate or dspy.LM(
        str(model_name),
        api_key=api_key,
        api_base=api_base,
        cache=False,
        num_retries=0,
        timeout=float(timeout),
        max_tokens=int(max_tokens),
        temperature=temperature,
        **extras,
    )
    if inner.model != str(model_name):
        raise ValueError("news_program_compile_lm_model_mismatch")
    provider = str(model_name).split("/", 1)[0] if "/" in str(model_name) else "openai"
    role_binding = ModelExecutionIdentity.issue(
        role=role,
        model=str(model_name),
        api_base=str(api_base),
        max_output_tokens=int(max_tokens),
        timeout_seconds=float(timeout),
        temperature=temperature,
        model_kwargs=extras,
    )
    lm = AuditedConfiguredLM(
        inner,
        structured_output=structured_output,
        runtime_identity=RuntimeModelIdentity.issue(
            provider=provider,
            model=str(model_name),
            model_sha256=canonical_sha(
                {
                    "model_execution_identity": role_binding.model_dump(mode="json"),
                    "structured_output": structured_output,
                }
            ),
        ),
        predictor=role,
        route="compile",
        model_binding=role,
        ledger=ledger,
        request_kwargs=extras,
    )
    lm.tracefold_compiler_endpoint_identity = role_binding
    return lm


def build_task_lm(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    timeout: float,
    max_tokens: int,
    model_kwargs: Mapping[str, Any] | None = None,
    temperature: float = _TASK_TEMPERATURE,
    structured_output: StructuredOutputMode = "json_schema",
    ledger: LMCallLedger,
    delegate: dspy.BaseLM | None = None,
) -> AuditedConfiguredLM:
    return _build_learning_lm(
        role="task",
        model_name=model_name,
        api_key=api_key,
        api_base=api_base,
        timeout=timeout,
        max_tokens=max_tokens,
        model_kwargs=model_kwargs,
        temperature=temperature,
        structured_output=structured_output,
        ledger=ledger,
        delegate=delegate,
    )


def build_reflection_lm(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    model_kwargs: Mapping[str, Any] | None = None,
    structured_output: StructuredOutputMode = "json_schema",
    ledger: LMCallLedger,
    delegate: dspy.BaseLM | None = None,
) -> AuditedConfiguredLM:
    return _build_learning_lm(
        role="reflection",
        model_name=model_name,
        api_key=api_key,
        api_base=api_base,
        timeout=REFLECTION_TIMEOUT_SECONDS,
        max_tokens=REFLECTION_MAX_TOKENS,
        model_kwargs=model_kwargs,
        temperature=_REFLECTION_TEMPERATURE,
        structured_output=structured_output,
        ledger=ledger,
        delegate=delegate,
    )


def require_model_identity(role_holder: Any, *, role: str) -> ModelExecutionIdentity:
    """The identity this endpoint will answer under, or a refusal before anything is spent.

    Reconstructing one from the object's own kwargs would attest nothing: the role contract — temperature,
    token ceiling, deadline — is exactly what an identity is for, so inferring it from the object it
    describes is circular.
    """

    identity = getattr(role_holder, "tracefold_compiler_endpoint_identity", None)
    if not isinstance(identity, ModelExecutionIdentity) or identity.role != role:
        raise ValueError("news_program_compile_endpoint_identity_unavailable")
    return identity


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
    "news_program_compile_no_labelled_clusters",
    "news_program_compile_objective_blocked",
    "news_program_compile_objective_split_unavailable",
    "news_program_compile_split_",
    "news_program_compile_metric_budget_unverifiable",
    "news_program_compile_detailed_results_missing",
    "news_program_compile_nonfinite_receipt_value",
    "news_program_instruction_",
)
_NO_OP_CODE = "news_program_compile_no_program_change"


class OptimizationRunTerminated(dspy.LMError):  # type: ignore[misc]
    """A bounded provider run ended without a candidate-quality answer."""


class OptimizationBudgetExceeded(OptimizationRunTerminated):
    """Raised before another model call, or after a provider reports overspend."""


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
        imputed_call_cost_microusd: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_wall_clock_seconds: float | None = None,
    ) -> None:
        self.budget = budget
        self.metric_judge_model_calls = 0
        self.metric_judge_total_tokens = 0
        self._lock = threading.RLock()
        self._reserved_cost = 0
        self.observed_cost_microusd = 0
        self.unknown_cost_calls = 0
        for suffix in (
            "model_calls",
            "cost_microusd",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "total_tokens",
        ):
            setattr(self, f"metric_judge_{suffix}", 0)
        # Neither the local llama.cpp endpoint nor DeepSeek returns a price litellm can resolve, so
        # `provider_cost_microusd` is `None` for endpoints whose provider cannot price the response. Charge
        # the operator's declared per-call ceiling instead, which stops the run early rather than late.
        self.imputed_call_cost_microusd = imputed_call_cost_microusd
        self.task_model_calls = 0
        self.reflection_model_calls = 0
        self.task_cost_microusd = 0
        self.reflection_cost_microusd = 0
        self.task_input_tokens = 0
        self.task_output_tokens = 0
        self.task_cached_tokens = 0
        self.task_total_tokens = 0
        self.reflection_input_tokens = 0
        self.reflection_output_tokens = 0
        self.reflection_cached_tokens = 0
        self.reflection_total_tokens = 0
        self.budget_cost_microusd = 0
        self.imputed_cost_calls = 0
        # DSPy's evaluator converts every Module Exception to `failure_score`. Keep the first run-level
        # failure out of band so a returned Program cannot launder exhaustion into a candidate score.
        self.first_terminal_error: BaseException | None = None
        self._monotonic = monotonic
        self._max_wall_clock_seconds = max_wall_clock_seconds
        self._started_monotonic = monotonic()

    @property
    def total_model_calls(self) -> int:
        return self.task_model_calls + self.reflection_model_calls + self.metric_judge_model_calls

    @property
    def elapsed_seconds(self) -> float:
        return self._monotonic() - self._started_monotonic

    def before(self, role: Literal["task", "reflection", "metric_judge"]) -> None:
        with self._lock:
            self.raise_if_terminal()
            if self._max_wall_clock_seconds is not None and self.elapsed_seconds >= self._max_wall_clock_seconds:
                raise self._refuse("news_learning_optimize_wall_clock_exhausted")
            used = getattr(self, f"{role}_model_calls")
            limit = getattr(self.budget, f"max_{role}_model_calls")
            if used >= limit:
                raise self._refuse(f"news_program_compile_{role}_model_call_budget_exhausted")
            if (
                self.budget_cost_microusd + self._reserved_cost + self.budget.max_call_cost_microusd
                > self.budget.max_cost_microusd
            ):
                raise self._refuse("news_program_compile_cost_reservation_exhausted")
            setattr(self, f"{role}_model_calls", used + 1)
            self._reserved_cost += self.budget.max_call_cost_microusd

    def _refuse(self, code: str) -> OptimizationBudgetExceeded:
        refusal = OptimizationBudgetExceeded(code)
        self.first_terminal_error = self.first_terminal_error or refusal
        return refusal

    def _cost(self, response: dspy.LMResponse | None) -> int:
        if response is not None and response.cost is not None:
            cost = max(0, round(float(response.cost) * 1_000_000))
            self.observed_cost_microusd += cost
            return cost
        self.unknown_cost_calls += 1
        if self.imputed_call_cost_microusd is not None:
            self.imputed_cost_calls += 1
            return self.imputed_call_cost_microusd
        raise self._refuse("news_program_compile_provider_cost_unavailable")

    def after(self, role: Literal["task", "reflection", "metric_judge"], response: dspy.LMResponse) -> None:
        with self._lock:
            input_tokens, output_tokens, cached_tokens, total_tokens = _usage_values(response)
            self._record_usage(role, input_tokens, output_tokens, cached_tokens, total_tokens)
            self._settle(role, self._cost(response))

    def after_receipt(self, role: Literal["task", "reflection", "metric_judge"], receipt: LMCallReceipt) -> None:
        with self._lock:
            self._record_usage(
                role,
                receipt.input_tokens,
                receipt.output_tokens,
                receipt.cached_tokens,
                receipt.total_tokens,
            )
            cost = receipt.provider_cost_microusd
            if cost is not None:
                self.observed_cost_microusd += cost
            self._settle(role, self._cost(None) if cost is None else cost)

    def _record_usage(
        self,
        role: Literal["task", "reflection", "metric_judge"],
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        total_tokens: int,
    ) -> None:
        prefix = role
        setattr(self, f"{prefix}_input_tokens", getattr(self, f"{prefix}_input_tokens") + input_tokens)
        setattr(self, f"{prefix}_output_tokens", getattr(self, f"{prefix}_output_tokens") + output_tokens)
        setattr(self, f"{prefix}_cached_tokens", getattr(self, f"{prefix}_cached_tokens") + cached_tokens)
        setattr(self, f"{prefix}_total_tokens", getattr(self, f"{prefix}_total_tokens") + total_tokens)

    def after_provider_failure(
        self, role: Literal["task", "reflection", "metric_judge"], *, provider_reached: bool
    ) -> None:
        with self._lock:
            if provider_reached:
                self._settle(role, self._cost(None))
            else:
                with self._lock:
                    self._reserved_cost -= self.budget.max_call_cost_microusd

    def _settle(self, role: Literal["task", "reflection", "metric_judge"], cost: int) -> None:
        with self._lock:
            self._reserved_cost -= self.budget.max_call_cost_microusd
            self.budget_cost_microusd += cost
            setattr(self, f"{role}_cost_microusd", getattr(self, f"{role}_cost_microusd") + cost)
        if cost > self.budget.max_call_cost_microusd:
            raise self._refuse("news_program_compile_call_cost_reservation_exceeded")
        if self.budget_cost_microusd > self.budget.max_cost_microusd:
            raise self._refuse("news_program_compile_cost_budget_exceeded")

    def remember_terminal(self, error: BaseException) -> None:
        self.first_terminal_error = self.first_terminal_error or error

    def stopper(self, _state: Any) -> bool:
        return self.first_terminal_error is not None

    def raise_if_terminal(self) -> None:
        if self.first_terminal_error is not None:
            raise self.first_terminal_error


def _is_retryable_lm_failure(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            dspy.LMTransportError,
            dspy.LMTimeoutError,
            dspy.LMRateLimitError,
            dspy.LMServerError,
            ConnectionError,
            TimeoutError,
        ),
    )


def _provider_reached(exc: BaseException) -> bool:
    return not isinstance(exc, (dspy.LMTransportError, ConnectionError))


def _remembered_termination(meter: _BudgetMeter) -> str | None:
    error = meter.first_terminal_error
    if error is None:
        return None
    if isinstance(error, OptimizationRunTerminated):
        return str(error)
    raise error


class _MeteredLearningLM(dspy.BaseLM):  # type: ignore[misc]
    """Physical-call budget and learning-only retry around one audited DSPy LM."""

    forward_contract = "typed_lm"

    def __init__(
        self, lm: dspy.BaseLM, *, meter: _BudgetMeter, role: Literal["task", "reflection", "metric_judge"]
    ) -> None:
        super().__init__(
            model=lm.model,
            model_type=getattr(lm, "model_type", "chat"),
            cache=False,
            num_retries=0,
            **dict(getattr(lm, "kwargs", {}) or {}),
        )
        self._lm = lm
        self._meter = meter
        self._role = role
        self.transport_failures = 0
        self.transport_retries = 0
        self.tracefold_compiler_endpoint_identity = getattr(lm, "tracefold_compiler_endpoint_identity", None)

    @property
    def supports_response_schema(self) -> bool:
        return bool(self._lm.supports_response_schema)

    @property
    def supported_params(self) -> set[str]:
        return set(self._lm.supported_params)

    @property
    def supports_function_calling(self) -> bool:
        return bool(self._lm.supports_function_calling)

    @property
    def supports_reasoning(self) -> bool:
        return bool(self._lm.supports_reasoning)

    @property
    def ledger(self) -> LMCallLedger | None:
        ledger = getattr(self._lm, "ledger", None)
        return ledger if isinstance(ledger, LMCallLedger) else None

    def stopper(self, state: Any) -> bool:
        return self._meter.stopper(state)

    def raise_if_terminal(self) -> None:
        self._meter.raise_if_terminal()

    def forward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        return self._invoke(lambda: self._lm(request=request))

    async def aforward(self, request: dspy.LMRequest) -> dspy.LMResponse:
        last: BaseException | None = None
        for attempt in range(_NUM_RETRIES + 1):
            self._meter.before(self._role)
            receipt_index = len(self.ledger.receipts) if self.ledger is not None else None
            try:
                response = await self._lm.acall(request=request)
                if not isinstance(response, dspy.LMResponse):
                    raise dspy.LMUnexpectedError("news_program_compile_lm_response_invalid")
            except BaseException as exc:
                receipt_verified = self._settle_error(exc, receipt_index=receipt_index)
                if _is_retryable_lm_failure(exc) and attempt < _NUM_RETRIES:
                    self.transport_retries += 1
                    last = exc
                    continue
                if not isinstance(exc, LMOutputTruncatedError):
                    self.transport_failures += 1
                candidate_failure = self._is_candidate_failure(exc, receipt_verified=receipt_verified)
                terminal = exc if candidate_failure else self._terminal_error(exc)
                if not candidate_failure:
                    self._meter.remember_terminal(terminal)
                if terminal is exc:
                    raise
                raise terminal from exc
            self._meter.after(self._role, response)
            return response
        raise last if last is not None else RuntimeError("news_program_compile_lm_retry_invariant")

    def _invoke(self, invoke: Callable[[], Any]) -> dspy.LMResponse:
        last: BaseException | None = None
        for attempt in range(_NUM_RETRIES + 1):
            self._meter.before(self._role)
            receipt_index = len(self.ledger.receipts) if self.ledger is not None else None
            try:
                response = invoke()
                if not isinstance(response, dspy.LMResponse):
                    raise dspy.LMUnexpectedError("news_program_compile_lm_response_invalid")
            except BaseException as exc:
                receipt_verified = self._settle_error(exc, receipt_index=receipt_index)
                if _is_retryable_lm_failure(exc) and attempt < _NUM_RETRIES:
                    self.transport_retries += 1
                    last = exc
                    continue
                if not isinstance(exc, LMOutputTruncatedError):
                    self.transport_failures += 1
                candidate_failure = self._is_candidate_failure(exc, receipt_verified=receipt_verified)
                terminal = exc if candidate_failure else self._terminal_error(exc)
                if not candidate_failure:
                    self._meter.remember_terminal(terminal)
                if terminal is exc:
                    raise
                raise terminal from exc
            self._meter.after(self._role, response)
            return response
        raise last if last is not None else RuntimeError("news_program_compile_lm_retry_invariant")

    def _settle_error(self, exc: BaseException, *, receipt_index: int | None) -> bool:
        # GEPA is fixed to one worker. The audited LM writes this physical provider-success receipt before
        # raising truncation, so learning can settle the exact answer without changing the production
        # execution envelope or fabricating a second provider call.
        ledger = self.ledger
        if isinstance(exc, LMOutputTruncatedError) and ledger is not None and receipt_index is not None:
            receipts = ledger.receipts
            if len(receipts) == receipt_index + 1:
                receipt = receipts[receipt_index]
                if (
                    receipt.model_binding == self._role
                    and receipt.terminal_disposition == "provider_success"
                    and receipt.error_code == "news_program_lm_output_truncated"
                ):
                    self._meter.after_receipt(self._role, receipt)
                    return True
        self._meter.after_provider_failure(self._role, provider_reached=_provider_reached(exc))
        return False

    def _terminal_error(self, exc: BaseException) -> BaseException:
        if _is_retryable_lm_failure(exc):
            return OptimizationRunTerminated(f"news_program_compile_{self._role}_provider_unavailable")
        if isinstance(exc, LMOutputTruncatedError):
            return OptimizationRunTerminated(f"news_program_compile_{self._role}_model_output_truncated")
        return exc

    def _is_candidate_failure(self, exc: BaseException, *, receipt_verified: bool) -> bool:
        return receipt_verified and self._role == "task" and isinstance(exc, LMOutputTruncatedError)


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
    parent_program: NewsProgramStateV1
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
        parent_program: NewsProgramStateV1 | None = None,
    ) -> FrozenDevelopmentDataset:
        parent = parent_program or load_stable_program_state()
        active = load_stable_program_state()
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
    """Everything the offline job is allowed to hold: two endpoints, a budget and a clock.

    Deliberately not a database session, a repository, a canary handle or an artifact root. The list of
    fields is the list of powers this job has.
    """

    task_lm: dspy.BaseLM
    reflection_lm: dspy.BaseLM
    budget: OptimizationBudget
    # Which Predictor this run optimizes. One target per run: GEPA selects on one Pareto front, and two
    # Predictors moving under one selection score would make "which change earned the improvement"
    # unanswerable from the receipt.
    target: OptimizationTarget = "classification"
    judge: Any = None
    explanation_protocol: Literal["semantic", "proxy"] = "semantic"
    judge_calibration_receipt_sha256: str = ""
    # Injected so a test can drive the entry point without model spend; production uses `dspy.GEPA.compile`.
    compile_fn: Callable[..., dspy.Module] | None = None
    # Official GEPA state/log directory. The CLI supplies a fresh path under the one run directory.
    gepa_log_dir: str | None = None
    # Injected so a terminal report is byte-reproducible under test; production passes the wall clock.
    now_ms: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    monotonic: Callable[[], float] = time.monotonic


def objective_summary(
    plan: GepaObjectivePlan,
    *,
    episode_projection_root_sha256: str = "",
    target: OptimizationTarget = "classification",
) -> dict[str, Any]:
    """What the Objective Plan decided, in the shape a candidate and a report both carry.

    Per-case dispositions are not here: `readiness` publishes those, and a candidate that embedded them
    would grow with the corpus while saying nothing a reader could act on. What survives is the membership
    a later evaluation has to reproduce exactly.
    """

    return {
        "schema": OBJECTIVE_SUMMARY_SCHEMA,
        "plan_schema": plan.schema_version,
        # Which Predictor this population was optimized for. A candidate that does not say so cannot be
        # re-derived, because the same corpus now answers three different questions.
        "target": target,
        "target_predictor": TARGET_PREDICTORS[target],
        # Which projection of the corpus this plan was built from. The frozen dataset pins the case set;
        # the reviews behind those cases can still be edited, so registration re-projects and compares
        # this rather than a count (#202 PR-B).
        "episode_projection_root_sha256": episode_projection_root_sha256,
        "case_n": plan.case_n,
        "cluster_n": plan.cluster_n,
        "optimizer_case_ids": list(plan.optimizer_case_ids),
        "optimizer_cluster_ids": list(plan.optimizer_cluster_ids),
        **optimizer_population_identity(plan),
        "excluded_case_ids": list(plan.excluded_case_ids),
        "exclusion_reasons": dict(plan.exclusion_reasons),
        "target_predictors": list(plan.target_predictors),
        "target_dimensions": list(plan.target_dimensions),
        "stable_exact_n": plan.stable_exact_n,
        "stable_mismatch_n": plan.stable_mismatch_n,
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
    if not plan.optimizer_cluster_ids:
        # Named by target since #651 §9: a corpus that holds no evidence this target can read is a
        # different situation from one that holds none for any target, and an operator acts on the two
        # differently — the second means go and review, the first means ask a different question.
        reasons.append(f"news_program_compile_no_labelled_clusters:{plan.target}")
    if plan.split is None:
        reasons.append(plan.split_error or "news_program_compile_objective_split_unavailable")
    reasons.extend(plan.blocking_reasons)
    return tuple(reasons)


def optimize(dataset: FrozenDevelopmentDataset, config: OptimizationConfig) -> OptimizationResult:
    """Run the one bounded GEPA optimization over a frozen corpus and return its terminal state."""

    started_at_ms = config.now_ms()
    plan = build_gepa_objective_plan(dataset.episodes, config.target)
    readiness = build_readiness_report(
        plan,
        episodes=dataset.episodes,
        identity={"development_dataset_sha": dataset.ref.development_dataset_sha256},
        coverage=dict(dataset.dataset_payload.get("counts") or {}),
        target=config.target,
    )
    objective = {
        **objective_summary(
            plan,
            episode_projection_root_sha256=dataset.ref.episode_projection_root_sha256,
            target=config.target,
        ),
        "compilable": readiness["objective"]["compilable"],
        # The per-target readiness block that replaced `development_profile` (#651 §9). It carries this
        # target's counts and the other two's beside them, so a `REJECTED` receipt says which kind of
        # evidence the corpus was short of rather than only that it was short of something.
        "targets": readiness["targets"],
        "train": readiness["train"],
        "development_selection": readiness["development_selection"],
        "taxonomy_gold": readiness["taxonomy_gold"],
    }
    blockers = plan_blockers(plan)
    if config.target == "explanation" and config.explanation_protocol == "semantic" and config.judge is None:
        blockers += ("news_program_compile_metric_judge_required",)
    if (
        config.target == "explanation"
        and config.explanation_protocol == "semantic"
        and config.budget.max_metric_judge_model_calls == 0
    ):
        blockers += ("news_program_compile_metric_judge_budget_required",)
    if blockers:
        return _terminal(
            "REJECTED",
            dataset=dataset,
            config=config,
            objective=objective,
            identities={},
            usage=_usage(meter=None, metric_calls=0, budgeted=()),
            reasons=blockers,
            started_at_ms=started_at_ms,
        )

    # Before anything is spent: all configured roles answer under identities they were stamped with, or the run
    # does not start. Reconstructing an identity from the object it describes would attest nothing.
    task_identity = require_model_identity(config.task_lm, role="task")
    reflection_identity = require_model_identity(config.reflection_lm, role="reflection")
    identities = {
        "task": task_identity.model_dump(mode="json"),
        "reflection": reflection_identity.model_dump(mode="json"),
    }
    if config.judge is not None:
        identities["metric_judge"] = require_model_identity(config.judge.lm, role="metric_judge").model_dump(
            mode="json"
        )
    # The wall clock is checked before each call, not during one: a request already in flight runs to its
    # own attested deadline, and clamping that deadline would break the very role contract
    # `ModelExecutionIdentity` exists to attest. So the worst case is `max_wall_clock_seconds` plus one
    # call, and a budget that cannot even bound one call is not a bound — a 60 s budget that still waits
    # 300 s for a reflection response would be a number, not a deadline. Refused here, before anything is
    # spent, against the deadlines these three roles actually carry.
    longest_call_seconds = max(
        float(task_identity.timeout_seconds),
        float(reflection_identity.timeout_seconds),
        float(identities.get("metric_judge", {}).get("timeout_seconds", 0)),
    )
    if config.budget.max_wall_clock_seconds < longest_call_seconds:
        raise ValueError(f"news_learning_optimize_wall_clock_below_call_deadline:{longest_call_seconds:g}")
    meter = _BudgetMeter(
        config.budget,
        imputed_call_cost_microusd=config.budget.max_call_cost_microusd,
        monotonic=config.monotonic,
        max_wall_clock_seconds=config.budget.max_wall_clock_seconds,
    )
    if config.judge is not None:
        config.judge.bind_run_budget(meter)
    task_lm = _MeteredLearningLM(config.task_lm, meter=meter, role="task")
    reflection_lm = _MeteredLearningLM(config.reflection_lm, meter=meter, role="reflection")
    budgeted: tuple[Any, ...] = (task_lm, reflection_lm)
    run: GepaRunResult | None = None
    reasons: tuple[str, ...] = ()
    outcome: Literal["NO_OP", "REJECTED", "ADVANCE"] = "ADVANCE"
    try:
        run = run_gepa(
            base_program=dataset.parent_program,
            episodes=dataset.episodes,
            task_lm=task_lm,
            reflection_lm=reflection_lm,
            target=config.target,
            judge=config.judge,
            explanation_protocol=config.explanation_protocol,
            judge_calibration_receipt_sha256=config.judge_calibration_receipt_sha256,
            auto=config.budget.auto,
            max_metric_calls=config.budget.max_metric_calls,
            seed=config.budget.seed,
            review_rubric_version=dataset.ref.review_rubric_version,
            compile_fn=config.compile_fn,
            gepa_log_dir=config.gepa_log_dir,
        )
    except GepaNoProgramChange as exc:
        # A complete run that kept the seed. The receipts are the run's, not an empty stand-in.
        run, outcome, reasons = exc.result, "NO_OP", (_NO_OP_CODE,)
    except OptimizationRunTerminated as exc:
        outcome, reasons = "REJECTED", (str(exc),)
    except ValueError as exc:
        code = str(exc)
        if code.startswith(_REJECTION_PREFIXES):
            outcome, reasons = "REJECTED", (code,)
        else:
            termination = _remembered_termination(meter)
            if termination is None:
                raise
            outcome, reasons = "REJECTED", (termination,)
    except Exception:
        # DSPy's evaluator may translate the physical-call error into a failure score and later raise its
        # own max-errors exception. The first metered terminal remains the run answer; unrelated defects
        # still propagate because this is a no-op when no terminal was remembered.
        termination = _remembered_termination(meter)
        if termination is None:
            raise
        outcome, reasons = "REJECTED", (termination,)

    metric_calls = run.metric_calls if run is not None else None
    usage = _usage(meter=meter, metric_calls=metric_calls, budgeted=budgeted)
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
            "gepa_public_result": run.public_result,
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

    if not run.state.changed_predictors(dataset.parent_program):
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
        state=run.state.model_dump(mode="json"),
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
        gepa_public_result=produced.get("gepa_public_result"),
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
    metric_calls: int | None,
    budgeted: Sequence[Any],
) -> dict[str, Any]:
    return {
        "schema": USAGE_SCHEMA,
        "task_model_calls": meter.task_model_calls if meter else 0,
        "reflection_model_calls": meter.reflection_model_calls if meter else 0,
        "task_cost_microusd": meter.task_cost_microusd if meter else 0,
        "reflection_cost_microusd": meter.reflection_cost_microusd if meter else 0,
        "task_input_tokens": meter.task_input_tokens if meter else 0,
        "task_output_tokens": meter.task_output_tokens if meter else 0,
        "task_cached_tokens": meter.task_cached_tokens if meter else 0,
        "task_total_tokens": meter.task_total_tokens if meter else 0,
        "reflection_input_tokens": meter.reflection_input_tokens if meter else 0,
        "reflection_output_tokens": meter.reflection_output_tokens if meter else 0,
        "reflection_cached_tokens": meter.reflection_cached_tokens if meter else 0,
        "reflection_total_tokens": meter.reflection_total_tokens if meter else 0,
        "total_tokens": (
            meter.task_total_tokens + meter.reflection_total_tokens + meter.metric_judge_total_tokens if meter else 0
        ),
        **{
            f"metric_judge_{suffix}": getattr(meter, f"metric_judge_{suffix}") if meter else 0
            for suffix in (
                "model_calls",
                "cost_microusd",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "total_tokens",
            )
        },
        "wall_clock_ms": round(meter.elapsed_seconds * 1_000) if meter else 0,
        "imputed_cost_calls": meter.imputed_cost_calls if meter else 0,
        "budget_cost_microusd": meter.budget_cost_microusd if meter else 0,
        "observed_cost_microusd": meter.observed_cost_microusd if meter else 0,
        "unknown_cost_calls": meter.unknown_cost_calls if meter else 0,
        "actual_cost_microusd": (None if meter.unknown_cost_calls else meter.observed_cost_microusd) if meter else 0,
        "metric_calls": metric_calls,
        "transport_failures": sum(lm.transport_failures for lm in budgeted),
        "transport_retries": sum(lm.transport_retries for lm in budgeted),
    }


def _overspend(usage: Mapping[str, Any], *, budget: OptimizationBudget, elapsed_seconds: float) -> tuple[str, ...]:
    """The bounds the meter cannot stop mid-call: total spend and the clock."""

    reasons: list[str] = []
    if int(usage["budget_cost_microusd"]) > budget.max_cost_microusd:
        reasons.append("news_program_compile_cost_budget_exceeded")
    if elapsed_seconds > budget.max_wall_clock_seconds:
        reasons.append("news_learning_optimize_wall_clock_exhausted")
    return tuple(reasons)


__all__ = [
    "OBJECTIVE_SUMMARY_SCHEMA",
    "OPTIMIZATION_TARGETS",
    "REFLECTION_MAX_TOKENS",
    "REFLECTION_TIMEOUT_SECONDS",
    "TARGET_PREDICTORS",
    "USAGE_SCHEMA",
    "FrozenDevelopmentDataset",
    "GepaNoProgramChange",
    "GepaRunResult",
    "ModelExecutionIdentity",
    "OptimizationBudgetExceeded",
    "OptimizationConfig",
    "OptimizationRunTerminated",
    "OptimizationTarget",
    "OptimizerRole",
    "TargetMetric",
    "build_reflection_lm",
    "build_task_lm",
    "gepa_metric_call_ceiling",
    "objective_summary",
    "optimize",
    "optimizer_config_receipt",
    "optimizer_constructor",
    "plan_blockers",
    "require_model_identity",
    "resolve_auto_metric_calls",
    "run_gepa",
    "target_plan",
]
