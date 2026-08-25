"""Untrusted, bounded GEPA logic executed only by the compiler container.

The trusted host seals the ``program_v7`` corpus and launches the runner.  This
module has no database, artifact-writer, proposal or promotion authority.  It
can return only a ``ProgramStrategyPatchV1`` — the two advisory instructions —
and content-addressable optimizer receipt payloads.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import Field, model_validator

from ...program.artifact import (
    ProgramStrategyArtifactV1,
    load_stable_program_artifact,
)
from ...program.dspy_adapter import (
    ExactProviderCallCapture,
    ExactProviderMetadata,
    PredictorAdapterError,
    _is_retryable_exception,
)
from ..metric import (
    METRIC_ID,
    _json_safe,
    accepted_review_metric,
)
from ..objective import DevelopmentEpisode, _ExactModel
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


class CompileBudgetExceeded(RuntimeError):
    """Raised before another model call, or after a provider reports overspend."""


class _BudgetMeter:
    def __init__(self, budget: CompileBudget, *, tariff: CompilerProxyTariff | None = None) -> None:
        self.budget = budget
        # Neither the local llama.cpp endpoint nor DeepSeek returns a price litellm can resolve, so
        # `provider_cost_microusd` is `None` for every endpoint this project actually uses and the meter used
        # to fail closed on the first call. The tariff is the trusted worst-case rate the proxy sidecar already
        # applies; accepting it here is what lets the compiler be metered with or without that sidecar, rather
        # than making the budget proof depend on a price map the provider does not publish.
        self.tariff = tariff
        self.task_model_calls = 0
        self.reflection_model_calls = 0
        self.task_cost_microusd = 0
        self.reflection_cost_microusd = 0
        self.actual_cost_microusd = 0
        self.imputed_cost_calls = 0

    @property
    def total_model_calls(self) -> int:
        return self.task_model_calls + self.reflection_model_calls

    def before(self, role: Literal["task", "reflection"]) -> None:
        used = self.task_model_calls if role == "task" else self.reflection_model_calls
        limit = self.budget.max_task_model_calls if role == "task" else self.budget.max_reflection_model_calls
        if used >= limit:
            raise CompileBudgetExceeded(f"news_program_compile_{role}_model_call_budget_exhausted")
        if self.actual_cost_microusd + self.budget.max_call_cost_microusd > self.budget.max_cost_microusd:
            raise CompileBudgetExceeded("news_program_compile_cost_reservation_exhausted")
        self._role = role
        if role == "task":
            self.task_model_calls += 1
        else:
            self.reflection_model_calls += 1

    def _cost(self, metadata: ExactProviderMetadata) -> int:
        if metadata.provider_cost_microusd is not None:
            return metadata.provider_cost_microusd
        if self.tariff is None:
            raise CompileBudgetExceeded("news_program_compile_provider_cost_unavailable")
        self.imputed_cost_calls += 1
        # Charged at the trusted worst-case rate, from tokens the provider did report. Over-charging is the
        # safe direction: the budget stops the run early rather than late.
        return self.tariff.worst_case_cost_microusd(
            role=getattr(self, "_role", "task"),
            request_bytes=metadata.input_tokens,
            max_output_tokens=max(1, metadata.output_tokens),
        )

    def after(self, metadata: ExactProviderMetadata) -> None:
        cost = self._cost(metadata)
        if cost > self.budget.max_call_cost_microusd:
            raise CompileBudgetExceeded("news_program_compile_call_cost_reservation_exceeded")
        self.actual_cost_microusd += cost
        if getattr(self, "_role", "task") == "task":
            self.task_cost_microusd += cost
        else:
            self.reflection_cost_microusd += cost
        if self.actual_cost_microusd > self.budget.max_cost_microusd:
            raise CompileBudgetExceeded("news_program_compile_cost_budget_exceeded")


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
        for attempt in range(_COMPILE_NUM_RETRIES + 1):
            self._meter.before(self._role)
            with self._lm.observe_exact_call() as capture:
                try:
                    output = call()
                except BaseException as exc:
                    # A transport failure means the provider never returned a response, so nothing recorded
                    # usage or cost and `_require_compile_metadata` would raise here — aborting the compile and
                    # masking the original error. `before()` has already reserved this attempt's worst-case
                    # cost, so the budget stays bounded without a settle-up the provider never supplied.
                    transport = _is_transport_failure(exc)
                    if not transport:
                        self._meter.after(_require_compile_metadata(capture))
                        raise
                    if attempt == _COMPILE_NUM_RETRIES:
                        self.transport_failures += 1
                        raise
                    self.transport_retries += 1
                    last = exc
                    continue
            self._meter.after(_require_compile_metadata(capture))
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
                self._meter.after(_require_compile_metadata(capture))
                raise
        self._meter.after(_require_compile_metadata(capture))
        return output


def _require_compile_metadata(capture: ExactProviderCallCapture) -> ExactProviderMetadata:
    try:
        return capture.require_exactly_one()
    except PredictorAdapterError as exc:
        raise CompileBudgetExceeded("news_program_compile_provider_metadata_unavailable") from exc


# One transient 5xx from a single-slot local server is not evidence that a candidate is bad, but with
# `num_retries=0` GEPA scored it as `failure_score` and moved on. The production route keeps retries off.
_COMPILE_NUM_RETRIES = 2


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
            raise CompileBudgetExceeded("news_program_compile_cost_budget_exceeded")
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
    "CompileBudgetExceeded",
    "CompileRequest",
    "DevelopmentEpisode",
    "ProgramCompileResult",
    "ProgramCompiler",
    "accepted_review_metric",
    "build_compile_lm",
]
