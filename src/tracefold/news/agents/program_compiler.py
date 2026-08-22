"""Untrusted, bounded GEPA logic executed only by the compiler container.

The trusted host seals the ``program_v5`` corpus and launches the runner.  This
module has no database, artifact-writer, proposal or promotion authority.  It
can return only ``ProgramPatchV2`` (two LearnedStrategies plus eligible demo
references) and content-addressable optimizer receipt payloads.
"""

from __future__ import annotations

import importlib.metadata
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol, cast

import dspy  # type: ignore[import-untyped]
from pydantic import Field, ValidationError, model_validator

from ..artifact_identity import canonical_sha
from .program_compiler_security import CompileBudgetV2, CompilerEndpointIdentity, CompilerProxyTariff
from .program_compiler_trusted import REFLECTION_MAX_TOKENS, REFLECTION_TIMEOUT_SECONDS
from .program_metric import (
    METRIC_ID,
    DevelopmentEpisode,
    _compile_example,
    _ExactModel,
    _honest_split,
    _json_safe,
    _metric_receipt,
    _retrieval_receipt,
    accepted_review_metric,
)
from .program_proposer import RulePackAwareProposer
from .semantic_program import (
    DspyCompileProgram,
    DspyStrictJSONAdapter,
    EligibleDemoBank,
    ExactMetadataDspyLM,
    ExactProviderCallCapture,
    ExactProviderMetadata,
    PredictorAdapterError,
    ProgramArtifact,
    ProgramPatchV2,
    _is_retryable_exception,
    extract_optimizer_patch,
    load_stable_program_artifact,
)

LEARNING_EPOCH = "program_v5"
COMPILER_ID = "tracefold.news.dspy_gepa_compiler_v2"
_PROPOSAL_GUARDRAILS = (
    "fixed_factory_v2",
    "development_only",
    "holdout_unseen",
    "no_dynamic_code",
    "no_auto_promotion",
)


class CompileBudget(CompileBudgetV2):
    """Three independent operator-owned limits for one cold compile."""


