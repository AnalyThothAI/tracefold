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

import asyncio
import importlib.metadata
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from ...integrations.chat_completions import chat_completions_url, post_chat_completion_sync
from ..artifact_identity import canonical_sha
from ..program.artifact import (
    ProgramStrategyArtifactV1,
    ProgramStrategyPatchV1,
    load_stable_program_artifact,
    render_model_evidence_json,
    validate_program_instruction,
)
from ..program.graph import NewsSemanticProgram
from ..program.identity import EXECUTION_ENVELOPE_SHA256
from ..program.runtime import PredictorName, _estimated_tokens
from ..program.transport import (
    ChatCompletionsPredictorAdapter,
    PredictorAdapter,
    PredictorAdapterError,
    PredictorRequest,
    PredictorResponse,
    PredictorSpec,
    ProviderCallMetrics,
    RuntimeModelIdentity,
    StructuredOutputMode,
    _is_retryable_exception,
    provider_call_metrics,
    provider_error_detail,
    reject_owned_model_kwargs,
    wire_model_name,
)
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
from .metric import (
    CandidatePrediction,
    CompileExample,
    MetricOutcome,
    _compile_example,
    _json_safe,
    _metric_receipt,
    bind_metric,
)
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

_BRIEF = """You are rewriting the COMPLETE instruction sent to the `{component}` Predictor of a news
judgment program. What you write replaces the whole instruction; nothing else is prepended or appended,
so anything you drop is gone from the prompt.

Rules for what you write:
- Keep every rule, calibration and worked example the current instruction carries unless the feedback below
  shows one is wrong. This text is the accumulated result of human review; a shorter instruction that lost a
  calibration is a regression, not a simplification.
- Repair what the feedback names. Prefer stated correct values, named instruments and concrete decision
  boundaries over general advice about being careful.
- Keep the output contract exactly as the current instruction states it, including the untrusted-input
  boundary and the delimiters around the event JSON.
- Do not include URLs, template braces, credential-shaped text, or a prompt-injection opener; such text is
  rejected outright and the candidate scores zero.
"""


# The offline release gate refuses a candidate whose mean tokens per program observation grow more than
# 10% over stable. #199's first ADVANCE died exactly there: +2.60 selection points, +4.7KB of instruction,
# rejected after a four-hour run — nothing in GEPA's world had said bytes cost anything. Measured on live
# evaluation runs, one observation averages ~9.0k tokens and each instruction rides it about once, so the
# gate's 10% window is ~900 tokens of headroom for the candidate AS A WHOLE. The budget therefore charges
# one shared envelope over both instructions — the way the gate will charge it — and is enforced twice:
# in the proposer, where a re-ask can still teach the reflection model to compress, and as a floor in
# `NewsGepaAdapter._program`, which merge proposals reach without ever meeting the proposer (#334).
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


