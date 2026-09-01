"""The framework-neutral taxonomy Objective Plan and shared evaluation vocabulary.

Issue #456 makes the optimizer population exact: targets are recorded Stable taxonomy mismatches with an
explicit ``first_bad_owner=taxonomy``; controls are recorded Stable answers that exactly match accepted
taxonomy Gold and have no explicit owner; every other case is excluded. Readiness, GEPA, candidate
registration and release evaluation re-derive that same plan from the same frozen episodes.

The older composite case metric still imports its dimension and policy helpers from this module for release
evaluation. Those helpers do not participate in target selection or GEPA scoring.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_sha
from ..events.storyline import final_storyline_key
from ..models import base_symbol
from ..program.contracts import ScoredJudgment, TriageContext
from ..taxonomy import ModelTaxonomyV1
from ..triage_rules import DecidePolicy, DecisionResult, GateFacts, decide, storyline_status
from .card_lint import lint_reader_card
from .profile import development_coverage_blockers
from .taxonomy_metric import TAXONOMY_TARGET_DIMENSIONS, compare_taxonomy, summarize_taxonomy


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DevelopmentEpisode(_ExactModel):
    """The compiler-visible projection of one accepted development case."""

    case_id: str = Field(min_length=1)
    cluster_id: str = Field(min_length=1)
    stratum: str = Field(min_length=1)
    context: TriageContext
    accepted_review: dict[str, Any]
    production_judgment: ScoredJudgment | None = None
    # Frozen Gate facts plus the ordered sent ledger `decide()` reads. Never rendered, never model-visible,
    # and carrying no control state: the metric evaluates editorial judgment, not operator silence.
    policy_metric: dict[str, Any] = Field(default_factory=dict)


# Shared by the diagnostic composite metric and release evaluator. They do not define the #456 GEPA
# population, which is taxonomy-only below.
_RELEVANCE_DIMENSIONS = (
    "trade_impact_breadth",
    "trade_tradability",
    "trade_surprise",
    "trade_development_delta",
    "trade_channels",
    "trade_affected_markets",
    "reader_value",
)
_SEMANTICS_DIMENSIONS = ("asset_grounding", "direction", "magnitude")
_CARD_DIMENSIONS = ("factual_fidelity", "headline_fidelity", "why_support", "why_value")
_DELIVERY_DIMENSIONS = ("timeliness",)
_FREE_TEXT_DIMENSIONS = ("headline_fidelity", "why_support", "why_value", "factual_fidelity")
_NO_GOLD: Final = object()
_GOLD_KEY = {
    "direction": "direction",
    "magnitude": "magnitude",
    **{name: name for name in _RELEVANCE_DIMENSIONS},
}


def _frozen_policy(projection: Mapping[str, Any]) -> DecidePolicy:
    """The exact policy the arm ran, carried by the episode — never process-global ambient state.

    Production builds `DecidePolicy(**arm.policy)` from the active `ArmManifest`; this metric used to import
    `DEFAULT_POLICY` and call itself a "production-action metric" anyway. `news.policy` is operator-owned, so
    changing `similarity_max` or `noise_veto_max_magnitude` would have made every offline score silently
    describe a policy production did not run.

    Fails closed. A policy-scored example without a verified policy is not a cheap approximation of the right
    answer; it is a different question, and answering it under the same name is how a receipt starts lying.
    """

    values = projection.get("policy_values")
    if not isinstance(values, Mapping) or not values:
        raise ValueError("news_program_metric_policy_values_missing")
    expected = str(projection.get("policy_sha256") or "")
    if not expected:
        raise ValueError("news_program_metric_policy_sha256_missing")
    if not str(projection.get("policy_version") or ""):
        # The receipt names a version; scoring without one publishes provenance the example never carried.
        raise ValueError("news_program_metric_policy_version_missing")
    actual = canonical_sha(dict(values))
    if actual != expected:
        raise ValueError(f"news_program_metric_policy_sha256_mismatch:{actual[:16]}!={expected[:16]}")
    try:
        return DecidePolicy(**dict(values))
    except Exception as exc:
        raise ValueError("news_program_metric_policy_values_invalid") from exc


def verify_policy_projection(projection: Mapping[str, Any]) -> None:
    """The metric's policy check, callable before any provider call.

    Same function, same error codes — a second implementation would drift from the one that scores. The
    baseline runs this over every case up front so a corrupt corpus costs nothing: it is a pure function of
    the input, and discovering it after two Predictor calls turns "the policy is unverifiable" into "the
    Program did not answer".
    """

    _frozen_policy(projection)


def production_decision(judgment: ScoredJudgment, projection: Mapping[str, Any]) -> DecisionResult:
    """The complete action the reader saw under the exact frozen production policy.

    ``decide()`` has no operational input to exclude any more: #137 removed the pause/mute plane outright, so
    every path it takes is editorial. That is the property the metric needs — a card withheld because an
    operator silenced a storyline would not be evidence that its editorial judgment was wrong, and teaching
    that into a Prompt would make the Program quieter for reasons that have nothing to do with the news.

    Recorded mode carries the persisted `DecisionResult` fields; live modes run the same current `decide()`
    function production uses. No caller may replace that action authority with model-owned relevance.
    """

    recorded = projection.get("recorded_decision_result")
    if isinstance(recorded, Mapping):
        final = str(recorded.get("final") or "")
        baseline = str(recorded.get("rule_baseline") or "")
        if final not in {"push", "escalate", "drop", "throttled"} or baseline not in {
            "push",
            "escalate",
            "drop",
            "throttled",
        }:
            raise ValueError("news_program_metric_recorded_decision_invalid")
        return DecisionResult(
            final=final,  # type: ignore[arg-type]
            override_rule=str(recorded.get("override_rule") or "") or None,
            throttled_by=str(recorded.get("throttled_by") or "") or None,
            rule_baseline=baseline,  # type: ignore[arg-type]
            watchlist_hits=tuple(str(v) for v in recorded.get("watchlist_hits") or ()),
            seen_similarity=(
                float(recorded["seen_similarity"]) if recorded.get("seen_similarity") is not None else None
            ),
            seen_against=int(recorded["seen_against"]) if recorded.get("seen_against") is not None else -1,
            seen_scope=str(recorded.get("seen_scope") or ""),
        )
    policy = _frozen_policy(projection)
    gate = dict(projection.get("gate") or {})
    storyline = dict(projection.get("storyline") or {})
    seen = list(projection.get("seen") or ())
    told = list(projection.get("told") or ())
    grounded = tuple(str(value) for value in gate.get("grounded_assets") or ())
    key = final_storyline_key(
        title=str(storyline.get("title") or ""),
        headline_zh=judgment.verdict.headline_zh,
        scope=judgment.verdict.scope,
        verdict_primaries=[asset.symbol for asset in judgment.verdict.assets if asset.role == "primary"],
        grounded_assets=grounded,
        dedupe_family=str(storyline.get("dedupe_family") or "general"),
    )
    return decide(
        judgment,
        GateFacts(
            grounded_assets=grounded,
            watchlist_symbols=frozenset(str(value) for value in gate.get("watchlist_symbols") or ()),
            admission=str(gate.get("admission") or "candidate"),
            # #154: `news learning baseline` scores the production action, so this metric has to be able to
            # reach `stale_source_artifact` too.
            source_age_s=gate.get("source_age_s"),
        ),
        storyline_status(key, told=told, seen=seen),
        policy=policy,
    )


def _labelled(dimensions: Mapping[str, Any], names: Sequence[str]) -> list[tuple[str, str]]:
    """Only dimensions a reviewer actually decided. A missing or ``uncertain`` label leaves the component
    denominator, rather than counting as a pass and diluting the failures next to it."""

    return [(name, str(dimensions[name])) for name in names if str(dimensions.get(name) or "") in {"pass", "fail"}]


def _gold_value(expected: Mapping[str, Any], name: str) -> Any:
    """Return one exact accepted correction, or the no-Gold sentinel."""

    if name == "asset_grounding":
        assets = expected.get("assets")
        if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
            return _NO_GOLD
        return frozenset(
            base_symbol(str(dict(asset).get("symbol") or "")) for asset in assets if isinstance(asset, Mapping)
        )
    if name in {"trade_channels", "trade_affected_markets"}:
        value = expected.get(name)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return _NO_GOLD
        return tuple(str(item) for item in value)
    key = _GOLD_KEY.get(name)
    if key is None:
        return _NO_GOLD
    value = expected.get(key, _NO_GOLD)
    return _NO_GOLD if value is None else value


# GEPA optimizes on one set and picks the winner on another. Handing it the same object for both proved
# nothing about generalization, and the receipt said so in as many words ("same_object_as_trainset").
_TRAIN_SHARE = 0.70
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

    A cluster appears exactly once: the same fact appearing twice would both overweight it and let GEPA pick
    a winner using an example it just optimized against. Representatives are ordered by Event time and then
    by the stable cluster id — no shuffle, no seed — so the earlier 70% trains and the later 30% selects,
    which is also the only ordering that resembles how the Program meets news.
    """

    representatives: dict[str, DevelopmentEpisode] = {}
    for episode in episodes:
        cluster = episode.cluster_id
        if cluster in representatives:
            raise ValueError("news_program_compile_split_requires_one_representative_per_cluster")
        representatives[cluster] = episode
    # The Event's own time, not its position in the export: the receipt has to be reproducible from the
    # sealed data, not from the order it happened to arrive in.
    ordered = sorted(representatives, key=lambda cluster: (representatives[cluster].context.now_ms, cluster))
    cut = max(1, min(len(ordered) - 1, round(len(ordered) * _TRAIN_SHARE))) if len(ordered) > 1 else 0
    if cut <= 0:
        raise ValueError("news_program_compile_split_requires_two_clusters")
    train_roots, val_roots = ordered[:cut], ordered[cut:]
    train = [representatives[cluster] for cluster in train_roots]
    val = [representatives[cluster] for cluster in val_roots]

    coverage: dict[str, dict[str, int]] = {}
    for name, half in (("train", train), ("development_selection", val)):
        seen: dict[str, int] = dict.fromkeys(_REQUIRED_STRATA, 0)
        for episode in half:
            for stratum in _episode_strata(episode):
                seen[stratum] += 1
        coverage[name] = seen

    train_ids = {episode.case_id for episode in train}
    val_ids = {episode.case_id for episode in val}
    if train_ids & val_ids or set(train_roots) & set(val_roots):
        raise ValueError("news_program_compile_split_not_disjoint")
    receipt = {
        "schema": "tracefold.news.compile_split_receipt.v3",
        "policy": {
            "share": _TRAIN_SHARE,
            "unit": "connected_fact_cluster_representative",
            "representative_n_per_cluster": 1,
            "representative_order": [
                "target_before_control",
                "target_dimension_n_desc",
                "safety_strength_desc",
                "event_time_desc",
                "case_id",
            ],
            "split_order": ["event_time", "cluster_id"],
        },
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
            "proof": "one elected representative per connected fact cluster; clusters are never divided",
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


# ---------------------------------------------------------------------------
# The Objective Plan: what GEPA is allowed to see, and why
# ---------------------------------------------------------------------------

OBJECTIVE_PLAN_SCHEMA: Literal["tracefold.news.gepa_objective_plan.v3"] = "tracefold.news.gepa_objective_plan.v3"
TAXONOMY_OWNER: Final = "taxonomy"
_PUSH_ACTIONS: Final = frozenset({"push", "escalate"})
_OBJECTIVE_GUARD_ADMISSIONS: Final = frozenset({"listing_deterministic", "telemetry_deterministic"})

Disposition = Literal["target", "control", "excluded"]


class ObjectiveCase(_ExactModel):
    """One case's disposition and the reason for it, bounded and readable.

    Deliberately IDs and strings rather than a hash family (#199 §3.5): the plan is re-derived from the
    frozen dataset by every reader that needs it, so a digest of it would address nothing that is not
    already addressed by the episode projection root and the split roots.
    """

    case_id: str = Field(min_length=1)
    cluster_id: str = Field(min_length=1)
    stratum: str = Field(min_length=1)
    disposition: Disposition
    owner: str = ""
    owner_source: Literal["explicit", "derived", "absent"] = "absent"
    predictors: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    reason: str = ""


class GepaObjectivePlan(_ExactModel):
    """The single answer to "what does GEPA actually eat".

    `optimizer_episodes`, `train_episodes` and `development_selection_episodes` are excluded from the JSON
    dump: they are the episodes themselves, already addressed by `episode_projection_root_sha256` upstream
    and by the split's case roots below. Everything a receipt needs to state is a bounded id or a count.
    """

    schema_version: Literal["tracefold.news.gepa_objective_plan.v3"] = OBJECTIVE_PLAN_SCHEMA
    case_n: int = Field(default=0, ge=0)
    cluster_n: int = Field(default=0, ge=0)
    cases: tuple[ObjectiveCase, ...] = ()
    target_case_ids: tuple[str, ...] = ()
    target_failure_cluster_ids: tuple[str, ...] = ()
    control_case_ids: tuple[str, ...] = ()
    control_cluster_ids: tuple[str, ...] = ()
    excluded_case_ids: tuple[str, ...] = ()
    optimizer_case_ids: tuple[str, ...] = ()
    target_predictors: tuple[str, ...] = ()
    target_dimensions: tuple[str, ...] = ()
    # The owner-blind superset: every cluster an accepted review says is wrong, whoever owns it. It is not
    # what GEPA optimizes — it is what a *policy* candidate may declare, and what readiness reports as the
    # gap between "errors we have" and "errors a Prompt may be asked to fix".
    observed_failure_cluster_ids: tuple[str, ...] = ()
    observed_failure_dimensions: tuple[str, ...] = ()
    owner_distribution: dict[str, Any] = Field(default_factory=dict)
    exclusion_reasons: dict[str, int] = Field(default_factory=dict)
    exact_gold_coverage: dict[str, Any] = Field(default_factory=dict)
    # ReaderCard targets are free text: only the sealed equivalence judge can say whether a rewrite kept
    # what the reviewer accepted. The plan records the requirement rather than reclassifying on it, because
    # a disposition that moved with a runtime flag would make two readers of the same corpus disagree.
    reader_card_targets_require_semantic_judge: bool = False
    split: dict[str, Any] | None = None
    # The exact code `_honest_split` refused with, so `run_gepa` can fail closed with the identical error
    # rather than a translation of it.
    split_error: str = ""
    blocking_reasons: tuple[str, ...] = ()
    optimizer_episodes: tuple[DevelopmentEpisode, ...] = Field(default=(), exclude=True, repr=False)
    train_episodes: tuple[DevelopmentEpisode, ...] = Field(default=(), exclude=True, repr=False)
    development_selection_episodes: tuple[DevelopmentEpisode, ...] = Field(default=(), exclude=True, repr=False)

    @property
    def optimizer_ready(self) -> bool:
        return not self.blocking_reasons


def optimizer_population_identity(plan: GepaObjectivePlan) -> dict[str, Any]:
    """The bounded representative-set identity shared by every Objective Plan consumer."""

    return {
        "optimizer_case_n": len(plan.optimizer_case_ids),
        "optimizer_cluster_n": len({episode.cluster_id for episode in plan.optimizer_episodes}),
        "optimizer_case_root_sha256": canonical_sha(sorted(plan.optimizer_case_ids)),
    }


def _objective_guard(policy_metric: Mapping[str, Any]) -> str:
    """The code-owned guard that suspends action scoring, exactly as ``accepted_review_metric`` reads it."""

    gate_facts = dict(policy_metric.get("gate") or {})
    admission = str(gate_facts.get("admission") or "candidate")
    if admission in _OBJECTIVE_GUARD_ADMISSIONS:
        return admission
    grounded = {base_symbol(str(value)) for value in gate_facts.get("grounded_assets") or ()}
    watchlist = {base_symbol(str(value)) for value in gate_facts.get("watchlist_symbols") or ()}
    return "watchlist" if grounded & watchlist else "none"


def stable_hard_gate(
    episode: DevelopmentEpisode,
    decision: DecisionResult,
    *,
    judge_configured: bool = False,
) -> str:
    """Which hard gate the *stable* arm's own output trips on this case, or ``""``.

    A hard-gated case scores zero however good the rest of it is, so it is never "the Program already
    answers this correctly" and never a control. This mirrors the gate ladder inside
    ``accepted_review_metric`` — the same conditions in the same order — and
    ``test_news_objective_plan.py`` runs the real metric over the same episodes to prove the two agree.
    Mirroring rather than sharing is forced: the ladder is inside the scoring function whose bytes are the
    metric's published identity, and this module may not import the module that holds it.

    ``judge_configured`` only affects ``factual_contradiction``: without a judge the metric cannot verify a
    factual repair and gates unconditionally. A control never carries a failed dimension, so the parameter
    changes nothing for the plan and exists so the parity test can drive both arms of the metric.
    """

    review = dict(episode.accepted_review or {})
    dimensions = {str(key): str(value) for key, value in dict(review.get("dimensions") or {}).items()}
    should_push = str(review.get("should_push") or "uncertain")
    accepted_novelty = str(dict(review.get("novelty") or {}).get("judgment") or "uncertain")
    judgment = episode.production_judgment
    if judgment is None:
        return "schema_invalid"
    guard = _objective_guard(episode.policy_metric)
    reaches_reader = decision.final in _PUSH_ACTIONS
    if should_push == "must_push" and not reaches_reader:
        return "must_push_miss"
    if should_push == "must_hold" and reaches_reader:
        return "must_hold_send"
    if dimensions.get("factual_fidelity") == "fail" and not judge_configured:
        return "factual_contradiction"
    lint = lint_reader_card(
        headline_zh=judgment.verdict.headline_zh,
        why_zh=judgment.verdict.why_zh,
        source_title=episode.context.evidence.title,
    )
    if lint.gate:
        return lint.gate
    grounded = {
        base_symbol(str(value)) for value in dict(episode.policy_metric.get("gate") or {}).get("grounded_assets") or ()
    }
    if grounded and any(
        base_symbol(asset.symbol) not in grounded for asset in judgment.verdict.assets if asset.role == "primary"
    ):
        return "ungrounded_primary_asset"
    relevance = judgment.editorial.relevance
    if (
        guard == "none"
        and relevance is not None
        and str(relevance.reader_value) in {"background", "none"}
        and reaches_reader
    ):
        return "background_realtime_send"
    if decision.override_rule == "trade_relevance_inconsistent":
        return "relevance_inconsistent"
    if accepted_novelty == "restatement" and reaches_reader and guard == "none":
        return "known_duplicate_leak"
    return ""


def _explicit_owner(review: Mapping[str, Any]) -> str:
    """The owner an operator wrote into the submission, never the one ReviewDesk derived for the queue.

    ``news_reviews.first_bad_owner`` is ``submission.first_bad_owner or _derive_owner(submission)`` — the
    column cannot tell the two apart. The submission itself is persisted verbatim in ``payload``, so the
    distinction needs no new table and no new column (#199 §3.1, §12): a null there means nobody claimed
    the Prompt was at fault, whatever the column says.
    """

    return str(review.get("first_bad_owner_explicit") or "")


def _owner_identity(review: Mapping[str, Any]) -> tuple[str, str]:
    explicit = _explicit_owner(review)
    if explicit:
        return explicit, "explicit"
    derived = str(review.get("first_bad_owner") or "")
    return (derived, "derived") if derived else ("", "absent")


def _taxonomy_classify(episode: DevelopmentEpisode) -> tuple[ObjectiveCase, dict[str, Any]]:
    """Classify the only objective #456 authorizes: accepted taxonomy Gold."""

    review = dict(episode.accepted_review or {})
    owner, owner_source = _owner_identity(review)
    taxonomy_mismatch = False

    def result(disposition: Disposition, reason: str) -> tuple[ObjectiveCase, dict[str, Any]]:
        target = disposition == "target"
        case = ObjectiveCase(
            case_id=episode.case_id,
            cluster_id=episode.cluster_id,
            stratum=episode.stratum,
            disposition=disposition,
            owner=owner,
            owner_source=owner_source,
            predictors=("event_semantics",) if target else (),
            dimensions=TAXONOMY_TARGET_DIMENSIONS if target else (),
            reason=reason,
        )
        return case, {
            "disposition": disposition,
            "reason": reason,
            "failed": TAXONOMY_TARGET_DIMENSIONS if target else (),
            "failed_with_exact_gold": TAXONOMY_TARGET_DIMENSIONS if target else (),
            "observed_failure": taxonomy_mismatch,
            "observed_dimensions": TAXONOMY_TARGET_DIMENSIONS if taxonomy_mismatch else (),
        }

    if episode.production_judgment is None:
        return result("excluded", "stable_output_absent")
    try:
        gold = ModelTaxonomyV1.model_validate(review.get("taxonomy"))
    except ValueError:
        return result("excluded", "accepted_taxonomy_gold_invalid")
    predicted = episode.production_judgment.editorial.taxonomy
    if predicted is None:
        return result("excluded", "recorded_stable_taxonomy_absent")

    exact = compare_taxonomy(gold, predicted).exact
    taxonomy_mismatch = not exact
    if not exact:
        if owner_source == "explicit" and owner == TAXONOMY_OWNER:
            return result("target", "explicit_taxonomy_owner_with_exact_mismatch")
        if owner_source == "explicit":
            return result("excluded", f"non_taxonomy_owner:{owner}")
        if owner_source == "derived":
            return result("excluded", f"owner_derived_only:{owner}")
        return result("excluded", "taxonomy_mismatch_without_explicit_owner")
    if owner_source == "explicit":
        return result("excluded", "explicit_owner_on_taxonomy_control")
    return result("control", "stable_taxonomy_exact_under_accepted_gold")


def _split_blockers(split_error: str) -> tuple[str, ...]:
    """Translate `_honest_split`'s refusal into the readiness vocabulary, without re-deciding it."""

    if split_error.startswith("news_program_compile_split_requires_two_clusters"):
        return ("split_requires_two_clusters",)
    marker = "news_program_compile_split_coverage_incomplete:"
    if split_error.startswith(marker):
        half, _, missing = split_error[len(marker) :].partition(":")
        return tuple(f"{half}_{stratum}_missing" for stratum in missing.split(",") if stratum)
    return ("optimizer_split_unavailable",) if split_error else ()


def _representative_order(case: ObjectiveCase, episode: DevelopmentEpisode) -> tuple[Any, ...]:
    """Stable preference for the one optimizer example a connected fact cluster may contribute."""

    strata = _episode_strata(episode)
    target = case.disposition == "target"
    return (
        0 if target else 1,
        -len(case.dimensions) if target else 0,
        -int("safety" in strata),
        -episode.context.now_ms,
        case.case_id,
    )


def _elect_cluster_representatives(
    classified: Sequence[tuple[ObjectiveCase, dict[str, Any]]],
    episodes: Sequence[DevelopmentEpisode],
) -> list[tuple[ObjectiveCase, dict[str, Any]]]:
    """Keep one target/control per connected fact cluster; retain every other case as audit evidence."""

    eligible: dict[str, list[int]] = {}
    for index, ((case, _facts), _episode) in enumerate(zip(classified, episodes, strict=True)):
        if case.disposition in {"target", "control"}:
            eligible.setdefault(case.cluster_id, []).append(index)

    elected = {
        min(indexes, key=lambda index: _representative_order(classified[index][0], episodes[index]))
        for indexes in eligible.values()
    }
    result = list(classified)
    for indexes in eligible.values():
        for index in indexes:
            if index in elected:
                continue
            case, facts = result[index]
            reason = f"cluster_representative_shadowed:{case.disposition}"
            result[index] = (
                case.model_copy(update={"disposition": "excluded", "reason": reason}),
                {**facts, "disposition": "excluded", "reason": reason},
            )
    return result


def build_gepa_objective_plan(episodes: Sequence[DevelopmentEpisode]) -> GepaObjectivePlan:
    """Decide, once, which episodes GEPA may train and select on — and why every other one is out.

    The predecessor asked "does an accepted review say anything is wrong here", took the answer as the
    optimization target, and then handed GEPA *every* episode to split. Both halves were wrong in the same
    direction: a retrieval miss or a Gate suppression became a failure cluster an instruction was told to
    repair, and a case nobody had blamed on anything still reached the reflective minibatch as a low score.
    """

    classified = _elect_cluster_representatives([_taxonomy_classify(episode) for episode in episodes], episodes)
    cases = tuple(case for case, _facts in classified)

    targets = tuple(case for case in cases if case.disposition == "target")
    controls = tuple(case for case in cases if case.disposition == "control")
    excluded = tuple(case for case in cases if case.disposition == "excluded")
    optimizer_ids = {case.case_id for case in (*targets, *controls)}
    optimizer_episodes = tuple(
        sorted(
            (episode for episode in episodes if episode.case_id in optimizer_ids),
            key=lambda episode: (episode.context.now_ms, episode.cluster_id, episode.case_id),
        )
    )

    observed_clusters: set[str] = set()
    observed_dimensions: set[str] = set()
    exclusion_reasons: dict[str, int] = {}
    owner_distribution: dict[str, dict[str, int]] = {"explicit": {}, "derived": {}, "absent": {}}
    gold_failed: dict[str, int] = {}
    gold_with_value: dict[str, int] = {}
    novelty_unverifiable = 0
    non_replayable_target = 0
    for case, facts in classified:
        bucket = owner_distribution[case.owner_source]
        key = case.owner or "none"
        bucket[key] = bucket.get(key, 0) + 1
        if facts.get("observed_failure"):
            observed_clusters.add(case.cluster_id)
            observed_dimensions.update(facts.get("observed_dimensions") or ())
        for name in facts["failed"]:
            gold_failed[name] = gold_failed.get(name, 0) + 1
        for name in facts["failed_with_exact_gold"]:
            gold_with_value[name] = gold_with_value.get(name, 0) + 1
        if case.disposition == "excluded":
            exclusion_reasons[case.reason] = exclusion_reasons.get(case.reason, 0) + 1
            if case.reason == "non_replayable_target":
                non_replayable_target += 1
            if case.reason.startswith("accepted_novelty_target_not_verifiable") or case.reason.startswith(
                "novelty_prior_"
            ):
                novelty_unverifiable += 1

    target_clusters = tuple(sorted({case.cluster_id for case in targets}))
    control_clusters = tuple(sorted({case.cluster_id for case in controls}))
    target_dimensions = tuple(sorted({name for case in targets for name in case.dimensions}))
    target_predictors = tuple(sorted({name for case in targets for name in case.predictors}))

    split: dict[str, Any] | None = None
    split_error = ""
    train: tuple[DevelopmentEpisode, ...] = ()
    selection: tuple[DevelopmentEpisode, ...] = ()
    if targets and controls:
        try:
            train_list, selection_list, receipt = _honest_split(optimizer_episodes)
        except ValueError as exc:
            split_error = str(exc)
        else:
            train, selection, split = tuple(train_list), tuple(selection_list), receipt

    blocking: list[str] = []
    if not target_clusters:
        blocking.append("no_verified_prompt_target_clusters")
    if not control_clusters:
        blocking.append("no_correct_control_clusters")
    blocking.extend(_split_blockers(split_error))
    if split is not None:
        target_case_ids = {case.case_id for case in targets}
        control_case_ids = {case.case_id for case in controls}
        if not any(episode.case_id in target_case_ids for episode in train):
            blocking.append("train_target_missing")
        if not any(episode.case_id in target_case_ids for episode in selection):
            blocking.append("development_selection_target_missing")
        if not any(episode.case_id in control_case_ids for episode in train):
            blocking.append("train_control_missing")
        if not any(episode.case_id in control_case_ids for episode in selection):
            blocking.append("development_selection_control_missing")
    if non_replayable_target:
        blocking.append("non_replayable_target")
    if not target_clusters and novelty_unverifiable:
        blocking.append("accepted_novelty_target_not_verifiable")

    return GepaObjectivePlan(
        case_n=len(cases),
        cluster_n=len({case.cluster_id for case in cases}),
        cases=cases,
        target_case_ids=tuple(case.case_id for case in targets),
        target_failure_cluster_ids=target_clusters,
        control_case_ids=tuple(case.case_id for case in controls),
        control_cluster_ids=control_clusters,
        excluded_case_ids=tuple(case.case_id for case in excluded),
        optimizer_case_ids=tuple(episode.case_id for episode in optimizer_episodes),
        target_predictors=target_predictors,
        target_dimensions=target_dimensions,
        observed_failure_cluster_ids=tuple(sorted(observed_clusters)),
        observed_failure_dimensions=tuple(sorted(observed_dimensions)),
        owner_distribution={
            "explicit": dict(sorted(owner_distribution["explicit"].items())),
            "derived": dict(sorted(owner_distribution["derived"].items())),
            "absent": int(sum(owner_distribution["absent"].values())),
            "explicit_taxonomy_owner_n": int(owner_distribution["explicit"].get(TAXONOMY_OWNER, 0)),
        },
        exclusion_reasons=dict(sorted(exclusion_reasons.items())),
        exact_gold_coverage={
            "failed_by_dimension": dict(sorted(gold_failed.items())),
            "failed_with_exact_gold_by_dimension": dict(sorted(gold_with_value.items())),
            "target_dimension_gold_n": sum(gold_with_value.get(name, 0) for name in TAXONOMY_TARGET_DIMENSIONS),
        },
        reader_card_targets_require_semantic_judge=False,
        split=split,
        split_error=split_error,
        blocking_reasons=tuple(blocking),
        optimizer_episodes=optimizer_episodes,
        train_episodes=train,
        development_selection_episodes=selection,
    )


def _expected_delivery(should_push: str) -> bool | None:
    """Which delivery the accepted action implies, or `None` when the reviewer stated no opinion.

    Read by the release evaluator's own delivery checks too, so it lives here with the rest of the corpus
    vocabulary rather than in the module that happens to have the most callers.
    """

    if should_push in {"must_push", "should_push"}:
        return True
    if should_push in {"must_hold", "should_hold"}:
        return False
    return None


# v2 (#259): the report carries the frozen dataset's own `coverage` counts beside the plan, so one
# document answers both "may this corpus be optimized" and "how much separable evidence is in it".
# A v1 report cannot answer the second question and must not be read as if it could.
READINESS_SCHEMA: Literal["tracefold.news.gepa_readiness_report.v3"] = "tracefold.news.gepa_readiness_report.v3"
# One Predictor evaluation may use JSONAdapter's single format fallback. This is a physical-call ceiling,
# not the usual successful-path count, so the readiness receipt must reserve both attempts.
_TASK_CALLS_PER_METRIC_CALL: Final = 2


def _strata_coverage(episodes: Sequence[DevelopmentEpisode]) -> dict[str, int]:
    counts = dict.fromkeys(_REQUIRED_STRATA, 0)
    for episode in episodes:
        for stratum in _episode_strata(episode):
            counts[stratum] += 1
    return counts


def _half_counts(plan: GepaObjectivePlan, half: Sequence[DevelopmentEpisode]) -> dict[str, Any]:
    targets = set(plan.target_case_ids)
    controls = set(plan.control_case_ids)
    taxonomy_targets = {
        case.case_id
        for case in plan.cases
        if case.disposition == "target" and set(case.dimensions) & set(TAXONOMY_TARGET_DIMENSIONS)
    }
    return {
        "case_n": len(half),
        "cluster_n": len({episode.cluster_id for episode in half}),
        "target_case_n": sum(1 for episode in half if episode.case_id in targets),
        "target_cluster_n": len({episode.cluster_id for episode in half if episode.case_id in targets}),
        "taxonomy_target_case_n": sum(1 for episode in half if episode.case_id in taxonomy_targets),
        "taxonomy_target_cluster_n": len(
            {episode.cluster_id for episode in half if episode.case_id in taxonomy_targets}
        ),
        "control_case_n": sum(1 for episode in half if episode.case_id in controls),
        "control_cluster_n": len({episode.cluster_id for episode in half if episode.case_id in controls}),
        "taxonomy_control_case_n": sum(1 for episode in half if episode.case_id in controls),
        "taxonomy_control_cluster_n": len({episode.cluster_id for episode in half if episode.case_id in controls}),
        "strata": _strata_coverage(half),
    }


def development_split_profile_counts(plan: GepaObjectivePlan) -> dict[str, int]:
    """Project the two sealed Objective halves into the shared release-profile vocabulary."""

    return {
        "train_stratum_n": sum(value > 0 for value in _strata_coverage(plan.train_episodes).values()),
        "development_selection_stratum_n": sum(
            value > 0 for value in _strata_coverage(plan.development_selection_episodes).values()
        ),
    }


def build_readiness_report(
    plan: GepaObjectivePlan,
    *,
    episodes: Sequence[DevelopmentEpisode],
    identity: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    """Explain a compile before anyone pays for one. No model call, no write, no second projection.

    Readiness is an explanation, not a bypass: the trusted compiler rebuilds this exact plan and refuses
    on the same conditions. What it buys is that `insufficient` costs nothing instead of costing a
    container, two endpoints and an operator's evening.

    `coverage` is the frozen dataset's own sealed counts, handed in by the caller that loaded them and
    republished verbatim. This module decides what GEPA may optimize and never reads a dataset; carrying
    the block is what lets one report answer both "may this corpus be optimized" and "how much separable
    evidence is in it, and how concentrated is it in time" (#259 §5.2).
    """

    train = _half_counts(plan, plan.train_episodes)
    selection = _half_counts(plan, plan.development_selection_episodes)
    profile_counts = {
        **dict(coverage),
        **development_split_profile_counts(plan),
        "train_taxonomy_target_cluster_n": train["taxonomy_target_cluster_n"],
        "train_taxonomy_control_cluster_n": train["taxonomy_control_cluster_n"],
        "development_selection_taxonomy_target_cluster_n": selection["taxonomy_target_cluster_n"],
        "development_selection_taxonomy_control_cluster_n": selection["taxonomy_control_cluster_n"],
    }
    profile_blockers = development_coverage_blockers(profile_counts)
    gold_rows: list[dict[str, Any]] = []
    for episode in episodes:
        try:
            gold = ModelTaxonomyV1.model_validate(dict(episode.accepted_review or {}).get("taxonomy"))
        except ValueError:
            continue
        predicted = episode.production_judgment.editorial.taxonomy if episode.production_judgment else None
        if predicted is None:
            continue
        gold_rows.append(
            {
                "case_id": episode.case_id,
                "cluster_id": episode.cluster_id,
                "gold": gold,
                "predicted": predicted,
            }
        )
    gold_summary = summarize_taxonomy(gold_rows)
    return {
        "schema": READINESS_SCHEMA,
        "identity": dict(identity),
        # Diagnostics, in the release profile's own vocabulary, and separate from `corpus` below because
        # these are the *dataset's* sealed counts rather than anything re-derived from the episodes.
        # `natural_day_n` and `window_duration_hours` describe sample concentration and gate nothing.
        "coverage": dict(coverage),
        "corpus": {
            "case_n": plan.case_n,
            "cluster_n": plan.cluster_n,
            "observed_failure_cluster_n": len(plan.observed_failure_cluster_ids),
            "observed_failure_dimensions": list(plan.observed_failure_dimensions),
            "strata": _strata_coverage(episodes),
        },
        "owner_distribution": dict(plan.owner_distribution),
        "objective": {
            "schema": plan.schema_version,
            "compilable": plan.optimizer_ready,
            "blockers": list(plan.blocking_reasons),
            "target_case_n": len(plan.target_case_ids),
            "target_cluster_n": len(plan.target_failure_cluster_ids),
            "control_case_n": len(plan.control_case_ids),
            "control_cluster_n": len(plan.control_cluster_ids),
            "excluded_case_n": len(plan.excluded_case_ids),
            **optimizer_population_identity(plan),
            "target_predictors": list(plan.target_predictors),
            "target_dimensions": list(plan.target_dimensions),
            "target_failure_cluster_ids": list(plan.target_failure_cluster_ids),
            "exclusion_reasons": dict(plan.exclusion_reasons),
            "reader_card_targets_require_semantic_judge": plan.reader_card_targets_require_semantic_judge,
        },
        "development_profile": {
            "ready": not profile_blockers,
            "blockers": list(profile_blockers),
            "counts": profile_counts,
        },
        "taxonomy_gold": {
            "cluster_n": gold_summary["cluster_n"],
            "support": gold_summary["support"],
            "zero_support": gold_summary["zero_support"],
        },
        "exact_gold_coverage": dict(plan.exact_gold_coverage),
        "split": plan.split,
        "split_error": plan.split_error,
        "train": train,
        "development_selection": selection,
        # Retrieval is reported on the *whole* corpus, not the optimizer half: "the model called it new" and
        # "the model was never shown the card" are different defects, and the second one is precisely what
        # the objective plan just excluded. Narrowing this to the optimizer corpus would hide it.
        "retrieval": _retrieval_receipt(episodes),
        "call_envelope": {
            "note": "ceilings computed from the corpus; GEPA chooses how many rounds fit in --max-metric-calls",
            "task_model_calls_per_metric_call": _TASK_CALLS_PER_METRIC_CALL,
            "metric_calls_per_full_selection_evaluation": len(plan.development_selection_episodes),
            "metric_calls_per_reflection_minibatch": min(10, len(plan.train_episodes)),
            "task_model_calls_per_full_selection_evaluation": (
                len(plan.development_selection_episodes) * _TASK_CALLS_PER_METRIC_CALL
            ),
            "reflection_model_calls_per_proposal_round": 1,
        },
        "case_dispositions": [case.model_dump(mode="json") for case in plan.cases],
    }


__all__ = [
    "OBJECTIVE_PLAN_SCHEMA",
    "READINESS_SCHEMA",
    "TAXONOMY_OWNER",
    "DevelopmentEpisode",
    "Disposition",
    "GepaObjectivePlan",
    "ObjectiveCase",
    "build_gepa_objective_plan",
    "build_readiness_report",
    "development_split_profile_counts",
    "optimizer_population_identity",
    "production_decision",
    "retrieval_receipt",
    "stable_hard_gate",
    "verify_policy_projection",
]

# The one private helper a caller outside this module reads by name: `baseline.py` publishes the retrieval
# receipt beside its scores. `_honest_split` and `_episode_strata` deliberately stay private — the plan is
# the only thing that should be splitting an optimizer corpus.
retrieval_receipt = _retrieval_receipt