class CompileRequest(_ExactModel):
    development_dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    learning_epoch: Literal["program_v5"] = "program_v5"
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

    patch: ProgramPatchV2
    receipt_payloads: CompileReceiptPayloads
    failure_cluster_ids: tuple[str, ...] = Field(min_length=1)
    target_dimensions: tuple[str, ...] = Field(min_length=1)
    metric_calls: int = Field(ge=0)
    task_model_calls: int = Field(ge=0)
    reflection_model_calls: int = Field(ge=0)
    actual_cost_microusd: int = Field(ge=0)

    @model_validator(mode="after")
    def _receipt_identities_match(self) -> ProgramCompileResult:
        hashes = {
            "metric": canonical_sha(self.receipt_payloads.metric),
            "optimizer_config": canonical_sha(self.receipt_payloads.optimizer_config),
            "trajectory": canonical_sha(self.receipt_payloads.trajectory),
            "checkpoint": canonical_sha(self.receipt_payloads.checkpoint),
            "split": canonical_sha(self.receipt_payloads.split),
            "retrieval": canonical_sha(self.receipt_payloads.retrieval),
        }
        payloads = self.receipt_payloads.model_dump(mode="json")
        for name, actual in hashes.items():
            if actual != canonical_sha(payloads[name]):
                raise ValueError(f"news_program_compile_{name}_receipt_hash_mismatch")
        return self


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
        self.actual_cost_microusd = 0
        self.imputed_cost_calls = 0

    @property
    def total_model_calls(self) -> int:
        return self.task_model_calls + self.reflection_model_calls

    def before(self, role: Literal["task", "reflection"]) -> None:
        if self.total_model_calls >= self.budget.max_task_model_calls:
            raise CompileBudgetExceeded("news_program_compile_task_model_call_budget_exhausted")
        if (self.total_model_calls + 1) * self.budget.max_call_cost_microusd > self.budget.max_cost_microusd:
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

    It lives here rather than in `semantic_program` on purpose: the factory source is hashed into the shipped
    Artifact's QualityKernel, so editing that module would force a new `program_sha256` and reset the review
    corpus — the exact failure mode #143 exists to stop.
    """

    def forward(self, evidence_json: str, card_evidence_json: str, told_count: int) -> dspy.Prediction:
        trace = dspy.settings.trace
        before = len(trace) if isinstance(trace, list) else None
        try:
            prediction = cast(dspy.Prediction, super().forward(evidence_json, card_evidence_json, told_count))
        except (ValidationError, ValueError) as exc:
            code = _advisory_rejection_code(exc)
            if code is None:
                raise
            return dspy.Prediction(semantics=None, card=None, verdict=None, advisory_rejected=code)
        if before is not None:
            self._rekey_trace(trace, before)
        return prediction

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
                    self._meter.after(_require_compile_metadata(capture))
                    if not _is_transport_failure(exc) or attempt == _COMPILE_NUM_RETRIES:
                        if _is_transport_failure(exc):
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
    owned = {"api_key", "api_base", "base_url", "cache", "num_retries", "temperature", "max_tokens", "timeout"}
    overlap = owned.intersection(extras)
    if overlap:
        raise ValueError(f"news_program_compile_model_kwargs_owned:{','.join(sorted(overlap))}")
    reflection = role == "reflection"
    lm = ExactMetadataDspyLM(
        str(model_name),
        api_key=str(api_key),
        api_base=str(api_base),
        timeout=max(float(timeout), REFLECTION_TIMEOUT_SECONDS) if reflection else float(timeout),
        max_tokens=max(int(max_tokens), REFLECTION_MAX_TOKENS) if reflection else int(max_tokens),
        temperature=_REFLECTION_TEMPERATURE if reflection else _TASK_TEMPERATURE,
        cache=False,
        # Stays zero. `_BudgetedLM` counts every physical attempt against the operator's budget, and a retry
        # hidden inside the provider client would be a request the receipt never saw. The retry that #143 adds
        # lives one layer up, where it is metered.
        num_retries=0,
        **extras,
    )
    lm.tracefold_compiler_endpoint_identity = CompilerEndpointIdentity.issue(
        model=str(model_name), api_base=str(api_base)
    )
    return lm


def _compiler_model_identity(lm: dspy.LM) -> dict[str, Any]:
    identity = getattr(lm, "tracefold_compiler_endpoint_identity", None)
    if isinstance(identity, CompilerEndpointIdentity):
        return identity.model_dump(mode="json")
    model = str(getattr(lm, "model", "")).strip()
    kwargs = getattr(lm, "kwargs", None)
    api_base = str(kwargs.get("api_base") or "") if isinstance(kwargs, Mapping) else ""
    if not model or not api_base:
        raise ValueError("news_program_compile_endpoint_identity_unavailable")
    return CompilerEndpointIdentity.issue(model=model, api_base=api_base).model_dump(mode="json")


def _optimizer_config_receipt(
    *,
    constructor: Mapping[str, Any],
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
        "constructor_scalar_arguments": _json_safe(
            {key: value for key, value in constructor.items() if key != "instruction_proposer"}
        ),
        "instruction_proposer": {
            "implementation": f"{type(constructor['instruction_proposer']).__module__}."
            f"{type(constructor['instruction_proposer']).__qualname__}"
            if constructor.get("instruction_proposer") is not None
            else None,
            "reads": "full rendered predictor instruction (QualityKernel + ordered RulePacks + authority seal)",
            "writes": "LearnedStrategy body only",
        },
        "model_identities": {
            "task": _compiler_model_identity(task_lm),
            "reflection": _compiler_model_identity(reflection_lm),
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


class ProgramCompiler:
    """Bounded cold optimizer for the fixed v2 semantic Program factory."""

    def __init__(
        self,
        *,
        base_artifact: ProgramArtifact,
        eligible_demo_bank: EligibleDemoBank,
        task_lm: dspy.LM,
        reflection_lm: dspy.LM,
        optimizer_factory: OptimizerFactory = dspy.GEPA,
        tariff: CompilerProxyTariff | None = None,
    ) -> None:
        active = load_stable_program_artifact()
        if (
            base_artifact.parent_program_sha256 is not None
            or base_artifact.compile_receipt.accepted_by != "code_owner"
            or base_artifact.program_sha256 != active.program_sha256
            or base_artifact.state_sha256 != active.state_sha256
        ):
            raise ValueError("news_program_compile_parent_must_be_exact_stable_root")
        self._base = base_artifact
        self._eligible_demo_bank = eligible_demo_bank
        self._task_lm = task_lm
        self._reflection_lm = reflection_lm
        self._optimizer_factory = optimizer_factory
        self._tariff = tariff

    def compile(self, request: CompileRequest) -> ProgramCompileResult:
        if request.learning_epoch != LEARNING_EPOCH:
            raise ValueError("news_program_compile_epoch_mismatch")
        failure_clusters, target_dimensions = _failure_scope(request.episodes)
        if not failure_clusters:
            raise ValueError("news_program_compile_no_verified_failure_clusters")
        train_episodes, val_episodes, split_receipt = _honest_split(request.episodes)
        train_examples = [_compile_example(episode) for episode in train_episodes]
        val_examples = [_compile_example(episode) for episode in val_episodes]
        examples = train_examples + val_examples
        retrieval_receipt = _retrieval_receipt(request.episodes)
        meter = _BudgetMeter(request.budget, tariff=self._tariff)
        task_lm = _BudgetedLM(self._task_lm, role="task", meter=meter)
        reflection_lm = _BudgetedLM(self._reflection_lm, role="reflection", meter=meter)
        student = _FeedbackCompileProgram(self._base)
        if tuple(name for name, _ in student.named_predictors()) != ("event_semantics", "reader_card"):
            raise ValueError("news_program_compile_factory_topology_mismatch")

        proposer = RulePackAwareProposer(self._base)
        metric_receipt = _metric_receipt(accepted_review_metric, review_rubric_version=request.review_rubric_version)
        metric_sha = canonical_sha(metric_receipt)
        optimizer_constructor = {
            "auto": None,
            "max_full_evals": None,
            "max_metric_calls": request.budget.max_metric_calls,
            "reflection_minibatch_size": min(3, len(train_examples)),
            "candidate_selection_strategy": "pareto",
            "skip_perfect_score": True,
            "add_format_failure_as_feedback": True,
            # #143. The default proposer shows the reflection model only the mutable component, which for this
            # Program is an advisory slot whose code-owned baseline is empty. It was being asked to write a
            # whole instruction while blind to the eight RulePacks already in the prompt.
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
            "seed": request.budget.seed,
            "gepa_kwargs": None,
        }
        optimizer_config_receipt = _optimizer_config_receipt(
            constructor=optimizer_constructor,
            task_lm=self._task_lm,
            reflection_lm=self._reflection_lm,
            optimizer_factory=self._optimizer_factory,
            metric_sha256=metric_sha,
            example_count=len(examples),
            train_count=len(train_examples),
            val_count=len(val_examples),
        )
        optimizer = self._optimizer_factory(
            accepted_review_metric,
            reflection_lm=reflection_lm,
            **optimizer_constructor,
        )
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
        if metric_calls < 0 or metric_calls > request.budget.max_metric_calls:
            raise ValueError("news_program_compile_metric_budget_unverifiable")
        trajectory_receipt = _trajectory_receipt(details)
        checkpoint_receipt = _checkpoint_receipt(compiled)
        patch = extract_optimizer_patch(
            compiled,
            self._base,
            self._eligible_demo_bank,
        )
        if (
            tuple(strategy.text for strategy in patch.learned_strategies)
            == tuple(strategy.text for strategy in self._base.learned_strategies)
            and patch.demo_refs == self._base.demo_bank.refs
        ):
            raise ValueError("news_program_compile_no_program_change")
        receipt_payloads = CompileReceiptPayloads(
            metric=metric_receipt,
            optimizer_config=optimizer_config_receipt,
            trajectory=trajectory_receipt,
            checkpoint=checkpoint_receipt,
            split=split_receipt,
            retrieval=retrieval_receipt,
        )
        return ProgramCompileResult(
            patch=patch,
            receipt_payloads=receipt_payloads,
            failure_cluster_ids=failure_clusters,
            target_dimensions=target_dimensions,
            metric_calls=metric_calls,
            task_model_calls=meter.task_model_calls,
            reflection_model_calls=meter.reflection_model_calls,
            actual_cost_microusd=meter.actual_cost_microusd,
        )


def _failure_scope(episodes: Sequence[DevelopmentEpisode]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    clusters: set[str] = set()
    dimensions: set[str] = set()
    for episode in episodes:
        review = episode.accepted_review
        results = dict(review.get("dimensions") or {})
        failed = {str(key) for key, value in results.items() if value == "fail"}
        production = dict(episode.production_verdict or {})
        should_push = str(review.get("should_push") or "uncertain")
        production_pushes = str(production.get("decision") or "") in {"push", "escalate"}
        decision_failed = (should_push in {"must_push", "should_push"} and not production_pushes) or (
            should_push in {"must_hold", "should_hold"} and production_pushes
        )
        novelty = str(dict(review.get("novelty") or {}).get("judgment") or "uncertain")
        novelty_failed = novelty != "uncertain" and novelty != str(production.get("novelty") or "")
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


def _trajectory_receipt(details: Any) -> dict[str, Any]:
    if details is None:
        raise ValueError("news_program_compile_trajectory_missing")
    scores = [float(value) for value in list(getattr(details, "val_aggregate_scores", ()) or ())]
    if any(not math.isfinite(score) for score in scores):
        raise TypeError("news_program_compile_nonfinite_receipt_value")
    parents = _json_safe(list(getattr(details, "parents", ()) or ()))
    discovery = [int(value) for value in list(getattr(details, "discovery_eval_counts", ()) or ())]
    return {
        "schema": "tracefold.news.compile_trajectory_receipt.v1",
        "parents": parents,
        "val_aggregate_scores": scores,
        "discovery_eval_counts": discovery,
        "total_metric_calls": int(getattr(details, "total_metric_calls", -1)),
        "num_full_val_evals": int(getattr(details, "num_full_val_evals", 0) or 0),
        "seed": int(getattr(details, "seed", 0) or 0),
        "best_idx": int(getattr(details, "best_idx", 0) or 0),
    }


def _checkpoint_receipt(program: DspyCompileProgram) -> dict[str, Any]:
    predictors: dict[str, Any] = {}
    for name, predictor in program.named_predictors():
        predictors[name] = {
            "instruction_sha256": canonical_sha(str(predictor.signature.instructions)),
            "demos_sha256": canonical_sha([_json_safe(demo.toDict()) for demo in predictor.demos]),
        }
    return {
        "schema": "tracefold.news.compile_checkpoint_receipt.v1",
        "factory": program.artifact.factory_id,
        "predictors": predictors,
    }


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