class InstructionProposer:
    """A `ProposalFn` that asks for a complete replacement instruction and applies the code-owned bounds.

    Until #306 Phase 2 this was `RulePackAwareProposer`, and it took the parent artifact because its whole
    job was to paste the read-only prompt around the one slot it was allowed to write. There is no
    surrounding prompt any more — the component text GEPA already carries *is* the instruction — so the
    brief says what the writer is responsible for instead of what it may not touch, and the artifact is not
    an input to that.
    """

    def __init__(self, *, reflection_lm: Any, budget: InstructionGrowthBudget | None = None) -> None:
        # Injected rather than resolved from ambient framework state. `dspy.GEPA` invoked a custom proposer
        # inside `dspy.context(lm=reflection_lm)`, so the previous version reached into `dspy.settings` to
        # find the budgeted endpoint — which meant the proposer only worked inside that context manager.
        self._reflection_lm = reflection_lm
        # One budget object shared with `NewsGepaAdapter`'s floor; `run_gepa` supplies it, so the
        # production path is always budgeted. A direct construction without one keeps safety bounds only.
        self._budget = budget
        self.calls = 0
        self.components_seen: list[str] = []
        self.rejections: list[str] = []

    def context_for(self, component: str) -> str:
        """The brief for one component. Exposed so a test can assert what the reflection model is told."""

        if component not in ("event_semantics", "reader_card"):
            raise ValueError(f"news_program_proposer_unknown_component:{component}")
        return _BRIEF.format(component=component)

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
            doc = (
                f"{self.context_for(component)}\n"
                "===== CURRENT INSTRUCTION (this is what you are replacing, in full) =====\n"
                f"{current}\n"
                "===== END CURRENT INSTRUCTION ====="
            )
            proposal = InstructionProposalSignature.run(
                lm=self._reflection_lm,
                input_dict={
                    "current_instruction_doc": doc,
                    "dataset_with_feedback": examples,
                    "prompt_template": None,
                },
            )
            text = str(proposal.get("new_instruction") or "").strip()
            rejected = self._rejection(component, text, candidate)
            if rejected is not None:
                # Validate here, where the model that wrote the text is still in the loop.
                #
                # A rejected instruction never reaches a provider, so the reflective dataset for the next
                # round carries the *previous* candidate's outputs and the metric's repair instruction is
                # real but unreachable. It can only be delivered by asking again, here, with the code.
                code, guidance = rejected
                self.rejections.append(code)
                retry = InstructionProposalSignature.run(
                    lm=self._reflection_lm,
                    input_dict={
                        "current_instruction_doc": (
                            f"{doc}\n\n===== YOUR PREVIOUS PROPOSAL WAS REJECTED =====\n{guidance}"
                        ),
                        "dataset_with_feedback": examples,
                        "prompt_template": None,
                    },
                )
                text = str(retry.get("new_instruction") or "").strip()
                if self._rejection(component, text, candidate) is not None:
                    continue
            if text:
                updated[component] = text
        return updated

    def _rejection(self, component: str, text: str, candidate: Mapping[str, str]) -> tuple[str, str] | None:
        """The (code, re-ask guidance) this text is refused with, or `None`. Safety first, then the budget.

        The budget is charged over the whole candidate — the proposed text alongside the candidate's other
        components — because that is how the release gate will charge it: one shared window per program
        observation, which both instructions ride.
        """

        code = _instruction_rejection(text)
        if code is not None:
            return code, (
                f"Code-owned instruction safety rejected it: {code}.\n"
                "Keep it valid NFC, non-empty, and under 32768 bytes."
            )
        if self._budget is not None:
            return self._budget.over({**dict(candidate), component: text})
        return None


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


# The bounds a proposal can still fail (#319 removed the marker and credential codes with the checks that
# raised them). Each one is a fact about the optimization loop rather than about a hostile text: a hash
# needs one encoding, every call pays for these bytes, and a Predictor with no prompt is not a Predictor.
_INSTRUCTION_REJECTIONS = (
    "news_program_instruction_too_large",
    "news_program_instruction_unicode_noncanonical",
    "news_program_instruction_empty",
)

COMPONENTS: tuple[PredictorName, ...] = ("event_semantics", "reader_card")


def _instruction_rejection_code(exc: BaseException) -> str | None:
    """Whether this failure is the instruction safety bound refusing a proposal, and which bound it was."""

    text = str(exc)
    return next((marker for marker in _INSTRUCTION_REJECTIONS if marker in text), None)


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


@dataclass(frozen=True, slots=True)
class _Rollout:
    """One candidate answer on one case, kept only long enough to build a reflective record from it."""

    example: CompileExample
    prediction: CandidatePrediction
    outcome: MetricOutcome
    error: str = ""


