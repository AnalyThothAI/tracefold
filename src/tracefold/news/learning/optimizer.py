"""The one offline optimization this repository runs, and the only thing that produces a Prompt candidate.

Before #202 there were two. The trusted compiler sealed a corpus, launched a container against a metered
proxy and produced a `CompileRecordV1` that a release gate would accept; the experiment loop ran the same
`run_gepa()` in process and produced an `ExperimentCandidate` marked `promotable=False`, which had to be
reproduced by a container before any gate would look at it. Both optimized the same two strings.

The generator was never the authority for those two strings, and treating it as one cost a whole platform:
an image, a launcher, a proxy sidecar, a sandbox policy, a tariff, three-party build attestation and a
smoke lane. What actually makes a candidate trustworthy is downstream of generation — a bounded write-set,
a parent bound to the active stable, a frozen dataset, an independent evaluation, future holdout, shadow,
canary and a human promotion — and none of it needs to know where the text came from.

So this module owns generation, whole: the budget every physical provider call is metered against, the
Objective Plan that decides what GEPA may see, the run itself, and the terminal answer. It owns nothing
downstream: no database handle, no artifact writer, no registration, no canary, no promotion. A run ends in
`NO_OP`, `REJECTED` or `ADVANCE`, and `ADVANCE` is a candidate, not a release.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import dspy  # type: ignore[import-untyped]

from ..artifact_identity import canonical_sha
from ..program.artifact import ProgramStrategyArtifactV1, load_stable_program_artifact
from ..program.dspy_adapter import (
    ExactProviderCallCapture,
    ExactProviderMetadata,
    PredictorAdapterError,
    _is_retryable_exception,
)
from .compiler.gepa import GepaNoProgramChange, GepaRunResult, OptimizerFactory, require_model_identity, run_gepa
from .contracts import (
    DevelopmentDatasetRef,
    OptimizationBudget,
    OptimizationResult,
    OptimizationRunReport,
    PromptCandidateV1,
    PromptPatchV1,
)
from .objective import DevelopmentEpisode, GepaObjectivePlan, build_gepa_objective_plan, retrieval_receipt

OBJECTIVE_SUMMARY_SCHEMA = "tracefold.news.optimization_objective_summary.v1"
USAGE_SCHEMA = "tracefold.news.optimization_usage.v1"

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

    def __post_init__(self) -> None:
        if not self.episodes:
            raise ValueError("news_learning_optimize_dataset_empty")
        if len(self.episodes) != self.ref.episode_count:
            raise ValueError("news_learning_optimize_dataset_episode_count_mismatch")
        projection = canonical_sha([episode.model_dump(mode="json") for episode in self.episodes])
        if projection != self.ref.episode_projection_root_sha256:
            raise ValueError("news_learning_optimize_dataset_projection_root_mismatch")

    @classmethod
    def bind(
        cls,
        *,
        ref: DevelopmentDatasetRef,
        episodes: Sequence[DevelopmentEpisode],
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


def objective_summary(plan: GepaObjectivePlan) -> dict[str, Any]:
    """What the Objective Plan decided, in the shape a candidate and a report both carry.

    Per-case dispositions are not here: `readiness` publishes those, and a candidate that embedded them
    would grow with the corpus while saying nothing a reader could act on. What survives is the membership
    a later evaluation has to reproduce exactly.
    """

    return {
        "schema": OBJECTIVE_SUMMARY_SCHEMA,
        "case_n": plan.case_n,
        "cluster_n": plan.cluster_n,
        "target_case_ids": list(plan.target_case_ids),
        "target_failure_cluster_ids": list(plan.target_failure_cluster_ids),
        "control_case_ids": list(plan.control_case_ids),
        "control_cluster_ids": list(plan.control_cluster_ids),
        "optimizer_case_ids": list(plan.optimizer_case_ids),
        "excluded_case_ids": list(plan.excluded_case_ids),
        "exclusion_reasons": dict(plan.exclusion_reasons),
        "target_predictors": list(plan.target_predictors),
        "target_dimensions": list(plan.target_dimensions),
        "exact_gold_coverage": dict(plan.exact_gold_coverage),
        "owner_distribution": dict(plan.owner_distribution),
        "blocking_reasons": list(plan.blocking_reasons),
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

    if config.judge is None:
        raise ValueError("news_program_compile_metric_judge_required")
    # Before anything is spent: the three roles answer under identities they were stamped with, or the run
    # does not start. Reconstructing an identity from the object it describes would attest nothing.
    identities = {
        "task": require_model_identity(config.task_lm, role="task").model_dump(mode="json"),
        "reflection": require_model_identity(config.reflection_lm, role="reflection").model_dump(mode="json"),
        "metric_judge": dict(getattr(config.judge, "identity", {})),
    }
    started_at_ms = config.now_ms()
    plan = build_gepa_objective_plan(dataset.episodes)
    objective = objective_summary(plan)
    blockers = plan_blockers(plan)
    if blockers:
        return _terminal(
            "REJECTED",
            dataset=dataset,
            config=config,
            objective=objective,
            identities=identities,
            usage=_usage(meter=None, judge=config.judge, metric_calls=0, budgeted=()),
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
    usage = _usage(meter=meter, judge=config.judge, metric_calls=metric_calls, budgeted=budgeted)
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
) -> dict[str, Any]:
    stats = dict(getattr(judge, "stats", {}) or {})
    judge_calls = int(stats.get("model_calls", 0))
    judge_cost = int(stats.get("actual_cost_microusd", 0))
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
    "OBJECTIVE_SUMMARY_SCHEMA",
    "USAGE_SCHEMA",
    "FrozenDevelopmentDataset",
    "OptimizationBudgetExceeded",
    "OptimizationConfig",
    "objective_summary",
    "optimize",
    "plan_blockers",
]
