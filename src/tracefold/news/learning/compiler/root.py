"""Untrusted, bounded GEPA logic executed only by the compiler container.

The trusted host seals the ``program_v7`` corpus and launches the runner.  This
module has no database, artifact-writer, proposal or promotion authority.  It
can return only a ``ProgramStrategyPatchV1`` — the two advisory instructions —
and content-addressable optimizer receipt payloads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

import dspy  # type: ignore[import-untyped]
from pydantic import Field, ValidationError, model_validator

from ...program.artifact import (
    ProgramStrategyArtifactV1,
    ProgramStrategyPatchV1,
    load_stable_program_artifact,
)
from ...program.dspy_adapter import (
    ExactMetadataDspyLM,
    ExactProviderCallCapture,
    ExactProviderMetadata,
    PredictorAdapterError,
    _is_retryable_exception,
)
from ...program.graph import (
    DspyCompileProgram,
)
from ..metric import (
    METRIC_ID,
    DevelopmentEpisode,
    _ExactModel,
    _json_safe,
    accepted_review_metric,
)
from .gepa import OptimizerFactory, require_model_identity, run_gepa
from .security import (
    CompileBudgetV3,
    CompilerProxyTariff,
    ModelExecutionIdentity,
)
from .trusted import REFLECTION_MAX_TOKENS, REFLECTION_TIMEOUT_SECONDS

_OWNED_LM_KWARGS = frozenset(
    {"api_key", "api_base", "base_url", "cache", "num_retries", "temperature", "max_tokens", "timeout"}
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


class CompileReceiptPayloads(_ExactModel):
    """Canonical, secret-free evidence behind every compile provenance hash."""

    metric: dict[str, Any]
    optimizer_config: dict[str, Any]
    trajectory: dict[str, Any]
    checkpoint: dict[str, Any]
    # The two things a scalar score cannot answer: was the winner picked on examples it never trained on,
    # and did the model even see the card it was supposed to recognise.
    split: dict[str, Any] = Field(default_factory=dict)
    retrieval: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _all_payloads_are_finite_json(self) -> CompileReceiptPayloads:
        for payload in (
            self.metric,
            self.optimizer_config,
            self.trajectory,
            self.checkpoint,
            self.split,
            self.retrieval,
        ):
            _json_safe(payload)
        return self


class ProgramCompileResult(_ExactModel):
    """Untrusted runner output; trusted host still validates and applies it."""

    patch: ProgramStrategyPatchV1
    receipt_payloads: CompileReceiptPayloads
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    metric_calls: int = Field(ge=0)
    task_model_calls: int = Field(ge=0)
    reflection_model_calls: int = Field(ge=0)
    metric_judge_attempts: int = Field(ge=0)
    metric_judge_model_calls: int = Field(ge=0)
    metric_judge_failures: int = Field(ge=0)
    task_cost_microusd: int = Field(ge=0)
    reflection_cost_microusd: int = Field(ge=0)
    metric_judge_cost_microusd: int = Field(ge=0)
    actual_cost_microusd: int = Field(ge=0)

    @model_validator(mode="after")
    def _accounting_is_coherent(self) -> ProgramCompileResult:
        """Check the arithmetic. There is nothing here to cross-bind.

        This used to hash each of six payloads and compare the result against a hash of the same payload,
        which is a comparison that cannot fail. Everything these payloads have to be bound to lives in the
        record the trusted host builds around them.
        """

        if (
            self.metric_judge_model_calls > self.metric_judge_attempts
            or self.metric_judge_failures > self.metric_judge_attempts
            or self.actual_cost_microusd
            != self.task_cost_microusd + self.reflection_cost_microusd + self.metric_judge_cost_microusd
        ):
            raise ValueError("news_program_compile_result_accounting_mismatch")
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

    It lives here rather than in the Program package because it is optimizer-only: nothing in the production
    graph may learn to answer with `advisory_rejected`, and the module boundary is what keeps that true.
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


# The task LM answers the Program's own signatures, so it keeps the production route's determinism: temperature
# 0 and the route's own token ceiling. The reflection LM does something else entirely — it reads a minibatch of
# failures and writes a whole new instruction — and DSPy's guidance for it is the opposite on every axis. Until
# #143 both were built from the task route's numbers, which capped a proposed instruction at 1,200 tokens (below
# what `LearnedStrategy` itself accepts) and gave a reflection call the 20 s route deadline.
_REFLECTION_TEMPERATURE = 1.0
_TASK_TEMPERATURE = 0
# Aligned with the reflection settings DSPy documents for GEPA.
# One transient 5xx from a single-slot local server is not evidence that a candidate is bad, but with
# `num_retries=0` GEPA scored it as `failure_score` and moved on. The production route keeps retries off.
_COMPILE_NUM_RETRIES = 2


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
    """Build the cold compiler LM while keeping DSPy out of the CLI layer."""

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
            student_factory=_FeedbackCompileProgram,
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
            patch=result.patch,
            receipt_payloads=CompileReceiptPayloads(
                metric=result.metric,
                optimizer_config=result.optimizer_config,
                trajectory=result.trajectory,
                checkpoint=result.checkpoint,
                split=result.split,
                retrieval=result.retrieval,
            ),
            failure_cluster_ids=result.failure_cluster_ids,
            target_dimensions=result.target_dimensions,
            metric_calls=result.metric_calls,
            task_model_calls=meter.task_model_calls,
            reflection_model_calls=meter.reflection_model_calls,
            metric_judge_attempts=metric_judge_attempts,
            metric_judge_model_calls=metric_judge_model_calls,
            metric_judge_failures=metric_judge_failures,
            task_cost_microusd=meter.task_cost_microusd,
            reflection_cost_microusd=meter.reflection_cost_microusd,
            metric_judge_cost_microusd=metric_judge_cost_microusd,
            actual_cost_microusd=total_cost_microusd,
        )


__all__ = [
    "COMPILER_ID",
    "LEARNING_EPOCH",
    "METRIC_ID",
    "CompileBudget",
    "CompileBudgetExceeded",
    "CompileReceiptPayloads",
    "CompileRequest",
    "DevelopmentEpisode",
    "ProgramCompileResult",
    "ProgramCompiler",
    "accepted_review_metric",
    "build_compile_lm",
]