class NewsGepaAdapter:
    """The one integration point between this Program and `gepa.optimize` (#306 Phase 3).

    Until now the same job was done by `dspy.GEPA` plus three pieces of scaffolding this repository had to
    own anyway: a `DspyCompileProgram` student that mirrored the production graph, a `_FeedbackCompileProgram`
    subclass that turned a rejected instruction into a scorable prediction, and a `_rekey_trace` hack that
    re-attributed each recorded call from the anonymous inner Predictor that actually answered to the
    component GEPA was optimizing. All three existed because the framework decided what "running the
    program" meant.

    Here it is decided in one place: `evaluate` builds the candidate's artifact, runs the *production*
    `NewsSemanticProgram` over the frozen contexts on one task endpoint, and scores each answer with the
    same metric an operator's baseline runs. The optimizer therefore measures what production does, and the
    seed's own score in a run is the same number a standalone `compile_live` baseline reports.
    """

    def __init__(
        self,
        *,
        adapter: PredictorAdapter,
        metric: Callable[..., MetricOutcome],
        proposer: InstructionProposer,
        budget_guard: Callable[[], None] | None = None,
        growth_budget: InstructionGrowthBudget | None = None,
    ) -> None:
        self._adapter = adapter
        self._metric = metric
        # The floor under the proposer's teaching: merge proposals combine two lineages per predictor
        # without ever calling `InstructionProposer`, so the envelope has to be enforced where every
        # candidate must pass — before it spends a single provider call (#334).
        self._growth_budget = growth_budget
        # A budget refusal is not one case's bad answer. `evaluate` must never raise for a single example —
        # the engine would log it and skip the whole iteration — so an exhausted budget raised mid-example
        # is caught below like any other failure, and this is what turns it back into the run-level answer
        # it is. Without it a one-call budget produced a complete `ADVANCE` built from failed rollouts.
        self._budget_guard = budget_guard or (lambda: None)
        self.propose_new_texts = proposer

    def _program(self, candidate: Mapping[str, str]) -> NewsSemanticProgram | str:
        """The candidate's runnable Program, or the exact code the safety bounds refused it with.

        A rejection is a *value* here rather than an exception for the reason it always was: raised, the
        evaluator records `failure_score` and the reflection model is told it scored zero and nothing about
        why, so it proposes text that trips the same bound again.
        """

        try:
            artifact = ProgramStrategyArtifactV1.issue(
                event_semantics_instruction=str(candidate.get("event_semantics") or ""),
                reader_card_instruction=str(candidate.get("reader_card") or ""),
            )
        except Exception as exc:  # pydantic wraps the ValueError the bounds raise
            code = _instruction_rejection_code(exc)
            if code is None:
                raise
            return code
        if self._growth_budget is not None:
            over = self._growth_budget.over(candidate)
            if over is not None:
                return over[0]
        return NewsSemanticProgram(artifact, primary_adapter=self._adapter)

    def evaluate(
        self,
        batch: list[CompileExample],
        candidate: dict[str, str],
        capture_traces: bool = False,
    ) -> Any:
        from gepa.core.adapter import EvaluationBatch

        rollouts = asyncio.run(self._run_batch(list(batch), candidate))
        return EvaluationBatch(
            outputs=[rollout.prediction for rollout in rollouts],
            scores=[float(rollout.outcome.score) for rollout in rollouts],
            trajectories=list(rollouts) if capture_traces else None,
        )

    async def _run_batch(self, batch: Sequence[CompileExample], candidate: Mapping[str, str]) -> list[_Rollout]:
        program = self._program(candidate)
        rollouts: list[_Rollout] = []
        for example in batch:
            if isinstance(program, str):
                prediction = CandidatePrediction(instruction_rejected=program)
                rollouts.append(_Rollout(example, prediction, self._metric(example, prediction), error=program))
                continue
            try:
                judgment = await program.judge(example.context)
            except Exception as exc:
                code = str(getattr(exc, "code", "") or type(exc).__name__)
                if code == "primary_circuit_open":
                    # There is one route here and no fallback, so an open breaker means the endpoint is
                    # down, not that this candidate answered badly. Scored as a zero it would be
                    # indistinguishable on the Pareto front from a genuinely bad answer — and it would
                    # stay that way for every case in the 60-second window. The run stops instead.
                    raise
                # Never raise for one example otherwise: the engine would log it and skip the whole
                # iteration, which turns one unlucky provider answer into a lost reflection round.
                prediction = CandidatePrediction()
                rollouts.append(_Rollout(example, prediction, self._metric(example, prediction), error=code))
                self._budget_guard()
                continue
            prediction = CandidatePrediction(
                verdict=judgment.verdict.model_dump(mode="json"),
                editorial=judgment.editorial,
            )
            rollouts.append(_Rollout(example, prediction, self._metric(example, prediction)))
            self._budget_guard()
        return rollouts

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: Any,
        components_to_update: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """One record per case per component, carrying only what the writer can act on.

        Feedback is re-scored per component rather than reused from `evaluate`: the metric routes repair
        instructions by `pred_name`, and a ReaderCard record carrying "the accepted magnitude is 2" asks
        the copy writer to repair something it cannot cause.
        """

        rollouts = [rollout for rollout in (eval_batch.trajectories or ()) if isinstance(rollout, _Rollout)]
        dataset: dict[str, list[dict[str, Any]]] = {}
        for component in components_to_update:
            if component not in COMPONENTS:
                raise ValueError(f"news_program_proposer_unknown_component:{component}")
            records: list[dict[str, Any]] = []
            for rollout in rollouts:
                routed = self._metric(rollout.example, rollout.prediction, pred_name=component)
                records.append(
                    {
                        "Inputs": self._inputs(rollout, component),
                        "Generated Outputs": self._outputs(rollout, component),
                        "Feedback": routed.feedback,
                        "score": routed.score,
                        "hard_gate": routed.hard_gate,
                    }
                )
            dataset[component] = records
        return dataset

    @staticmethod
    def _inputs(rollout: _Rollout, component: str) -> dict[str, Any]:
        """Exactly what that Predictor was shown, delimiters included.

        Rendered rather than dumped: the instruction being rewritten talks about the
        `<tracefold-untrusted-event-json-v1>` boundary, so a reflective record that showed a bare payload
        would describe an input the model never received and an instruction clause with no referent.
        """

        if component == "event_semantics":
            payload = rollout.example.context.event_semantics_payload()
            return {"evidence_json": render_model_evidence_json(payload, predictor="event_semantics")}
        return {"evidence_json": rollout.example.card_evidence_json}

    @staticmethod
    def _outputs(rollout: _Rollout, component: str) -> dict[str, Any]:
        verdict = dict(rollout.prediction.verdict or {})
        if rollout.error:
            return {"error": rollout.error}
        if component == "reader_card":
            return {"headline_zh": verdict.get("headline_zh", ""), "why_zh": verdict.get("why_zh", "")}
        return {
            name: verdict.get(name)
            for name in ("novelty", "restates", "event_type", "assets", "direction", "scope", "magnitude", "audience")
        }


