"""Bounded cold compiler for News semantic Program candidates.

The compiler accepts only an
already-frozen ``program_v3`` development corpus, runs the fixed two-Predictor
factory through a bounded GEPA optimizer, and emits a state-only candidate
image plus a proposal input.  It cannot read a holdout, register a candidate,
promote a Program, or change Python topology.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_json, canonical_sha
from .semantic_program import (
    CompileProvenance,
    DspyCompileProgram,
    DspyStrictJSONAdapter,
    ExactMetadataDspyLM,
    ExactProviderCallCapture,
    ExactProviderMetadata,
    PredictorAdapterError,
    ProgramArtifact,
    ProgramArtifactCodec,
    RuntimeModelIdentity,
    TriageContext,
)

LEARNING_EPOCH = "program_v3"
COMPILER_ID = "tracefold.news.dspy_gepa_compiler_v1"
METRIC_ID = "tracefold.news.accepted_review_feedback_v1"
_PROPOSAL_GUARDRAILS = (
    "fixed_factory_v1",
    "development_only",
    "holdout_unseen",
    "no_dynamic_code",
    "no_auto_promotion",
)


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompileBudget(_ExactModel):
    """Three independent operator-owned limits for one cold compile."""

    max_metric_calls: int = Field(gt=0)
    max_task_model_calls: int = Field(gt=0)
    max_cost_microusd: int = Field(gt=0)
    seed: int = Field(ge=0)


class DevelopmentEpisode(_ExactModel):
    """The compiler-visible projection of one accepted development case."""

    case_id: str = Field(min_length=1)
    cluster_id: str = Field(min_length=1)
    stratum: str = Field(min_length=1)
    context: TriageContext
    accepted_review: dict[str, Any]
    production_verdict: dict[str, Any] | None = None


class CompileRequest(_ExactModel):
    development_dataset_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    learning_epoch: Literal["program_v3"] = "program_v3"
    episodes: tuple[DevelopmentEpisode, ...] = Field(min_length=1)
    budget: CompileBudget


class CompileReceiptPayloads(_ExactModel):
    """Canonical, secret-free evidence behind every compile provenance hash."""

    metric: dict[str, Any]
    optimizer_config: dict[str, Any]
    trajectory: dict[str, Any]
    checkpoint: dict[str, Any]

    @model_validator(mode="after")
    def _all_payloads_are_finite_json(self) -> CompileReceiptPayloads:
        for payload in (self.metric, self.optimizer_config, self.trajectory, self.checkpoint):
            _json_safe(payload)
        return self


class ProgramCompileResult(_ExactModel):
    artifact: ProgramArtifact
    artifact_directory: str
    compile_provenance: CompileProvenance
    compile_provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_payloads: CompileReceiptPayloads
    machine_diff: dict[str, Any]
    proposal_input: dict[str, Any]

    @model_validator(mode="after")
    def _provenance_identity_matches(self) -> ProgramCompileResult:
        if self.compile_provenance_sha256 != canonical_sha(self.compile_provenance.model_dump(mode="json")):
            raise ValueError("news_program_compile_provenance_hash_mismatch")
        hashes = {
            "metric": self.compile_provenance.metric_sha256,
            "optimizer_config": self.compile_provenance.optimizer_config_sha256,
            "trajectory": self.compile_provenance.trajectory_sha256,
            "checkpoint": self.compile_provenance.checkpoint_sha256,
        }
        payloads = self.receipt_payloads.model_dump(mode="json")
        for name, expected in hashes.items():
            if expected != canonical_sha(payloads[name]):
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
    def __init__(self, budget: CompileBudget) -> None:
        self.budget = budget
        self.task_model_calls = 0
        self.reflection_model_calls = 0
        self.actual_cost_microusd = 0

    @property
    def total_model_calls(self) -> int:
        return self.task_model_calls + self.reflection_model_calls

    def before(self, role: Literal["task", "reflection"]) -> None:
        if self.total_model_calls >= self.budget.max_task_model_calls:
            raise CompileBudgetExceeded("news_program_compile_task_model_call_budget_exhausted")
        if self.actual_cost_microusd >= self.budget.max_cost_microusd:
            raise CompileBudgetExceeded("news_program_compile_cost_budget_exhausted")
        if role == "task":
            self.task_model_calls += 1
        else:
            self.reflection_model_calls += 1

    def after(self, metadata: ExactProviderMetadata) -> None:
        if metadata.provider_cost_microusd is None:
            raise CompileBudgetExceeded("news_program_compile_provider_cost_unavailable")
        self.actual_cost_microusd += metadata.provider_cost_microusd
        if self.actual_cost_microusd > self.budget.max_cost_microusd:
            raise CompileBudgetExceeded("news_program_compile_cost_budget_exceeded")


class _BudgetedLM:
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

    def __getattr__(self, name: str) -> Any:
        return getattr(self._lm, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self._meter.before(self._role)
        with self._lm.observe_exact_call() as capture:
            try:
                output = self._lm(*args, **kwargs)
            except BaseException:
                self._meter.after(_require_compile_metadata(capture))
                raise
        self._meter.after(_require_compile_metadata(capture))
        return output

    async def acall(self, *args: Any, **kwargs: Any) -> Any:
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


def build_compile_lm(
    *,
    model_name: str,
    api_key: str,
    api_base: str,
    timeout: float,
    max_tokens: int,
    model_kwargs: Mapping[str, Any] | None = None,
) -> dspy.LM:
    """Build the cold compiler LM while keeping DSPy out of the CLI layer."""

    extras = dict(model_kwargs or {})
    owned = {"api_key", "api_base", "base_url", "cache", "num_retries", "temperature", "max_tokens", "timeout"}
    overlap = owned.intersection(extras)
    if overlap:
        raise ValueError(f"news_program_compile_model_kwargs_owned:{','.join(sorted(overlap))}")
    return ExactMetadataDspyLM(
        str(model_name),
        api_key=str(api_key),
        api_base=str(api_base),
        timeout=float(timeout),
        max_tokens=int(max_tokens),
        temperature=0,
        cache=False,
        num_retries=0,
        **extras,
    )


def accepted_review_metric(
    gold: dspy.Example,
    pred: dspy.Prediction,
    trace: Any,
    pred_name: str | None,
    pred_trace: Any,
    program_trace: Any = None,
) -> dspy.Prediction:
    """Conservative feedback metric over accepted development truth only.

    Failed rubric dimensions provide reflection feedback, while fields the
    reviewer accepted act as retention anchors.  This metric proposes a
    candidate; it is intentionally not a release decision and never sees the
    future holdout.
    """

    del trace, pred_trace, program_trace
    review = dict(gold.get("accepted_review") or {})
    production = dict(gold.get("production_verdict") or {})
    try:
        verdict_value = pred.get("verdict")
        verdict = (
            verdict_value.model_dump(mode="json") if isinstance(verdict_value, BaseModel) else dict(verdict_value or {})
        )
        if not verdict:
            raise ValueError("verdict_missing")
    except Exception:
        return dspy.Prediction(score=0.0, feedback="Return one complete, schema-valid semantic verdict and card.")

    checks: list[bool] = []
    feedback: list[str] = []
    should_push = str(review.get("should_push") or "uncertain")
    if should_push in {"must_push", "should_push"}:
        checks.append(str(verdict.get("decision")) in {"push", "escalate"})
        if not checks[-1]:
            feedback.append("Accepted review says this fact should reach the reader.")
    elif should_push in {"must_hold", "should_hold"}:
        checks.append(str(verdict.get("decision")) == "drop")
        if not checks[-1]:
            feedback.append("Accepted review says this fact should be held.")

    novelty = dict(review.get("novelty") or {})
    expected_novelty = str(novelty.get("judgment") or "uncertain")
    if expected_novelty != "uncertain":
        checks.append(str(verdict.get("novelty")) == expected_novelty)
        if not checks[-1]:
            feedback.append(f"Accepted novelty is {expected_novelty}.")

    dimensions = dict(review.get("dimensions") or {})
    retention_fields = {
        "asset_grounding": "assets",
        "direction": "direction",
        "magnitude": "magnitude",
        "headline_fidelity": "headline_zh",
        "why_support": "why_zh",
    }
    for dimension, field in retention_fields.items():
        if dimensions.get(dimension) == "pass" and field in production:
            checks.append(verdict.get(field) == production.get(field))
            if not checks[-1]:
                feedback.append(f"Preserve the accepted {dimension} behavior from the prior verdict.")

    failed = sorted(key for key, value in dimensions.items() if value == "fail")
    if failed:
        focus = f" for predictor {pred_name}" if pred_name else ""
        feedback.append(f"Repair accepted failed dimensions{focus}: {', '.join(failed)}.")
        failed_fields = {
            "asset_grounding": "assets",
            "direction": "direction",
            "magnitude": "magnitude",
            "headline_fidelity": "headline_zh",
            "why_support": "why_zh",
            "why_value": "why_zh",
            "novelty": "novelty",
        }
        for dimension in failed:
            failed_field = failed_fields.get(dimension)
            if failed_field is not None and failed_field in production:
                # The accepted review proves the prior value is wrong. A
                # changed value is only a proposal signal; future blind review
                # still decides whether that change is actually better.
                checks.append(verdict.get(failed_field) != production.get(failed_field))
        if "factual_fidelity" in failed and production:
            checks.append(verdict != production)
    correction = str(review.get("expected_correction") or "").strip()
    if correction:
        feedback.append(f"Reviewer correction: {correction}")
    if not checks:
        checks.append(bool(verdict))
    score = sum(checks) / len(checks)
    return dspy.Prediction(
        score=round(score, 6),
        feedback=" ".join(feedback) or "Retain the accepted behavior while making the output more precise.",
    )


def _metric_receipt(metric: Callable[..., Any]) -> dict[str, Any]:
    try:
        source = inspect.getsource(metric).replace("\r\n", "\n")
    except (OSError, TypeError) as exc:
        raise ValueError("news_program_compile_metric_source_unavailable") from exc
    return {
        "schema": "tracefold.news.compile_metric_receipt.v1",
        "metric_id": METRIC_ID,
        "implementation": {
            "module": str(metric.__module__),
            "qualname": str(metric.__qualname__),
            "source": source,
        },
        "dspy_version": importlib.metadata.version("dspy"),
    }


def _compiler_model_identity(lm: dspy.LM) -> dict[str, Any]:
    model = str(getattr(lm, "model", "")).strip()
    if not model:
        raise ValueError("news_program_compile_model_identity_unavailable")
    provider = model.split("/", maxsplit=1)[0] if "/" in model else "unknown"
    return RuntimeModelIdentity.issue(provider=provider, model=model).model_dump(mode="json")


def _optimizer_config_receipt(
    *,
    constructor: Mapping[str, Any],
    task_lm: dspy.LM,
    reflection_lm: dspy.LM,
    optimizer_factory: OptimizerFactory,
    metric_sha256: str,
    example_count: int,
) -> dict[str, Any]:
    return {
        "schema": "tracefold.news.compile_optimizer_config_receipt.v1",
        "optimizer": {
            "implementation": f"{optimizer_factory.__module__}.{optimizer_factory.__qualname__}",
            "dspy_version": importlib.metadata.version("dspy"),
            "gepa_version": importlib.metadata.version("gepa"),
        },
        "metric_sha256": metric_sha256,
        "constructor_scalar_arguments": _json_safe(dict(constructor)),
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
            "trainset_count": example_count,
            "valset_count": example_count,
            "valset_identity": "same_object_as_trainset",
        },
    }


class ProgramCompiler:
    """Bounded cold compiler for the fixed v1 semantic Program factory."""

    def __init__(
        self,
        *,
        base_artifact: ProgramArtifact,
        task_lm: dspy.LM,
        reflection_lm: dspy.LM,
        optimizer_factory: OptimizerFactory = dspy.GEPA,
    ) -> None:
        self._base = base_artifact
        self._task_lm = task_lm
        self._reflection_lm = reflection_lm
        self._optimizer_factory = optimizer_factory

    def compile(self, request: CompileRequest, *, artifact_root: Path) -> ProgramCompileResult:
        if request.learning_epoch != LEARNING_EPOCH:
            raise ValueError("news_program_compile_epoch_mismatch")
        failure_clusters, target_dimensions = _failure_scope(request.episodes)
        if not failure_clusters:
            raise ValueError("news_program_compile_no_verified_failure_clusters")
        examples = [_compile_example(episode) for episode in request.episodes]
        meter = _BudgetMeter(request.budget)
        task_lm = _BudgetedLM(self._task_lm, role="task", meter=meter)
        reflection_lm = _BudgetedLM(self._reflection_lm, role="reflection", meter=meter)
        student = DspyCompileProgram(self._base)
        if tuple(name for name, _ in student.named_predictors()) != ("event_semantics", "reader_card"):
            raise ValueError("news_program_compile_factory_topology_mismatch")

        metric_receipt = _metric_receipt(accepted_review_metric)
        metric_sha = canonical_sha(metric_receipt)
        optimizer_constructor = {
            "auto": None,
            "max_full_evals": None,
            "max_metric_calls": request.budget.max_metric_calls,
            "reflection_minibatch_size": min(3, len(examples)),
            "candidate_selection_strategy": "pareto",
            "skip_perfect_score": True,
            "add_format_failure_as_feedback": True,
            "instruction_proposer": None,
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
        )
        optimizer_config_sha = canonical_sha(optimizer_config_receipt)
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
            compiled = optimizer.compile(student, trainset=examples, teacher=None, valset=examples)
        if not isinstance(compiled, DspyCompileProgram):
            raise TypeError("news_program_compile_result_type_invalid")
        details = getattr(compiled, "detailed_results", None)
        metric_calls = int(getattr(details, "total_metric_calls", -1))
        if metric_calls < 0 or metric_calls > request.budget.max_metric_calls:
            raise ValueError("news_program_compile_metric_budget_unverifiable")
        trajectory_receipt = _trajectory_receipt(details)
        checkpoint_receipt = _checkpoint_receipt(compiled)
        trajectory_sha = canonical_sha(trajectory_receipt)
        checkpoint_sha = canonical_sha(checkpoint_receipt)
        provenance = CompileProvenance(
            mode="optimizer_candidate",
            development_dataset_sha=request.development_dataset_sha,
            learning_epoch=request.learning_epoch,
            optimizer="dspy.GEPA@3.3.0/gepa@0.1.1",
            dspy_version=importlib.metadata.version("dspy"),
            gepa_version=importlib.metadata.version("gepa"),
            metric_sha256=metric_sha,
            optimizer_config_sha256=optimizer_config_sha,
            seed=request.budget.seed,
            max_metric_calls=request.budget.max_metric_calls,
            max_task_model_calls=request.budget.max_task_model_calls,
            max_cost_microusd=request.budget.max_cost_microusd,
            metric_calls=metric_calls,
            task_model_calls=meter.task_model_calls,
            reflection_model_calls=meter.reflection_model_calls,
            actual_cost_microusd=meter.actual_cost_microusd,
            trajectory_sha256=trajectory_sha,
            checkpoint_sha256=checkpoint_sha,
            holdout_access_attestation=False,
        )
        artifact = ProgramArtifactCodec.from_compiled_module(
            compiled,
            base_artifact=self._base,
            compiler=COMPILER_ID,
            source=request.development_dataset_sha,
            compile_provenance=provenance,
        )
        if artifact.program_sha256 == self._base.program_sha256:
            raise ValueError("news_program_compile_no_program_change")
        artifact_directory = _write_program_artifact(artifact, root=artifact_root)
        machine_diff = program_machine_diff(self._base, artifact)
        provenance_payload = provenance.model_dump(mode="json")
        receipt_payloads = CompileReceiptPayloads(
            metric=metric_receipt,
            optimizer_config=optimizer_config_receipt,
            trajectory=trajectory_receipt,
            checkpoint=checkpoint_receipt,
        )
        receipt_payload = receipt_payloads.model_dump(mode="json")
        patch_payload = {
            "parent_program_sha256": self._base.program_sha256,
            "candidate_program_sha256": artifact.program_sha256,
            "machine_diff": machine_diff,
            "compile_provenance": provenance_payload,
        }
        model_identity_sha = canonical_sha(
            {
                "task": str(getattr(self._task_lm, "model", "unknown")),
                "reflection": str(getattr(self._reflection_lm, "model", "unknown")),
            }
        )
        proposal_input = {
            "target": "program",
            "hypothesis": "Bounded GEPA improves accepted development failures while retaining reviewed passes.",
            "target_dimensions": list(target_dimensions),
            "failure_cluster_ids": list(failure_clusters),
            "guardrails": list(_PROPOSAL_GUARDRAILS),
            "generator_kind": "model",
            "generator_prompt_sha": metric_sha,
            "generator_model_sha": model_identity_sha,
            "generator_execution_sha": canonical_sha(provenance_payload),
            "program_version": artifact.program_version,
            "program_sha256": artifact.program_sha256,
            "program_parent_sha256": self._base.program_sha256,
            "program_state_sha256": artifact.state_sha256,
            "program_artifact_path": str(artifact_directory),
            "program_machine_diff": machine_diff,
            "compile_provenance": provenance_payload,
            "compile_receipt_payloads": receipt_payload,
            "candidate_patch_sha": canonical_sha(patch_payload),
        }
        return ProgramCompileResult(
            artifact=artifact,
            artifact_directory=str(artifact_directory),
            compile_provenance=provenance,
            compile_provenance_sha256=canonical_sha(provenance_payload),
            receipt_payloads=receipt_payloads,
            machine_diff=machine_diff,
            proposal_input=proposal_input,
        )


def _compile_example(episode: DevelopmentEpisode) -> dspy.Example:
    return dspy.Example(
        evidence_json=canonical_json(episode.context.model_payload()),
        told_count=len(episode.context.told.entries),
        case_id=episode.case_id,
        cluster_id=episode.cluster_id,
        accepted_review=episode.accepted_review,
        production_verdict=episode.production_verdict,
    ).with_inputs("evidence_json", "told_count")


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


def program_machine_diff(parent: ProgramArtifact, candidate: ProgramArtifact) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for name in ("event_semantics", "reader_card"):
        before = getattr(parent, name).model_dump(mode="json")
        after = getattr(candidate, name).model_dump(mode="json")
        if before != after:
            diff[name] = {"before": before, "after": after}
    if not diff:
        raise ValueError("news_program_compile_machine_diff_empty")
    return diff


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("news_program_compile_non_string_json_key")
        return {key: _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError("news_program_compile_nonfinite_receipt_value")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"news_program_compile_non_json_receipt_value:{type(value).__name__}")


def _write_program_artifact(artifact: ProgramArtifact, *, root: Path) -> Path:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / artifact.program_sha256
    manifest, state = ProgramArtifactCodec.encode(artifact)
    if target.exists():
        existing = ProgramArtifactCodec.load(str(target))
        if existing != artifact:
            raise ValueError("news_program_compile_artifact_collision")
        return target
    temporary = root / f".{artifact.program_sha256}.{uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        (temporary / "manifest.json").write_text(manifest, encoding="utf-8")
        (temporary / "state.json").write_text(state, encoding="utf-8")
        ProgramArtifactCodec.decode(manifest, state)
        try:
            temporary.rename(target)
        except FileExistsError:
            existing = ProgramArtifactCodec.load(str(target))
            if existing != artifact:
                raise ValueError("news_program_compile_artifact_collision") from None
    finally:
        if temporary.exists():
            for child in temporary.iterdir():
                child.unlink()
            temporary.rmdir()
    verified = ProgramArtifactCodec.load(str(target))
    if verified != artifact:
        raise ValueError("news_program_compile_artifact_roundtrip_mismatch")
    return target


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
    "program_machine_diff",
]
