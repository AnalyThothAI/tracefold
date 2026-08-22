"""Untrusted, bounded GEPA logic executed only by the compiler container.

The trusted host seals the ``program_v5`` corpus and launches the runner.  This
module has no database, artifact-writer, proposal or promotion authority.  It
can return only ``ProgramPatchV2`` (two LearnedStrategies plus eligible demo
references) and content-addressable optimizer receipt payloads.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_sha
from ..models import TRIAGE_POLICY_VERSION, TriageVerdict, base_symbol
from ..storyline import final_storyline_key
from ..triage_rules import DEFAULT_POLICY, GateFacts, decide, storyline_status
from .program_compiler_security import CompileBudgetV2, CompilerEndpointIdentity
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
    TriageContext,
    extract_optimizer_patch,
    load_stable_program_artifact,
    render_model_evidence_json,
)

LEARNING_EPOCH = "program_v5"
COMPILER_ID = "tracefold.news.dspy_gepa_compiler_v2"
METRIC_ID = "tracefold.news.production_action_feedback_v1"
_PROPOSAL_GUARDRAILS = (
    "fixed_factory_v2",
    "development_only",
    "holdout_unseen",
    "no_dynamic_code",
    "no_auto_promotion",
)


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompileBudget(CompileBudgetV2):
    """Three independent operator-owned limits for one cold compile."""


class DevelopmentEpisode(_ExactModel):
    """The compiler-visible projection of one accepted development case."""

    case_id: str = Field(min_length=1)
    cluster_id: str = Field(min_length=1)
    stratum: str = Field(min_length=1)
    context: TriageContext
    accepted_review: dict[str, Any]
    production_verdict: dict[str, Any] | None = None
    # Frozen Gate facts plus the ordered sent ledger `decide()` reads. Never rendered, never model-visible,
    # and carrying no control state: the metric evaluates editorial judgment, not operator silence.
    policy_metric: dict[str, Any] = Field(default_factory=dict)


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
        if (self.total_model_calls + 1) * self.budget.max_call_cost_microusd > self.budget.max_cost_microusd:
            raise CompileBudgetExceeded("news_program_compile_cost_reservation_exhausted")
        if role == "task":
            self.task_model_calls += 1
        else:
            self.reflection_model_calls += 1

    def after(self, metadata: ExactProviderMetadata) -> None:
        if metadata.provider_cost_microusd is None:
            raise CompileBudgetExceeded("news_program_compile_provider_cost_unavailable")
        if metadata.provider_cost_microusd > self.budget.max_call_cost_microusd:
            raise CompileBudgetExceeded("news_program_compile_call_cost_reservation_exceeded")
        self.actual_cost_microusd += metadata.provider_cost_microusd
        if self.actual_cost_microusd > self.budget.max_cost_microusd:
            raise CompileBudgetExceeded("news_program_compile_cost_budget_exceeded")


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
    lm = ExactMetadataDspyLM(
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
    lm.tracefold_compiler_endpoint_identity = CompilerEndpointIdentity.issue(
        model=str(model_name), api_base=str(api_base)
    )
    return lm


# The three components of the candidate-selection score. Code-owned and content-addressed: they are hashed into
# the metric receipt, so an optimizer run cannot silently reweight what "better" means.
_ACTION_WEIGHT = 0.50
_SEMANTICS_WEIGHT = 0.35
_CARD_WEIGHT = 0.15

# EventSemantics owns interpretation; ReaderCard owns copy. Feedback is routed the same way, so a Predictor is
# never asked to repair a failure it cannot cause.
#
# These are exactly the names `review._DIMENSIONS` accepts. Inventing plausible extras (`novelty`, `event_type`,
# `actionable`) would leave dead entries no reviewer can ever label, and publish them in the metric receipt as
# if they were scored. Novelty is a separate accepted field, not a rubric dimension, and is scored below.
_SEMANTICS_DIMENSIONS = ("asset_grounding", "direction", "magnitude", "timeliness")
_CARD_DIMENSIONS = ("factual_fidelity", "headline_fidelity", "why_support", "why_value")
_DIMENSION_FIELD = {
    "asset_grounding": "assets",
    "direction": "direction",
    "magnitude": "magnitude",
    "headline_fidelity": "headline_zh",
    "why_support": "why_zh",
    "why_value": "why_zh",
}


def _production_action(verdict: TriageVerdict, projection: Mapping[str, Any]) -> str:
    """The action the reader would actually have seen, from the exact frozen production policy.

    ``decide()`` has no operational input to exclude any more: #137 removed the pause/mute plane outright, so
    every path it takes is editorial. That is the property the metric needs — a card withheld because an
    operator silenced a storyline would not be evidence that its editorial judgment was wrong, and teaching
    that into a Prompt would make the Program quieter for reasons that have nothing to do with the news.
    """

    gate = dict(projection.get("gate") or {})
    storyline = dict(projection.get("storyline") or {})
    seen = list(projection.get("seen") or ())
    told = list(projection.get("told") or ())
    grounded = tuple(str(value) for value in gate.get("grounded_assets") or ())
    key = final_storyline_key(
        title=str(storyline.get("title") or ""),
        headline_zh=verdict.headline_zh,
        scope=verdict.scope,
        verdict_primaries=[asset.symbol for asset in verdict.assets if asset.role == "primary"],
        grounded_assets=grounded,
        family=str(storyline.get("family") or "general"),
    )
    decision = decide(
        verdict,
        GateFacts(
            grounded_assets=grounded,
            watchlist_symbols=frozenset(str(value) for value in gate.get("watchlist_symbols") or ()),
            provider_score=gate.get("provider_score"),
            priority=str(gate.get("priority") or "normal"),
            admission=str(gate.get("admission") or "candidate"),
        ),
        storyline_status(key, told=told, seen=seen),
        policy=DEFAULT_POLICY,
    )
    return decision.final


def _labelled(dimensions: Mapping[str, Any], names: Sequence[str]) -> list[tuple[str, str]]:
    """Only dimensions a reviewer actually decided. A missing or ``uncertain`` label leaves the component
    denominator, rather than counting as a pass and diluting the failures next to it."""

    return [(name, str(dimensions[name])) for name in names if str(dimensions.get(name) or "") in {"pass", "fail"}]


def _component(
    dimensions: Mapping[str, Any],
    names: Sequence[str],
    verdict: Mapping[str, Any],
    production: Mapping[str, Any],
) -> float | None:
    """Score one Predictor's accepted dimensions, or ``None`` when the reviewer labelled none of them.

    A ``pass`` is a retention anchor: keep what the reviewer accepted. A ``fail`` is proof the prior value was
    wrong, so changing it scores — whether the change is *better* is for blind review, not for this metric.
    """

    labelled = _labelled(dimensions, names)
    if not labelled:
        return None
    hits = 0
    for name, label in labelled:
        field = _DIMENSION_FIELD.get(name)
        if field is None:
            # `factual_fidelity` and `timeliness` are judgments about the whole card, not one field. A `fail`
            # is repaired by producing a different card; scoring them as an automatic pass would leave GEPA no
            # gradient between a candidate that fixed the fact and one that changed nothing.
            same = bool(production) and verdict == production
        elif field not in production:
            hits += label == "pass"
            continue
        else:
            same = verdict.get(field) == production.get(field)
        hits += same if label == "pass" else not same
    return hits / len(labelled)


def accepted_review_metric(
    gold: dspy.Example,
    pred: dspy.Prediction,
    trace: Any,
    pred_name: str | None,
    pred_trace: Any,
    program_trace: Any = None,
) -> dspy.Prediction:
    """Score the reader-facing action, then the two Predictors, over accepted development truth only.

    The predecessor compared the model's intermediate ``decision`` field and averaged every check flat. Both
    were wrong in the same direction: ``decision`` is an intent that ``decide()`` routinely overrides — a
    grounded restatement drop, a similarity throttle, a contested high-priority rescue, a watchlist rescue —
    so an offline gain could not predict what the reader would see, and a `must_push` miss could be averaged
    away by four retention anchors agreeing.

    Hard gates come first and are not averaged with anything. This metric proposes a candidate; it is never a
    release decision and never sees the future holdout.
    """

    del trace, pred_trace, program_trace
    review = dict(gold.get("accepted_review") or {})
    production = dict(gold.get("production_verdict") or {})
    projection = dict(gold.get("policy_metric") or {})
    try:
        verdict_value = pred.get("verdict")
        verdict = (
            verdict_value.model_dump(mode="json") if isinstance(verdict_value, BaseModel) else dict(verdict_value or {})
        )
        if not verdict:
            raise ValueError("verdict_missing")
        typed = TriageVerdict.model_validate(verdict)
    except Exception:
        return dspy.Prediction(score=0.0, feedback="Return one complete, schema-valid semantic verdict and card.")

    dimensions = dict(review.get("dimensions") or {})
    should_push = str(review.get("should_push") or "uncertain")
    feedback: list[str] = []

    action = _production_action(typed, projection) if projection else str(verdict.get("decision") or "")
    reaches_reader = action in {"push", "escalate"}

    # ---- hard gates: a dangerous miss cannot be averaged away ----
    if should_push == "must_push" and not reaches_reader:
        return dspy.Prediction(
            score=0.0,
            feedback=f"The reader must receive this fact; the production policy resolved it to {action}.",
        )
    if should_push == "must_hold" and reaches_reader:
        return dspy.Prediction(
            score=0.0, feedback=f"The reader must not receive this fact; the production policy resolved it to {action}."
        )
    if dimensions.get("factual_fidelity") == "fail" and production and verdict == production:
        return dspy.Prediction(
            score=0.0, feedback="The accepted review calls this card factually wrong; it is unchanged."
        )
    # Symbol sets, canonicalized on both sides. Gate grounding carries the provider's raw tag (`XYZ-CL`), and
    # a raw `.upper()` comparison would zero a candidate that correctly named `CL`.
    grounded = {base_symbol(str(value)) for value in (projection.get("gate") or {}).get("grounded_assets") or ()}
    ungrounded = sorted(
        asset.symbol
        for asset in typed.assets
        if asset.role == "primary" and grounded and base_symbol(asset.symbol) not in grounded
    )
    if ungrounded:
        return dspy.Prediction(
            score=0.0, feedback=f"Primary assets must be grounded in the evidence; {', '.join(ungrounded)} are not."
        )

    # ---- weighted score over what the reviewer actually labelled ----
    if should_push in {"must_push", "should_push"}:
        action_score: float | None = float(reaches_reader)
        if not reaches_reader:
            feedback.append(f"Accepted review says this fact should reach the reader; policy resolved it to {action}.")
    elif should_push in {"must_hold", "should_hold"}:
        action_score = float(not reaches_reader)
        if reaches_reader:
            feedback.append(f"Accepted review says this fact should be held; policy resolved it to {action}.")
    else:
        action_score = None

    # Novelty is the epoch's whole subject: a candidate that answers `new_fact` for every accepted
    # `restatement` must not score the same as one that gets it right, and on an `uncertain` action label
    # nothing else would notice.
    novelty = dict(review.get("novelty") or {})
    expected_novelty = str(novelty.get("judgment") or "uncertain")
    novelty_score = None if expected_novelty == "uncertain" else float(str(verdict.get("novelty")) == expected_novelty)
    semantics_score = _component(dimensions, _SEMANTICS_DIMENSIONS, verdict, production)
    if novelty_score is not None:
        semantics_score = novelty_score if semantics_score is None else (semantics_score + novelty_score) / 2
    card_score = _component(dimensions, _CARD_DIMENSIONS, verdict, production)
    components = [
        (_ACTION_WEIGHT, action_score),
        (_SEMANTICS_WEIGHT, semantics_score),
        (_CARD_WEIGHT, card_score),
    ]
    present = [(weight, value) for weight, value in components if value is not None]
    score = sum(weight * value for weight, value in present) / sum(weight for weight, _ in present) if present else 0.0

    # ---- per-Predictor feedback: never ask a Predictor to repair what it cannot cause ----
    owned = _SEMANTICS_DIMENSIONS if pred_name == "event_semantics" else _CARD_DIMENSIONS if pred_name else None
    failed = sorted(name for name, label in dimensions.items() if label == "fail" and (owned is None or name in owned))
    if failed:
        feedback.append(f"Repair accepted failed dimensions: {', '.join(failed)}.")
    if (
        expected_novelty != "uncertain"
        and str(verdict.get("novelty")) != expected_novelty
        and owned != _CARD_DIMENSIONS
    ):
        feedback.append(f"Accepted novelty is {expected_novelty}.")
    correction = str(review.get("expected_correction") or "").strip()
    if correction:
        feedback.append(f"Reviewer correction: {correction}")

    return dspy.Prediction(
        score=round(score, 6),
        feedback=" ".join(feedback) or "Retain the accepted behavior while making the output more precise.",
    )


def _metric_receipt(metric: Callable[..., Any], *, review_rubric_version: str) -> dict[str, Any]:
    try:
        source = inspect.getsource(metric).replace("\r\n", "\n")
    except (OSError, TypeError) as exc:
        raise ValueError("news_program_compile_metric_source_unavailable") from exc
    return {
        "schema": "tracefold.news.compile_metric_receipt.v2",
        "metric_id": METRIC_ID,
        "implementation": {
            "module": str(metric.__module__),
            "qualname": str(metric.__qualname__),
            "source": source,
        },
        # What "better" means, pinned. An optimizer run cannot reweight the components, swap the policy it is
        # scored against, or move to a different review rubric without changing this hash.
        "weights": {"final_action": _ACTION_WEIGHT, "event_semantics": _SEMANTICS_WEIGHT, "reader_card": _CARD_WEIGHT},
        "dimensions": {
            "event_semantics": [*_SEMANTICS_DIMENSIONS, "novelty(accepted_field)"],
            "reader_card": list(_CARD_DIMENSIONS),
        },
        "hard_gates": [
            "must_push_miss",
            "must_hold_send",
            "schema_invalid",
            "factual_contradiction_unchanged",
            "ungrounded_primary_asset",
        ],
        "action_source": {
            "policy": "tracefold.news.triage_rules.decide",
            "policy_version": TRIAGE_POLICY_VERSION,
            "policy_values": DEFAULT_POLICY.as_dict(),
            "storyline": "tracefold.news.storyline.final_storyline_key",
            "operational_controls": "none_the_pause_mute_plane_was_removed_in_137",
        },
        "review_rubric_version": review_rubric_version,
        "dspy_version": importlib.metadata.version("dspy"),
    }


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
            "example_count": example_count,
            "trainset_count": train_count,
            "valset_count": val_count,
            "valset_identity": "disjoint_cluster_split",
        },
    }


# GEPA optimizes on one set and picks the winner on another. Handing it the same object for both proved
# nothing about generalization, and the receipt said so in as many words ("same_object_as_trainset").
_TRAIN_SHARE = 0.70
# A split that leaves one side without safety cases, without both action labels, or without novelty cases
# cannot detect the regressions it exists to detect.
_REQUIRED_STRATA = ("safety", "positive_action", "negative_action", "novelty")


def _episode_strata(episode: DevelopmentEpisode) -> frozenset[str]:
    review = dict(episode.accepted_review or {})
    dimensions = dict(review.get("dimensions") or {})
    should_push = str(review.get("should_push") or "uncertain")
    novelty = str((review.get("novelty") or {}).get("judgment") or "uncertain")
    found: set[str] = set()
    if should_push in {"must_push", "must_hold"} or dimensions.get("factual_fidelity") == "fail":
        found.add("safety")
    if should_push in {"must_push", "should_push"}:
        found.add("positive_action")
    if should_push in {"must_hold", "should_hold"}:
        found.add("negative_action")
    if novelty != "uncertain":
        found.add("novelty")
    return frozenset(found)


def _honest_split(
    episodes: Sequence[DevelopmentEpisode],
) -> tuple[list[DevelopmentEpisode], list[DevelopmentEpisode], dict[str, Any]]:
    """Split accepted development into disjoint train / development-selection halves by fact cluster and time.

    A cluster is never split: the same fact appearing on both sides would let GEPA pick a winner using an
    example it just optimized against. Clusters are ordered by their latest Event time and then by the stable
    cluster id — no shuffle, no seed — so the earlier 70% trains and the later 30% selects, which is also the
    only ordering that resembles how the Program meets news.
    """

    latest: dict[str, int] = {}
    members: dict[str, list[DevelopmentEpisode]] = {}
    for episode in episodes:
        cluster = episode.cluster_id
        # The Event's own time, not its position in the export: the receipt has to be reproducible from the
        # sealed data, not from the order it happened to arrive in.
        latest[cluster] = max(latest.get(cluster, 0), episode.context.now_ms)
        members.setdefault(cluster, []).append(episode)
    ordered = sorted(members, key=lambda cluster: (latest[cluster], cluster))
    for cluster_members in members.values():
        cluster_members.sort(key=lambda episode: (episode.context.now_ms, episode.case_id))
    cut = max(1, min(len(ordered) - 1, round(len(ordered) * _TRAIN_SHARE))) if len(ordered) > 1 else 0
    if cut <= 0:
        raise ValueError("news_program_compile_split_requires_two_clusters")
    train_roots, val_roots = ordered[:cut], ordered[cut:]
    train = [episode for cluster in train_roots for episode in members[cluster]]
    val = [episode for cluster in val_roots for episode in members[cluster]]

    coverage: dict[str, dict[str, int]] = {}
    for name, half in (("train", train), ("development_selection", val)):
        seen: dict[str, int] = dict.fromkeys(_REQUIRED_STRATA, 0)
        for episode in half:
            for stratum in _episode_strata(episode):
                seen[stratum] += 1
        missing = sorted(stratum for stratum, count in seen.items() if count == 0)
        if missing:
            raise ValueError(f"news_program_compile_split_coverage_incomplete:{name}:{','.join(missing)}")
        coverage[name] = seen

    train_ids = {episode.case_id for episode in train}
    val_ids = {episode.case_id for episode in val}
    if train_ids & val_ids or set(train_roots) & set(val_roots):
        raise ValueError("news_program_compile_split_not_disjoint")
    receipt = {
        "schema": "tracefold.news.compile_split_receipt.v1",
        "policy": {"share": _TRAIN_SHARE, "unit": "connected_fact_cluster", "order": ["latest_case", "cluster_id"]},
        "train": {
            "cluster_n": len(train_roots),
            "case_n": len(train),
            "cluster_root_sha256": canonical_sha(list(train_roots)),
            "case_root_sha256": canonical_sha(sorted(train_ids)),
            "coverage": coverage["train"],
        },
        "development_selection": {
            "cluster_n": len(val_roots),
            "case_n": len(val),
            "cluster_root_sha256": canonical_sha(list(val_roots)),
            "case_root_sha256": canonical_sha(sorted(val_ids)),
            "coverage": coverage["development_selection"],
        },
        "disjointness": {
            "shared_case_ids": 0,
            "shared_clusters": 0,
            "proof": "cluster is the split unit; a cluster's cases are never divided",
        },
    }
    return train, val, receipt


def _retrieval_receipt(episodes: Sequence[DevelopmentEpisode]) -> dict[str, Any]:
    """Score retrieval on its own. A scalar candidate score must not be able to hide a recall failure:
    "the model called it new" and "the model was never shown the card" are different defects with different
    fixes, and only this separation tells them apart."""

    considered = 0
    recalled = 0
    ranks: list[int] = []
    for episode in episodes:
        review = dict(episode.accepted_review or {})
        novelty = dict(review.get("novelty") or {})
        if str(novelty.get("judgment") or "") != "restatement":
            continue
        target = str(novelty.get("duplicate_of") or "")
        source = {str(row.get("event_id") or "") for row in (episode.policy_metric.get("seen") or ())}
        if not target or target not in source:
            continue  # outside the bounded window: not a retrieval failure
        considered += 1
        hit = next((entry for entry in episode.context.told.entries if entry.event_id == target), None)
        if hit is not None:
            recalled += 1
            ranks.append(hit.i)
    return {
        "schema": "tracefold.news.compile_retrieval_receipt.v1",
        "accepted_restatements_in_window": considered,
        "target_recall_n": recalled,
        "target_recall": round(recalled / considered, 6) if considered else None,
        "selected_ranks": ranks,
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
        meter = _BudgetMeter(request.budget)
        task_lm = _BudgetedLM(self._task_lm, role="task", meter=meter)
        reflection_lm = _BudgetedLM(self._reflection_lm, role="reflection", meter=meter)
        student = DspyCompileProgram(self._base)
        if tuple(name for name, _ in student.named_predictors()) != ("event_semantics", "reader_card"):
            raise ValueError("news_program_compile_factory_topology_mismatch")

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


def _told_rows(context: TriageContext) -> list[dict[str, Any]]:
    """The selected context in the exact order and index the model saw, which is what ``restates`` points at."""

    return [
        {
            "i": entry.i,
            "event_id": entry.event_id,
            "dir": entry.direction,
            "headline_zh": entry.headline_zh,
            "grounded_assets": list(entry.symbols),
        }
        for entry in context.told.entries
    ]


def _compile_example(episode: DevelopmentEpisode) -> dspy.Example:
    return dspy.Example(
        evidence_json=render_model_evidence_json(
            episode.context.event_semantics_payload(), predictor="event_semantics"
        ),
        card_evidence_json=render_model_evidence_json(episode.context.reader_card_payload(), predictor="reader_card"),
        told_count=len(episode.context.told.entries),
        case_id=episode.case_id,
        cluster_id=episode.cluster_id,
        accepted_review=episode.accepted_review,
        production_verdict=episode.production_verdict,
        policy_metric={**episode.policy_metric, "told": _told_rows(episode.context)},
    ).with_inputs("evidence_json", "card_evidence_json", "told_count")


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