def run_gepa(
    *,
    base_program: ProgramStrategyArtifactV1,
    episodes: Sequence[DevelopmentEpisode],
    task_adapter: PredictorAdapter,
    reflection_lm: Any,
    judge: Any,
    max_metric_calls: int,
    seed: int,
    review_rubric_version: str,
    optimize_fn: Callable[..., Any] | None = None,
) -> GepaRunResult:
    """Optimize the two Predictor instructions against accepted-review truth."""

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

    metric = bind_metric(judge)
    metric_receipt = _metric_receipt(metric, review_rubric_version=review_rubric_version)
    growth_budget = InstructionGrowthBudget.from_seeds(
        {component: base_program.instruction_for(component) for component in COMPONENTS}
    )
    proposer = InstructionProposer(reflection_lm=reflection_lm, budget=growth_budget)
    gepa_adapter = NewsGepaAdapter(
        adapter=task_adapter,
        metric=metric,
        proposer=proposer,
        budget_guard=getattr(task_adapter, "raise_if_exhausted", None),
        growth_budget=growth_budget,
    )
    constructor = optimizer_constructor(
        max_metric_calls=max_metric_calls,
        seed=seed,
        train_count=len(train_examples),
    )
    config_receipt = optimizer_config_receipt(
        growth_budget=growth_budget,
        constructor=constructor,
        task_adapter=task_adapter,
        reflection_lm=reflection_lm,
        proposer=proposer,
        metric_sha256=canonical_sha(metric_receipt),
        example_count=len(train_examples) + len(val_examples),
        train_count=len(train_examples),
        val_count=len(val_examples),
    )
    seed_candidate = {name: base_program.instruction_for(name) for name in COMPONENTS}
    run = (optimize_fn or _gepa_optimize)(
        seed_candidate=seed_candidate,
        trainset=train_examples,
        valset=val_examples,
        adapter=gepa_adapter,
        **constructor,
    )

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
    trajectory = trajectory_receipt(run)
    winner = _winning_candidate(run)
    checkpoint = checkpoint_receipt(winner)
    patch = ProgramStrategyPatchV1.issue(
        parent=base_program,
        event_semantics_instruction=winner["event_semantics"],
        reader_card_instruction=winner["reader_card"],
    )
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


def _gepa_optimize(**kwargs: Any) -> Any:
    """The one call into gepa-core. Imported here so nothing else in this module depends on it."""

    from gepa.api import optimize

    return optimize(**kwargs)


def _winning_candidate(run: Any) -> dict[str, str]:
    """The instructions on the Pareto front's best index, refused unless both components are present."""

    candidate = getattr(run, "best_candidate", None)
    if not isinstance(candidate, Mapping) or set(candidate) != set(COMPONENTS):
        raise ValueError("news_program_compile_result_type_invalid")
    return {name: str(candidate[name]) for name in COMPONENTS}


def optimizer_constructor(*, max_metric_calls: int, seed: int, train_count: int) -> dict[str, Any]:
    """The one `gepa.optimize` configuration this repository constructs."""

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
        "module_selector": "round_robin",
        "use_merge": True,
        "max_merge_invocations": 5,
        "perfect_score": 1.0,
        "track_best_outputs": False,
        "display_progress_bar": False,
        "use_wandb": False,
        "use_mlflow": False,
        "raise_on_exception": True,
        "seed": seed,
    }


