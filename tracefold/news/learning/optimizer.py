"""The one bounded offline optimizer that can produce a News Prompt candidate.

Public ``dspy.GEPA`` compiles the single ``NativeNewsProgram.event_semantics`` Predict against accepted
taxonomy Gold. Task and reflection calls share one audited ledger and one physical-call meter; the returned
winner is accepted only when it contains that one Predictor and no demos. ReaderCard stays byte-identical.
The module owns no persistence,
activation, canary, or promotion authority. A run ends in ``NO_OP``, ``REJECTED``, or ``ADVANCE``, and an
``ADVANCE`` candidate is still subject to every downstream release gate.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.metadata
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

import dspy  # type: ignore[import-untyped]
from dspy.teleprompt.gepa.gepa import DspyGEPAResult  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_sha
from ..program.artifact import (
    ProgramStrategyArtifactV1,
    ProgramStrategyPatchV1,
    load_stable_program_artifact,
    render_model_evidence_json,
    validate_program_instruction,
)
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
from ..program.runtime import PROGRAM_VERSION, _estimated_tokens
from ..program.signatures import EventSemantics
from .contracts import (
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
from .metric import _json_safe
from .objective import (
    DevelopmentEpisode,
    GepaObjectivePlan,
    build_gepa_objective_plan,
    build_readiness_report,
    optimizer_population_identity,
    retrieval_receipt,
)
from .taxonomy_metric import TAXONOMY_AXES, compare_taxonomy

OBJECTIVE_SUMMARY_SCHEMA = "tracefold.news.optimization_objective_summary.v3"
USAGE_SCHEMA = "tracefold.news.optimization_usage.v2"

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
    """The typed patch and compact evidence produced by one public DSPy compile."""

    patch: ProgramStrategyPatchV1
    metric: dict[str, Any]
    optimizer_config: dict[str, Any]
    public_result: dict[str, Any]
    # The scalar cannot answer whether the winner was selected on examples it never trained on, so the
    # disjoint split and retrieval diagnostics remain public corpus evidence beside native GEPA state.
    split: dict[str, Any]
    retrieval: dict[str, Any]
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    metric_calls: int = Field(ge=0)
    train_count: int = Field(gt=0)
    val_count: int = Field(gt=0)


# The offline release gate refuses a candidate whose mean tokens per program observation grow more than
# 10% over stable. #199's first ADVANCE died exactly there: +2.60 selection points, +4.7KB of instruction,
# rejected after a four-hour run — nothing in GEPA's world had said bytes cost anything. Measured on live
# evaluation runs, one observation averages ~9.0k tokens, so the gate's 10% window is ~900 tokens of
# headroom. The budget therefore charges one envelope over the only writable EventSemantics instruction
# and is enforced twice:
# in the proposer, where a re-ask can still teach the reflection model to compress, and in the native
# Program's candidate guard, which merge proposals reach without ever meeting the proposer (#334).
INSTRUCTION_GROWTH_BUDGET_TOKENS: Final[int] = 800


@dataclass(frozen=True)
class InstructionGrowthBudget:
    """One shared token envelope over the whole candidate, anchored to the seed instructions.

    Anchored to the seed, never the current candidate: an anchor that moved with accepted rounds would let
    the allowance ratchet upward across a run. Components absent from a candidate are counted at their
    seed size, so a partial mapping cannot dodge the envelope.
    """

    seed_tokens: Mapping[str, int]
    max_growth_tokens: int = INSTRUCTION_GROWTH_BUDGET_TOKENS

    @classmethod
    def from_seeds(
        cls, seeds: Mapping[str, str], *, max_growth_tokens: int = INSTRUCTION_GROWTH_BUDGET_TOKENS
    ) -> InstructionGrowthBudget:
        return cls(
            seed_tokens={component: _estimated_tokens(text) for component, text in seeds.items()},
            max_growth_tokens=int(max_growth_tokens),
        )

    def total_budget(self) -> int:
        return sum(self.seed_tokens.values()) + self.max_growth_tokens

    def receipt(self) -> dict[str, Any]:
        return {
            "seed_tokens": dict(self.seed_tokens),
            "max_growth_tokens": self.max_growth_tokens,
            "total_budget": self.total_budget(),
            "estimator": "utf8_bytes_over_4",
        }

    def over(self, texts: Mapping[str, str]) -> tuple[str, str] | None:
        """(code, guidance) when the candidate exceeds the envelope, else None."""

        if not self.seed_tokens:
            return None
        total = 0
        for component, seed in self.seed_tokens.items():
            text = str(texts.get(component) or "")
            total += _estimated_tokens(text) if text.strip() else seed
        budget = self.total_budget()
        if total <= budget:
            return None
        return "news_program_instruction_growth_budget", (
            f"news_program_instruction_growth_budget: the candidate totals {total} estimated tokens "
            f"against a budget of {budget} (seeds {dict(self.seed_tokens)} + shared headroom "
            f"{self.max_growth_tokens}).\n"
            "The offline release gate refuses a candidate whose per-observation tokens grow more than "
            "10%, however well it scores, and both instructions ride every observation. Compress rather "
            "than cut substance: merge overlapping rules and drop restatements of what the instruction "
            "already says."
        )


def _instruction_rejection(text: str) -> str | None:
    """The exact code the instruction bounds would refuse this text with, or `None` if it is acceptable."""

    if not text:
        return None
    try:
        validate_program_instruction(text)
    except ValueError as exc:
        message = str(exc)
        for marker in _INSTRUCTION_REJECTIONS:
            if marker in message:
                return marker
        return "news_program_instruction_rejected"
    return None


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


class _DspyTaxonomyMetric:
    """The four-axis accepted-Gold ruler used by the single EventSemantics Predict."""

    def __call__(
        self,
        gold: dspy.Example,
        pred: dspy.Prediction,
        trace: Any = None,
        pred_name: str | None = None,
        pred_trace: Any = None,
    ) -> dspy.Prediction:
        del trace, pred_name, pred_trace
        expected = getattr(gold, "gold_taxonomy", None)
        if expected is None:
            raise TypeError("news_program_compile_taxonomy_gold_missing")
        try:
            semantics = EventSemantics.model_validate(getattr(pred, "semantics", None))
            comparison = compare_taxonomy(expected, semantics.taxonomy)
        except ValueError as exc:
            zeros = {
                "subject_codes_set_f1": 0.0,
                "event_family_accuracy": 0.0,
                "change_state_accuracy": 0.0,
                "assertion_status_accuracy": 0.0,
                "four_axis_exact_accuracy": 0.0,
            }
            return dspy.Prediction(
                score=0.0,
                feedback=f"Typed EventSemantics is invalid: {exc}",
                objective_scores=zeros,
            )
        objectives = {
            "subject_codes_set_f1": comparison.subject_f1,
            "event_family_accuracy": float(comparison.event_family_match),
            "change_state_accuracy": float(comparison.change_state_match),
            "assertion_status_accuracy": float(comparison.assertion_status_match),
            "four_axis_exact_accuracy": float(comparison.exact),
        }
        return dspy.Prediction(
            score=comparison.score,
            feedback=comparison.feedback,
            objective_scores=objectives,
        )


def _dspy_taxonomy_example(episode: DevelopmentEpisode) -> dspy.Example:
    gold = dict(episode.accepted_review or {}).get("taxonomy")
    if gold is None:
        raise ValueError("news_program_compile_taxonomy_gold_missing")
    return dspy.Example(
        evidence_json=render_model_evidence_json(
            episode.context.event_semantics_payload(), predictor="event_semantics"
        ),
        gold_taxonomy=gold,
        case_id=episode.case_id,
        cluster_id=episode.cluster_id,
    ).with_inputs("evidence_json")


def _taxonomy_metric_receipt(*, review_rubric_version: str) -> dict[str, Any]:
    return {
        "schema": "tracefold.news.taxonomy_gepa_metric.v1",
        "metric_id": "tracefold.news.taxonomy_gepa_direct_v1",
        "review_rubric_version": review_rubric_version,
        "scalar": "mean(subject_codes_set_f1,event_family_exact,change_state_exact,assertion_status_exact)",
        "axes": list(TAXONOMY_AXES),
        "invalid_prediction_score": 0.0,
    }


def _instruction_change_receipt(
    base_program: ProgramStrategyArtifactV1,
    *,
    winner_instruction: str,
) -> dict[str, Any]:
    before = base_program.event_semantics_instruction
    reader = base_program.reader_card_instruction
    return {
        "schema": "tracefold.news.taxonomy_instruction_change.v1",
        "event_semantics": {
            "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(winner_instruction.encode()).hexdigest(),
            "changed": winner_instruction != before,
            "before_bytes": len(before.encode()),
            "after_bytes": len(winner_instruction.encode()),
            "byte_growth": len(winner_instruction.encode()) - len(before.encode()),
            "before_estimated_tokens": _estimated_tokens(before),
            "after_estimated_tokens": _estimated_tokens(winner_instruction),
            "estimated_token_growth": _estimated_tokens(winner_instruction) - _estimated_tokens(before),
            "unified_diff": "\n".join(
                difflib.unified_diff(
                    before.splitlines(),
                    winner_instruction.splitlines(),
                    fromfile="stable/event_semantics",
                    tofile="winner/event_semantics",
                    lineterm="",
                )
            ),
        },
        "reader_card": {
            "instruction_sha256": hashlib.sha256(reader.encode()).hexdigest(),
            "unchanged": True,
        },
    }


def run_gepa(
    *,
    base_program: ProgramStrategyArtifactV1,
    episodes: Sequence[DevelopmentEpisode],
    task_lm: dspy.BaseLM,
    reflection_lm: Any,
    max_metric_calls: int,
    seed: int,
    review_rubric_version: str,
    compile_fn: Callable[..., dspy.Module] | None = None,
    gepa_log_dir: str | None = None,
) -> GepaRunResult:
    """Optimize only EventSemantics taxonomy against accepted Gold."""

    if gepa_log_dir:
        log_path = Path(gepa_log_dir)
        if log_path.exists() and (not log_path.is_dir() or any(log_path.iterdir())):
            raise ValueError("news_program_compile_gepa_log_dir_not_empty")
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
    train_examples = [_dspy_taxonomy_example(episode) for episode in plan.train_episodes]
    val_examples = [_dspy_taxonomy_example(episode) for episode in plan.development_selection_episodes]
    retrieval = retrieval_receipt(episodes)

    metric = _DspyTaxonomyMetric()
    metric_receipt = _taxonomy_metric_receipt(review_rubric_version=review_rubric_version)
    growth_budget = InstructionGrowthBudget.from_seeds({"event_semantics": base_program.event_semantics_instruction})
    student = NativeNewsProgram(base_program).event_semantics
    constructor = optimizer_constructor(
        max_metric_calls=max_metric_calls,
        seed=seed,
        train_count=len(train_examples),
    )
    config_receipt = optimizer_config_receipt(
        growth_budget=growth_budget,
        constructor=constructor,
        task_lm=task_lm,
        reflection_lm=reflection_lm,
        metric_sha256=canonical_sha(metric_receipt),
        example_count=len(train_examples) + len(val_examples),
        train_count=len(train_examples),
        val_count=len(val_examples),
    )
    stopper = getattr(task_lm, "stopper", None)
    if not callable(stopper):

        def stopper(_state: Any) -> bool:
            return False

    optimizer = dspy.GEPA(
        metric=metric,
        reflection_lm=reflection_lm,
        instruction_proposer=None,
        add_format_failure_as_feedback=True,
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
    with task_ledger.scope(scope_context), dspy.context(lm=task_lm, adapter=program_json_adapter()):
        optimized = (compile_fn or optimizer.compile)(student, trainset=train_examples, valset=val_examples)

    # DSPy's evaluator translates every per-example Exception to `failure_score`. A physical budget refusal
    # or systemic provider failure is a run answer, not a bad candidate, so reconcile it before looking at
    # the returned winner. The stopper merely avoids starting another GEPA step; this check is authoritative.
    raise_terminal = getattr(task_lm, "raise_if_terminal", None)
    if callable(raise_terminal):
        raise_terminal()

    run = getattr(optimized, "detailed_results", None)
    if not isinstance(run, DspyGEPAResult):
        raise ValueError("news_program_compile_detailed_results_missing")

    reported_calls = getattr(run, "total_metric_calls", None)
    metric_calls = int(reported_calls) if isinstance(reported_calls, int) else -1
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
    scores = [float(value) for value in list(getattr(run, "val_aggregate_scores", ()) or ())]
    if any(not math.isfinite(score) for score in scores):
        raise TypeError("news_program_compile_nonfinite_score")
    best_idx = run.best_idx
    if not scores or best_idx < 0 or best_idx >= len(scores):
        raise ValueError("news_program_compile_selection_scores_invalid")
    if len(run.candidates) != len(scores) or len(run.parents) != len(scores):
        raise ValueError("news_program_compile_public_result_invalid")
    objective_scores = run.val_aggregate_subscores
    if objective_scores is None or len(objective_scores) != len(scores):
        raise ValueError("news_program_compile_public_objective_scores_missing")
    val_subscores = run.val_subscores
    if len(val_subscores) != len(scores):
        raise ValueError("news_program_compile_public_validation_subscores_missing")
    expected_val_ids = set(range(len(val_examples)))
    if any(set(candidate_scores) != expected_val_ids for candidate_scores in val_subscores):
        raise ValueError("news_program_compile_public_validation_subscores_invalid")
    control_indexes = {
        index
        for index, episode in enumerate(plan.development_selection_episodes)
        if episode.case_id in set(plan.control_case_ids)
    }
    controls_regressed = any(
        float(val_subscores[best_idx][index]) < float(val_subscores[0][index]) for index in control_indexes
    )
    strictly_improved = best_idx != 0 and scores[best_idx] > scores[0] and not controls_regressed
    winner_instruction = _winning_event_instruction(run.candidates[best_idx])
    validate_program_instruction(winner_instruction)
    rejected = growth_budget.over({"event_semantics": winner_instruction})
    if rejected is not None:
        raise ValueError(rejected[0])
    patch = ProgramStrategyPatchV1.issue(
        parent=base_program,
        event_semantics_instruction=winner_instruction,
        reader_card_instruction=base_program.reader_card_instruction,
    )
    baseline_objectives = {key: float(value) for key, value in objective_scores[0].items()}
    winner_objectives = {key: float(value) for key, value in objective_scores[best_idx].items()}
    selection = {
        "schema": "tracefold.news.taxonomy_selection_score.v1",
        "candidate_0": {"taxonomy_overall": scores[0], **baseline_objectives},
        "winner": {"taxonomy_overall": scores[best_idx], **winner_objectives},
        "delta": {
            "taxonomy_overall": round(scores[best_idx] - scores[0], 6),
            **{
                key: round(winner_objectives.get(key, 0.0) - baseline_objectives.get(key, 0.0), 6)
                for key in sorted(set(baseline_objectives) | set(winner_objectives))
            },
        },
        "stable_correct_control_n": len(control_indexes),
        "stable_correct_control_regression_n": sum(
            float(val_subscores[best_idx][index]) < float(val_subscores[0][index]) for index in control_indexes
        ),
    }
    public_result = {
        "schema": "tracefold.news.dspy_gepa_public_result.v1",
        "candidate_count": len(run.candidates),
        "parents": run.parents,
        "validation_aggregate_scores": scores,
        "validation_subscores": [
            {str(key): float(value) for key, value in candidate_scores.items()} for candidate_scores in val_subscores
        ],
        "validation_aggregate_objective_scores": objective_scores,
        "best_index": best_idx,
        "total_metric_calls": metric_calls,
    }
    result = GepaRunResult(
        patch=patch,
        metric={
            **metric_receipt,
            "taxonomy_selection_score": selection,
            "instruction_change": _instruction_change_receipt(
                base_program,
                winner_instruction=winner_instruction,
            ),
        },
        optimizer_config=config_receipt,
        public_result=public_result,
        split=split_receipt,
        retrieval=retrieval,
        failure_cluster_ids=plan.target_failure_cluster_ids,
        target_dimensions=plan.target_dimensions,
        metric_calls=metric_calls,
        train_count=len(train_examples),
        val_count=len(val_examples),
    )
    if not strictly_improved or patch.event_semantics_instruction == base_program.event_semantics_instruction:
        raise GepaNoProgramChange(result)
    return result


def _winning_event_instruction(program: dspy.Module) -> str:
    """Read the one public Predict returned at ``candidates[best_idx]``."""

    predictors = dict(program.named_predictors())
    if len(predictors) != 1:
        raise ValueError("news_program_compile_result_type_invalid")
    predictor = next(iter(predictors.values()))
    if list(getattr(predictor, "demos", ()) or ()):
        raise ValueError("news_program_compile_result_write_set_invalid")
    return str(predictor.signature.instructions)


def optimizer_constructor(*, max_metric_calls: int, seed: int, train_count: int) -> dict[str, Any]:
    """The one public `dspy.GEPA` configuration this repository constructs."""

    return {
        "max_metric_calls": max_metric_calls,
        # gepa's default is 3, and 3 is too few for this metric. In the first real run every proposal was
        # skipped on an *exact* tie — 1.729166 vs 1.729166, 1.597917 vs 1.597917 — because a good instruction
        # here names recurring evidence patterns (a sentiment index, a comparison base, a crypto-linked
        # equity) that a 3-example sample almost never contains. The metric is also coarse, moving in steps
        # like 0 / 0.675 / 0.825 / 1.0, so ties are easy to hit and GEPA skips on a tie by rule. A wider
        # minibatch is what gives a real improvement room to show up as one.
        "reflection_minibatch_size": min(10, train_count),
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
    task_lm: dspy.BaseLM,
    reflection_lm: Any,
    growth_budget: InstructionGrowthBudget | None = None,
    metric_sha256: str,
    example_count: int,
    train_count: int,
    val_count: int,
) -> dict[str, Any]:
    return {
        "schema": "tracefold.news.compile_optimizer_config_receipt.v5",
        "optimizer": {
            "implementation": "dspy.GEPA",
            "dspy_version": importlib.metadata.version("dspy"),
            "gepa_version": importlib.metadata.version("gepa"),
            "adapter": "tracefold.news.program.lm.program_json_adapter",
            "evaluator": "NativeNewsProgram.event_semantics on one explicit task LM",
            "add_format_failure_as_feedback": True,
            "terminal_stopper": "shared_physical_lm_meter",
            "upstream_fixed_arguments": {"display_progress_bar": True, "raise_on_exception": True},
        },
        "metric_sha256": metric_sha256,
        "constructor_scalar_arguments": _json_scalars(constructor),
        "instruction_proposer": None,
        # v3 (#334): a selection rule that can decide who wins belongs in the compile record. `null` means
        # the run was not budgeted, which is itself evidence.
        "instruction_growth_budget": growth_budget.receipt() if growth_budget is not None else None,
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
    role: Literal["task", "reflection"],
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
    "news_program_compile_no_verified_failure_clusters",
    "news_program_compile_no_correct_control_clusters",
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
        self.actual_cost_microusd = 0
        self.imputed_cost_calls = 0
        # DSPy's evaluator converts every Module Exception to `failure_score`. Keep the first run-level
        # failure out of band so a returned Program cannot launder exhaustion into a candidate score.
        self.first_terminal_error: BaseException | None = None
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
        self.raise_if_terminal()
        # The wall clock is checked here, before the call, for the same reason the cost reservation is: the
        # only bound worth having is one that stops the next request rather than reporting the last one.
        if self._max_wall_clock_seconds is not None and self.elapsed_seconds >= self._max_wall_clock_seconds:
            raise self._refuse("news_learning_optimize_wall_clock_exhausted")
        used = self.task_model_calls if role == "task" else self.reflection_model_calls
        limit = self.budget.max_task_model_calls if role == "task" else self.budget.max_reflection_model_calls
        if used >= limit:
            raise self._refuse(f"news_program_compile_{role}_model_call_budget_exhausted")
        if self.actual_cost_microusd + self.budget.max_call_cost_microusd > self.budget.max_cost_microusd:
            raise self._refuse("news_program_compile_cost_reservation_exhausted")
        if role == "task":
            self.task_model_calls += 1
        else:
            self.reflection_model_calls += 1

    def _refuse(self, code: str) -> OptimizationBudgetExceeded:
        refusal = OptimizationBudgetExceeded(code)
        self.first_terminal_error = self.first_terminal_error or refusal
        return refusal

    def _cost(self, response: dspy.LMResponse | None) -> int:
        if response is not None and response.cost is not None:
            return max(0, round(float(response.cost) * 1_000_000))
        if self.imputed_call_cost_microusd is not None:
            self.imputed_cost_calls += 1
            return self.imputed_call_cost_microusd
        raise self._refuse("news_program_compile_provider_cost_unavailable")

    def after(self, role: Literal["task", "reflection"], response: dspy.LMResponse) -> None:
        input_tokens, output_tokens, cached_tokens, total_tokens = _usage_values(response)
        self._record_usage(role, input_tokens, output_tokens, cached_tokens, total_tokens)
        self._settle(role, self._cost(response))

    def after_receipt(self, role: Literal["task", "reflection"], receipt: LMCallReceipt) -> None:
        self._record_usage(
            role,
            receipt.input_tokens,
            receipt.output_tokens,
            receipt.cached_tokens,
            receipt.total_tokens,
        )
        cost = receipt.provider_cost_microusd
        self._settle(role, self._cost(None) if cost is None else cost)

    def _record_usage(
        self,
        role: Literal["task", "reflection"],
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        total_tokens: int,
    ) -> None:
        prefix = "task" if role == "task" else "reflection"
        setattr(self, f"{prefix}_input_tokens", getattr(self, f"{prefix}_input_tokens") + input_tokens)
        setattr(self, f"{prefix}_output_tokens", getattr(self, f"{prefix}_output_tokens") + output_tokens)
        setattr(self, f"{prefix}_cached_tokens", getattr(self, f"{prefix}_cached_tokens") + cached_tokens)
        setattr(self, f"{prefix}_total_tokens", getattr(self, f"{prefix}_total_tokens") + total_tokens)

    def after_provider_failure(self, role: Literal["task", "reflection"], *, provider_reached: bool) -> None:
        if provider_reached:
            self._settle(role, self._cost(None))

    def _settle(self, role: Literal["task", "reflection"], cost: int) -> None:
        self.actual_cost_microusd += cost
        if role == "task":
            self.task_cost_microusd += cost
        else:
            self.reflection_cost_microusd += cost
        if cost > self.budget.max_call_cost_microusd:
            raise self._refuse("news_program_compile_call_cost_reservation_exceeded")
        if self.actual_cost_microusd > self.budget.max_cost_microusd:
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

    def __init__(self, lm: dspy.BaseLM, *, meter: _BudgetMeter, role: Literal["task", "reflection"]) -> None:
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
                self._settle_error(exc, receipt_index=receipt_index)
                if _is_retryable_lm_failure(exc) and attempt < _NUM_RETRIES:
                    self.transport_retries += 1
                    last = exc
                    continue
                if not isinstance(exc, LMOutputTruncatedError):
                    self.transport_failures += 1
                terminal = self._terminal_error(exc)
                self._meter.remember_terminal(terminal)
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
                self._settle_error(exc, receipt_index=receipt_index)
                if _is_retryable_lm_failure(exc) and attempt < _NUM_RETRIES:
                    self.transport_retries += 1
                    last = exc
                    continue
                if not isinstance(exc, LMOutputTruncatedError):
                    self.transport_failures += 1
                terminal = self._terminal_error(exc)
                self._meter.remember_terminal(terminal)
                raise terminal from exc
            self._meter.after(self._role, response)
            return response
        raise last if last is not None else RuntimeError("news_program_compile_lm_retry_invariant")

    def _settle_error(self, exc: BaseException, *, receipt_index: int | None) -> None:
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
                    return
        self._meter.after_provider_failure(self._role, provider_reached=_provider_reached(exc))

    def _terminal_error(self, exc: BaseException) -> BaseException:
        if _is_retryable_lm_failure(exc):
            return OptimizationRunTerminated(f"news_program_compile_{self._role}_provider_unavailable")
        if isinstance(exc, LMOutputTruncatedError):
            return OptimizationRunTerminated(f"news_program_compile_{self._role}_model_output_truncated")
        return exc


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
    """Everything the offline job is allowed to hold: two endpoints, a budget and a clock.

    Deliberately not a database session, a repository, a canary handle or an artifact root. The list of
    fields is the list of powers this job has.
    """

    task_lm: dspy.BaseLM
    reflection_lm: dspy.BaseLM
    budget: OptimizationBudget
    # Injected so a test can drive the entry point without model spend; production uses `dspy.GEPA.compile`.
    compile_fn: Callable[..., dspy.Module] | None = None
    # Official GEPA state/log directory. The CLI supplies a fresh path under the one run directory.
    gepa_log_dir: str | None = None
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


def optimize(dataset: FrozenDevelopmentDataset, config: OptimizationConfig) -> OptimizationResult:
    """Run the one bounded GEPA optimization over a frozen corpus and return its terminal state."""

    started_at_ms = config.now_ms()
    plan = build_gepa_objective_plan(dataset.episodes)
    readiness = build_readiness_report(
        plan,
        episodes=dataset.episodes,
        identity={"development_dataset_sha": dataset.ref.development_dataset_sha256},
        coverage=dict(dataset.dataset_payload.get("counts") or {}),
    )
    objective = {
        **objective_summary(plan, episode_projection_root_sha256=dataset.ref.episode_projection_root_sha256),
        "compilable": readiness["objective"]["compilable"],
        "development_profile": readiness["development_profile"],
        "train": readiness["train"],
        "development_selection": readiness["development_selection"],
        "taxonomy_gold": readiness["taxonomy_gold"],
    }
    blockers = (*plan_blockers(plan), *tuple(readiness["development_profile"]["blockers"]))
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

    # Before anything is spent: both roles answer under identities they were stamped with, or the run
    # does not start. Reconstructing an identity from the object it describes would attest nothing.
    task_identity = require_model_identity(config.task_lm, role="task")
    reflection_identity = require_model_identity(config.reflection_lm, role="reflection")
    identities = {
        "task": task_identity.model_dump(mode="json"),
        "reflection": reflection_identity.model_dump(mode="json"),
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
    )
    if config.budget.max_wall_clock_seconds < longest_call_seconds:
        raise ValueError(f"news_learning_optimize_wall_clock_below_call_deadline:{longest_call_seconds:g}")
    meter = _BudgetMeter(
        config.budget,
        imputed_call_cost_microusd=config.budget.max_call_cost_microusd,
        monotonic=config.monotonic,
        max_wall_clock_seconds=config.budget.max_wall_clock_seconds,
    )
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

    metric_calls = run.metric_calls if run is not None else 0
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
    metric_calls: int,
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
        "total_tokens": (meter.task_total_tokens + meter.reflection_total_tokens if meter else 0),
        "wall_clock_ms": round(meter.elapsed_seconds * 1_000) if meter else 0,
        "imputed_cost_calls": meter.imputed_cost_calls if meter else 0,
        "actual_cost_microusd": meter.actual_cost_microusd if meter else 0,
        "metric_calls": metric_calls,
        "transport_failures": sum(lm.transport_failures for lm in budgeted),
        "transport_retries": sum(lm.transport_retries for lm in budgeted),
    }


def _overspend(usage: Mapping[str, Any], *, budget: OptimizationBudget, elapsed_seconds: float) -> tuple[str, ...]:
    """The bounds the meter cannot stop mid-call: total spend and the clock."""

    reasons: list[str] = []
    if int(usage["actual_cost_microusd"]) > budget.max_cost_microusd:
        reasons.append("news_program_compile_cost_budget_exceeded")
    if elapsed_seconds > budget.max_wall_clock_seconds:
        reasons.append("news_learning_optimize_wall_clock_exhausted")
    return tuple(reasons)


__all__ = [
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
    "OptimizationRunTerminated",
    "OptimizerRole",
    "build_reflection_lm",
    "build_task_lm",
    "gepa_metric_call_ceiling",
    "objective_summary",
    "optimize",
    "optimizer_config_receipt",
    "optimizer_constructor",
    "plan_blockers",
    "require_model_identity",
    "run_gepa",
]
