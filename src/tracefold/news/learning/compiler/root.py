"""Untrusted, bounded GEPA logic executed only by the compiler container.

The trusted host seals the ``program_v7`` corpus and launches the runner.  This
module has no database, artifact-writer, proposal or promotion authority.  It
can return only a ``ProgramStrategyPatchV1`` — the two advisory instructions —
and content-addressable optimizer receipt payloads.
"""

from __future__ import annotations

from typing import Any, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import Field, model_validator

from ...program.artifact import (
    ProgramStrategyArtifactV1,
    load_stable_program_artifact,
)
from ..metric import (
    METRIC_ID,
    _json_safe,
    accepted_review_metric,
)
from ..objective import DevelopmentEpisode, _ExactModel
from ..optimizer import OptimizationBudgetExceeded, _BudgetedLM, _BudgetMeter
from .gepa import GepaRunResult, OptimizerFactory, build_compile_lm, require_model_identity, run_gepa
from .security import (
    CompileBudgetV3,
    CompilerProxyTariff,
    CompileSpend,
)

LEARNING_EPOCH = "program_v7"
COMPILER_ID = "tracefold.news.dspy_gepa_compiler_v3"
_PROPOSAL_GUARDRAILS = (
    "fixed_factory_v4",
    "development_only",
    "holdout_unseen",
    "no_dynamic_code",
    "no_auto_promotion",
)


class CompileBudget(CompileBudgetV3):
    """Three independent operator-owned limits for one cold compile."""


class CompileRequest(_ExactModel):
    development_dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    learning_epoch: Literal["program_v7"] = "program_v7"
    # Declared by the trusted host in the sealed corpus receipt. The compiler records it; it never looks it up,
    # so the untrusted side does not import the review plane to obtain one string.
    review_rubric_version: str = Field(min_length=1, max_length=64)
    episodes: tuple[DevelopmentEpisode, ...] = Field(min_length=1)
    budget: CompileBudget


class ProgramCompileResult(_ExactModel):
    """What one bounded compile produced, and what it cost. Two things, kept apart on purpose.

    `run` is the optimization, owned by the shared core both planes call. `spend` is what this plane
    uniquely knows: how many physical provider calls the meter counted and what they cost. Metering is
    deliberately not `run_gepa`'s business — the experiment loop runs the same optimization against
    unmetered endpoints.

    Both halves are the same objects the host receives and the record embeds. There used to be a
    `CompileReceiptPayloads` wrapper here restating the six receipts `GepaRunResult` already carries,
    seven more fields restating the rest of it, a byte-identical copy of the accounting validator, and a
    field-by-field copy in the runner turning all of it into a third model of the same document.
    """

    run: GepaRunResult
    spend: CompileSpend

    @model_validator(mode="after")
    def _every_retained_payload_is_finite_json(self) -> ProgramCompileResult:
        for payload in (
            self.run.metric,
            self.run.optimizer_config,
            self.run.trajectory,
            self.run.checkpoint,
            self.run.split,
            self.run.retrieval,
        ):
            _json_safe(payload)
        return self


class ProgramCompiler:
    """Bounded cold optimizer for the fixed v2 semantic Program factory."""

    def __init__(
        self,
        *,
        base_artifact: ProgramStrategyArtifactV1,
        task_lm: dspy.LM,
        reflection_lm: dspy.LM,
        optimizer_factory: OptimizerFactory = dspy.GEPA,
        tariff: CompilerProxyTariff | None = None,
        judge: Any = None,
    ) -> None:
        active = load_stable_program_artifact()
        if base_artifact.program_sha256 != active.program_sha256:
            raise ValueError("news_program_compile_parent_must_be_exact_stable_root")
        require_model_identity(task_lm, role="task")
        require_model_identity(reflection_lm, role="reflection")
        self._base = base_artifact
        self._task_lm = task_lm
        self._reflection_lm = reflection_lm
        self._optimizer_factory = optimizer_factory
        self._tariff = tariff
        # #148/#160: the evidence-grounded equivalence judge makes the 10% ReaderCard component movable and
        # verifies factual corrections against immutable evidence.
        # The baseline harness and the optimizer must use the same ruler or the "before/after" number an
        # operator reads stops predicting what GEPA maximizes.
        self._judge = judge

    def compile(self, request: CompileRequest) -> ProgramCompileResult:
        if request.learning_epoch != LEARNING_EPOCH:
            raise ValueError("news_program_compile_epoch_mismatch")
        meter = _BudgetMeter(request.budget, tariff=self._tariff)
        # The only thing this plane adds to the shared core: every physical provider call is metered
        # against the operator's budget before it is made. The experiment loop runs the same optimizer
        # over plain LMs, which is what makes the two planes' numbers comparable at all.
        result = run_gepa(
            base_program=self._base,
            episodes=request.episodes,
            task_lm=_BudgetedLM(self._task_lm, role="task", meter=meter),
            reflection_lm=_BudgetedLM(self._reflection_lm, role="reflection", meter=meter),
            judge=self._judge,
            max_metric_calls=request.budget.max_metric_calls,
            seed=request.budget.seed,
            review_rubric_version=request.review_rubric_version,
            optimizer_factory=self._optimizer_factory,
        )
        judge_stats = dict(self._judge.stats)
        metric_judge_attempts = int(judge_stats.get("attempts", -1))
        metric_judge_model_calls = int(judge_stats.get("model_calls", -1))
        metric_judge_failures = int(judge_stats.get("failures", -1))
        metric_judge_cost_microusd = int(judge_stats.get("actual_cost_microusd", -1))
        if (
            min(
                metric_judge_attempts,
                metric_judge_model_calls,
                metric_judge_failures,
                metric_judge_cost_microusd,
            )
            < 0
            or metric_judge_model_calls > request.budget.max_metric_judge_model_calls
            or metric_judge_model_calls > metric_judge_attempts
            or metric_judge_failures > metric_judge_attempts
        ):
            raise ValueError("news_program_compile_metric_judge_accounting_invalid")
        total_cost_microusd = meter.actual_cost_microusd + metric_judge_cost_microusd
        if total_cost_microusd > request.budget.max_cost_microusd:
            raise OptimizationBudgetExceeded("news_program_compile_cost_budget_exceeded")
        return ProgramCompileResult(
            run=result,
            spend=CompileSpend(
                task_model_calls=meter.task_model_calls,
                reflection_model_calls=meter.reflection_model_calls,
                metric_judge_attempts=metric_judge_attempts,
                metric_judge_model_calls=metric_judge_model_calls,
                metric_judge_failures=metric_judge_failures,
                task_cost_microusd=meter.task_cost_microusd,
                reflection_cost_microusd=meter.reflection_cost_microusd,
                metric_judge_cost_microusd=metric_judge_cost_microusd,
                actual_cost_microusd=total_cost_microusd,
            ),
        )


__all__ = [
    "COMPILER_ID",
    "LEARNING_EPOCH",
    "METRIC_ID",
    "CompileBudget",
    "CompileRequest",
    "DevelopmentEpisode",
    "ProgramCompileResult",
    "ProgramCompiler",
    "accepted_review_metric",
    "build_compile_lm",
]