def optimizer_config_receipt(
    *,
    constructor: dict[str, Any],
    task_adapter: PredictorAdapter,
    reflection_lm: Any,
    proposer: InstructionProposer,
    growth_budget: InstructionGrowthBudget | None = None,
    metric_sha256: str,
    example_count: int,
    train_count: int,
    val_count: int,
) -> dict[str, Any]:
    return {
        "schema": "tracefold.news.compile_optimizer_config_receipt.v3",
        "optimizer": {
            "implementation": "gepa.optimize",
            "gepa_version": importlib.metadata.version("gepa"),
            # #306 Phase 3. `dspy.GEPA` and the private JSON-adapter surface under it are gone from this
            # path, so there is no DSPy version for this receipt to pin any more.
            "adapter": f"{NewsGepaAdapter.__module__}.{NewsGepaAdapter.__qualname__}",
            "evaluator": "production NewsSemanticProgram on one task endpoint",
        },
        "metric_sha256": metric_sha256,
        "constructor_scalar_arguments": _json_scalars(constructor),
        "instruction_proposer": {
            "implementation": f"{type(proposer).__module__}.{type(proposer).__qualname__}",
            "reads": "the current complete predictor instruction plus the reflective dataset",
            "writes": "one complete replacement predictor instruction",
        },
        # v3 (#334): a selection rule that can decide who wins belongs in the compile record. `null` means
        # the run was not budgeted, which is itself evidence.
        "instruction_growth_budget": growth_budget.receipt() if growth_budget is not None else None,
        "model_identities": {
            "task": require_model_identity(task_adapter, role="task").model_dump(mode="json"),
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


class ReflectionLM:
    """The reflection role, as one callable against one OpenAI-compatible endpoint.

    gepa calls a `LanguageModel` with either a rendered string or a message list and expects the reply text.
    That is the whole contract, which is why this is 40 lines rather than a framework: the reflection call
    is unstructured — it reads a minibatch of failures and writes an instruction — so none of the JSON-schema
    machinery the task route needs applies to it.
    """

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        api_base: str,
        max_tokens: int = REFLECTION_MAX_TOKENS,
        timeout: float = REFLECTION_TIMEOUT_SECONDS,
        model_kwargs: Mapping[str, Any] | None = None,
        transport: Any = None,
    ) -> None:
        extras = reject_owned_model_kwargs(model_kwargs, code="news_program_compile_model_kwargs_owned")
        self.model_name = str(model_name)
        self._wire_model = wire_model_name(self.model_name)
        self._api_key = str(api_key)
        self._url = chat_completions_url(api_base)
        self._max_tokens = int(max_tokens)
        self._timeout = float(timeout)
        self._extra_body = dict(extras.pop("extra_body", {}) or {})
        self._extras = extras
        self._transport = transport
        self.last_metrics: ProviderCallMetrics | None = None

    def __call__(self, prompt: str | list[dict[str, Any]]) -> str:
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
        body = {
            "model": self._wire_model,
            "messages": messages,
            "temperature": _REFLECTION_TEMPERATURE,
            "max_tokens": self._max_tokens,
            "stream": False,
            **self._extras,
            **self._extra_body,
        }
        reply = post_chat_completion_sync(
            url=self._url,
            body=body,
            api_key=self._api_key,
            timeout=self._timeout,
            transport=self._transport,
        )
        if reply.status_code >= 400 or reply.payload is None:
            detail = provider_error_detail(reply.payload)
            raise RuntimeError(
                f"news_program_compile_reflection_http_{reply.status_code}" + (f": {detail}" if detail else "")
            )
        payload = reply.payload
        self.last_metrics = provider_call_metrics(payload)
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], Mapping):
            raise ValueError("news_program_proposer_reflection_output_invalid")
        content = (choices[0].get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ValueError("news_program_proposer_reflection_output_invalid")
        return content


def build_task_adapter(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    timeout: float,
    max_tokens: int,
    model_kwargs: Mapping[str, Any] | None = None,
    temperature: float = _TASK_TEMPERATURE,
    structured_output: StructuredOutputMode | None = None,
    transport: Any = None,
) -> ChatCompletionsPredictorAdapter:
    """The task route `run_gepa` drives, stamped with the identity it will be held to.

    `transport` is the same seam the adapter itself exposes, forwarded so an offline test can drive the
    real builder — identity stamp included — instead of a hand-assembled stand-in for it.
    """

    adapter = ChatCompletionsPredictorAdapter(
        model_name=model_name,
        api_key=api_key,
        api_base=api_base,
        timeout=float(timeout),
        max_tokens=int(max_tokens),
        model_kwargs=model_kwargs,
        temperature=temperature,
        structured_output=structured_output,
        transport=transport,
    )
    adapter.tracefold_compiler_endpoint_identity = ModelExecutionIdentity.issue(  # type: ignore[attr-defined]
        role="task",
        model=str(model_name),
        api_base=str(api_base),
        max_output_tokens=int(max_tokens),
        timeout_seconds=float(timeout),
        temperature=temperature,
        model_kwargs=dict(model_kwargs or {}),
    )
    return adapter


def build_reflection_lm(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    model_kwargs: Mapping[str, Any] | None = None,
    transport: Any = None,
) -> ReflectionLM:
    """The reflection route, whose budget is exact rather than a floor.

    `ModelExecutionIdentity` holds the role to these values, so accepting a caller's larger timeout or token
    ceiling would silently contradict the thing meant to attest them.
    """

    lm = ReflectionLM(
        model_name=model_name,
        api_key=api_key,
        api_base=api_base,
        max_tokens=REFLECTION_MAX_TOKENS,
        timeout=REFLECTION_TIMEOUT_SECONDS,
        model_kwargs=model_kwargs,
        transport=transport,
    )
    lm.tracefold_compiler_endpoint_identity = ModelExecutionIdentity.issue(  # type: ignore[attr-defined]
        role="reflection",
        model=str(model_name),
        api_base=str(api_base),
        max_output_tokens=REFLECTION_MAX_TOKENS,
        timeout_seconds=REFLECTION_TIMEOUT_SECONDS,
        temperature=_REFLECTION_TEMPERATURE,
        model_kwargs=dict(model_kwargs or {}),
    )
    return lm


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


def trajectory_receipt(run: Any) -> dict[str, Any]:
    if run is None:
        raise ValueError("news_program_compile_trajectory_missing")
    scores = [float(value) for value in list(getattr(run, "val_aggregate_scores", ()) or ())]
    if any(not math.isfinite(score) for score in scores):
        raise TypeError("news_program_compile_nonfinite_receipt_value")
    return {
        "schema": "tracefold.news.compile_trajectory_receipt.v1",
        "parents": _json_scalars({"parents": list(getattr(run, "parents", ()) or ())})["parents"],
        "val_aggregate_scores": scores,
        "discovery_eval_counts": [int(value) for value in list(getattr(run, "discovery_eval_counts", ()) or ())],
        "total_metric_calls": (
            int(reported) if isinstance((reported := getattr(run, "total_metric_calls", None)), int) else -1
        ),
        "num_full_val_evals": int(getattr(run, "num_full_val_evals", 0) or 0),
        "seed": int(getattr(run, "seed", 0) or 0),
        "best_idx": int(getattr(run, "best_idx", 0) or 0),
    }


def checkpoint_receipt(winner: Mapping[str, str]) -> dict[str, Any]:
    """What one compile run produced, and the code identity it produced it under.

    v3, not v2: the receipt named a `factory` and now names an `envelope_sha256` (#314), and one schema
    string standing for two different key sets is the same lie a version exists to prevent. It also stopped
    taking a `parent` — the factory literal was the only thing it read from one.
    """

    return {
        "schema": "tracefold.news.compile_checkpoint_receipt.v3",
        "envelope_sha256": EXECUTION_ENVELOPE_SHA256,
        # The instruction text itself, not a digest of it: this receipt is the record of what the run
        # produced, and the winner's two instructions are already carried by the patch beside it.
        "predictors": {name: {"instruction": str(winner[name])} for name in COMPONENTS},
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
    "news_program_instruction_",
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
        # The first refusal, kept. A budget that stopped one call is a run-level answer, and the graph
        # under the metered adapter turns any exception into one case's failure — so the refusal has to
        # survive being swallowed there.
        self.exhausted: OptimizationBudgetExceeded | None = None
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
            raise self._refuse("news_learning_optimize_wall_clock_exhausted")
        used = self.task_model_calls if role == "task" else self.reflection_model_calls
        limit = self.budget.max_task_model_calls if role == "task" else self.budget.max_reflection_model_calls
        if used >= limit:
            raise self._refuse(f"news_program_compile_{role}_model_call_budget_exhausted")
        if self.actual_cost_microusd + self.budget.max_call_cost_microusd > self.budget.max_cost_microusd:
            raise self._refuse("news_program_compile_cost_reservation_exhausted")
        self._role = role
        if role == "task":
            self.task_model_calls += 1
        else:
            self.reflection_model_calls += 1

    def _refuse(self, code: str) -> OptimizationBudgetExceeded:
        refusal = OptimizationBudgetExceeded(code)
        self.exhausted = self.exhausted or refusal
        return refusal

    def _cost(self, metadata: ProviderCallMetrics) -> int:
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

    def after(self, metadata: ProviderCallMetrics) -> None:
        cost = self._cost(metadata)
        if cost > self.budget.max_call_cost_microusd:
            raise self._refuse("news_program_compile_call_cost_reservation_exceeded")
        self.actual_cost_microusd += cost
        if self._role == "task":
            self.task_cost_microusd += cost
        else:
            self.reflection_cost_microusd += cost
        if self.actual_cost_microusd > self.budget.max_cost_microusd:
            raise self._refuse("news_program_compile_cost_budget_exceeded")


def _is_transport_failure(exc: BaseException) -> bool:
    """The Program's own classifier, so "the provider did not answer" means one thing in both planes."""

    return _is_retryable_exception(exc)


class _MeteredPredictorAdapter:
    """Transparent Adapter proxy that makes every provider attempt observable and budgeted.

    Until #306 Phase 3 this was `_BudgetedLM`, a `dspy.BaseLM` subclass wrapping the framework's LM and
    settling its budget out of a captured provider response. The transport hands the response back
    directly now, so the proxy is a proxy: `before` admits the attempt, the call happens, `after` settles
    it. `before` does not accumulate anything — it refuses a call the budget could not afford one
    worst-case attempt of — so every attempt that reached the provider has to reach `after`, answered or
    refused. `transport_failures` counts only requests that never arrived, which is what its name says.
    """

    def __init__(self, adapter: PredictorAdapter, *, meter: _BudgetMeter) -> None:
        self._adapter = adapter
        self._meter = meter
        self.transport_failures = 0
        self.transport_retries = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def raise_if_exhausted(self) -> None:
        """Re-raise the first budget refusal, after the graph has had its chance to swallow it."""

        if self._meter.exhausted is not None:
            raise self._meter.exhausted

    def runtime_identity(self, model_binding: str) -> RuntimeModelIdentity:
        return self._adapter.runtime_identity(model_binding)

    async def invoke(self, request: PredictorRequest, spec: PredictorSpec) -> PredictorResponse:
        """One metered attempt, retried only on transport-class failures.

        The retry is here rather than in the transport because this proxy is what proves the budget: every
        physical request has to pass `before`/`after`. Without it, one transient 5xx from the single-slot
        local server made GEPA score that example as a failure — indistinguishable, on the Pareto front,
        from a candidate that genuinely answered badly.
        """

        last: BaseException | None = None
        for attempt in range(_NUM_RETRIES + 1):
            self._meter.before("task")
            try:
                response = await self._adapter.invoke(request, spec)
            except PredictorAdapterError as exc:
                if exc.provider_reached:
                    # The provider answered — with a refusal, an unparseable body or a truncation — so the
                    # attempt is settled before anything else happens to it. An observation carries exact
                    # usage; a bare status code carries none, and `_cost` then charges the operator's own
                    # declared per-call ceiling, which is the safe direction. Skipping this is how a run of
                    # 429s spends real provider work against a ledger that never moves: `before()` refuses
                    # a call the budget cannot afford, but it accumulates nothing on its own.
                    observation = exc.provider_observation
                    self._meter.after(
                        ProviderCallMetrics(
                            response_model=observation.model if observation else None,
                            input_tokens=observation.input_tokens if observation else 0,
                            output_tokens=observation.output_tokens if observation else 0,
                            cached_tokens=observation.cached_tokens if observation else 0,
                            total_tokens=observation.total_tokens if observation else 0,
                            provider_cost_microusd=observation.provider_cost_microusd if observation else None,
                            finish_reason=observation.finish_reason if observation else exc.finish_reason,
                        )
                    )
                    if not exc.retryable or attempt == _NUM_RETRIES:
                        raise
                    self.transport_retries += 1
                    last = exc
                    continue
                # The request never arrived, so nothing reported usage and nothing was billed. `before()`
                # refused to start it unless the budget could afford one worst-case call, which is the
                # bound that matters when there is nothing to settle.
                if not exc.retryable or attempt == _NUM_RETRIES:
                    self.transport_failures += 1
                    raise
                self.transport_retries += 1
                last = exc
                continue
            except BaseException as exc:
                if not _is_transport_failure(exc) or attempt == _NUM_RETRIES:
                    self.transport_failures += 1
                    raise
                self.transport_retries += 1
                last = exc
                continue
            self._meter.after(
                ProviderCallMetrics(
                    response_model=response.model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cached_tokens=response.cached_tokens,
                    total_tokens=response.total_tokens,
                    provider_cost_microusd=response.provider_cost_microusd,
                    finish_reason=response.finish_reason,
                )
            )
            return response
        raise last if last is not None else RuntimeError("news_program_compile_lm_retry_invariant")


class _MeteredReflectionLM:
    """The same proof for the reflection role, which answers text rather than a typed schema."""

    def __init__(self, lm: Any, *, meter: _BudgetMeter) -> None:
        self._lm = lm
        self._meter = meter
        self.transport_failures = 0
        self.transport_retries = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._lm, name)

    def __call__(self, prompt: Any) -> str:
        """One metered attempt, retried only on transport-class failures.

        The retry is not decoration. `raise_on_exception` is on, so one transient connection reset during
        a reflection call would otherwise abort a multi-hour run — and the predecessor `_BudgetedLM`
        routed *both* roles through the same retry, so dropping it here would have been a silent
        regression rather than a decision.
        """

        last: BaseException | None = None
        for attempt in range(_NUM_RETRIES + 1):
            self._meter.before("reflection")
            try:
                answer = self._lm(prompt)
            except BaseException as exc:
                if not _is_transport_failure(exc) or attempt == _NUM_RETRIES:
                    self.transport_failures += 1
                    raise
                self.transport_retries += 1
                last = exc
                continue
            metrics = getattr(self._lm, "last_metrics", None)
            self._meter.after(metrics if isinstance(metrics, ProviderCallMetrics) else ProviderCallMetrics())
            return str(answer)
        raise last if last is not None else RuntimeError("news_program_compile_lm_retry_invariant")


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

    task_adapter: PredictorAdapter
    reflection_lm: Any
    judge: Any
    budget: OptimizationBudget
    # Injected so a test can drive the whole entry point without gepa-core; production leaves it None and
    # `run_gepa` calls `gepa.optimize`.
    optimize_fn: Callable[..., Any] | None = None
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
    task_identity = require_model_identity(config.task_adapter, role="task")
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
    task_adapter = _MeteredPredictorAdapter(config.task_adapter, meter=meter)
    reflection_lm = _MeteredReflectionLM(config.reflection_lm, meter=meter)
    budgeted: tuple[Any, ...] = (task_adapter, reflection_lm)
    run: GepaRunResult | None = None
    reasons: tuple[str, ...] = ()
    outcome: Literal["NO_OP", "REJECTED", "ADVANCE"] = "ADVANCE"
    try:
        run = run_gepa(
            base_program=dataset.parent_program,
            episodes=dataset.episodes,
            task_adapter=task_adapter,
            reflection_lm=reflection_lm,
            judge=config.judge,
            max_metric_calls=config.budget.max_metric_calls,
            seed=config.budget.seed,
            review_rubric_version=dataset.ref.review_rubric_version,
            optimize_fn=config.optimize_fn,
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
    budgeted: Sequence[Any],
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
    "COMPONENTS",
    "METRIC_JUDGE_MAX_TOKENS",
    "METRIC_JUDGE_TIMEOUT_SECONDS",
    "OBJECTIVE_SUMMARY_SCHEMA",
    "REFLECTION_MAX_TOKENS",
    "REFLECTION_TIMEOUT_SECONDS",
    "USAGE_SCHEMA",
    "FrozenDevelopmentDataset",
    "GepaNoProgramChange",
    "GepaRunResult",
    "InstructionProposer",
    "ModelExecutionIdentity",
    "NewsGepaAdapter",
    "OptimizationBudgetExceeded",
    "OptimizationConfig",
    "OptimizerRole",
    "ReflectionLM",
    "build_reflection_lm",
    "build_task_adapter",
    "checkpoint_receipt",
    "gepa_metric_call_ceiling",
    "objective_summary",
    "optimize",
    "optimizer_config_receipt",
    "optimizer_constructor",
    "plan_blockers",
    "require_model_identity",
    "run_gepa",
    "trajectory_receipt",
]
