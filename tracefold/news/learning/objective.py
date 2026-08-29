"""What GEPA is allowed to optimize, decided once, without the optimizer.

Three questions have to be answered before a single provider call is worth making, and until #199 they
were answered in three different places that disagreed:

``target``      the errors GEPA has both the authority and the evidence to repair — an operator wrote
                ``first_bad_owner = triage_prompt`` into the submission itself, the failure belongs to
                EventSemantics or ReaderCard, and the accepted review states something checkable.
``control``     the cases the stable Program already answers correctly, kept so a candidate that breaks
                one is caught. A wrong answer nobody blamed the Prompt for is not a control: it is a
                low-scoring trajectory the reflection model reads and tries to fix by rewriting an
                instruction it cannot fix anything with.
``excluded``    everything else — retrieval, Gate, storyline, policy, delivery, taxonomy, provider
                failures, derived-only owners, failed dimensions with no stated correct value, and copy
                failures the metric has no way to score. They stay visible in readiness and baseline
                diagnostics and never enter a reflective minibatch.

The module is framework-neutral on purpose: no DSPy, no database, no provider, no promotion, no
container. `readiness`, the dataset-bound baseline, `run_gepa`, the experiment loop and
`CandidateEvaluator` all build the same plan from the same episodes, which is the only way the corpus a
release gate re-derives can be the corpus an optimizer actually saw.

It also owns the corpus vocabulary the metric reads — the dimension groups, the exact-gold lookup, the
frozen policy, the production action, the strata and the honest split. Those moved here from
``metric.py`` rather than being copied: the metric turns labels into a number, and this module decides
which labels are in scope, and a rule that answers "is this case in scope" cannot live in a module the
scope decision is not allowed to import.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_sha
from ..events.storyline import final_storyline_key
from ..models import base_symbol
from ..program.contracts import ScoredJudgment, TriageContext
from ..triage_rules import DecidePolicy, DecisionResult, GateFacts, decide, storyline_status
from .card_lint import lint_reader_card


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


# EventSemantics owns interpretation; ReaderCard owns copy. Feedback is routed the same way, so a Predictor is
# never asked to repair a failure it cannot cause.
#
# These are exactly the names `review._DIMENSIONS` accepts. Inventing plausible extras would leave dead entries
# no reviewer can ever label, and publish them in the metric receipt as
# if they were scored. Novelty is a separate accepted field, not a rubric dimension, and is scored below.
# `timeliness` is absent on purpose. The canonical ReviewDesk owner map assigns it to `delivery`, and
# `TriageVerdict` has no timeliness field — so scoring it here fell through to "did the whole verdict change?"
# and handed EventSemantics feedback about delivery latency it cannot repair. It stays in the corpus label
# distribution under its real owner; giving the model a freshness judgment needs a typed output and a gold
# contract of its own, not a borrowed one.
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


# A sentinel, because `None` is a legitimate absence of a reviewer opinion and must not read as "gold = null".
_NO_GOLD: Final = object()
# `news_review_v6` non-taxonomy gold keys, per dimension. Taxonomy has its own evaluator; `why_support`/
# `why_value`/`headline_fidelity`/`factual_fidelity`/
# `timeliness` have no scalar gold. Failed typed fields without exact gold do not enter the score; free-text
# retention and factual support use the sealed judge path instead.
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
    """The reviewer's stated correct value for one dimension, or ``_NO_GOLD``.

    ``asset_grounding`` is the one dimension whose gold is a set rather than a scalar: a reviewer names the
    instruments the card should have been about, and role/order are not what they were asserting.
    """

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


# Dimensions whose accepted value is a whole Chinese sentence, so "did the candidate keep it?" cannot be
# answered by `==`. `timeliness` is not one of them — it is handled separately in `_retains`, and only when a
# judge is present, so that the no-judge arm stays byte-for-byte the pre-#148 rule.
_FREE_TEXT_DIMENSIONS = ("headline_fidelity", "why_support", "why_value", "factual_fidelity")


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
        missing = sorted(stratum for stratum, count in seen.items() if count == 0)
        if missing:
            raise ValueError(f"news_program_compile_split_coverage_incomplete:{name}:{','.join(missing)}")
        coverage[name] = seen

    train_ids = {episode.case_id for episode in train}
    val_ids = {episode.case_id for episode in val}
    if train_ids & val_ids or set(train_roots) & set(val_roots):
        raise ValueError("news_program_compile_split_not_disjoint")
    receipt = {
        "schema": "tracefold.news.compile_split_receipt.v2",
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

OBJECTIVE_PLAN_SCHEMA: Literal["tracefold.news.gepa_objective_plan.v2"] = "tracefold.news.gepa_objective_plan.v2"
# The one owner value that grants GEPA permission, and it has to have been written by a human into the
# submission itself. `review._OWNER_BY_DIMENSION` derives an owner for every failed dimension so the
# ReviewDesk queue can route work; deriving is a hint, not a grant. 53% of the failure cases in the only
# corpus this project has ever collected carry a non-`triage_prompt` owner, and the owner-blind predecessor
# swept every one of them into the failure clusters GEPA was told to repair.
PROMPT_OWNER: Final = "triage_prompt"
# Typed EventSemantics targets: a failed dimension whose accepted repair is one exact value the metric can
# check. `asset_grounding` is absent on purpose — the canonical ReviewDesk owner map calls it a Gate defect,
# so a reviewer who fails it is not blaming the Prompt even when the Prompt is what emitted the symbol.
_EVENT_SEMANTICS_TARGET_DIMENSIONS: Final[tuple[str, ...]] = ("direction", "magnitude", *_RELEVANCE_DIMENSIONS)
# The only ReaderCard failure the metric can measure a repair of. `_GOLD_KEY` holds no copy dimension —
# `ExpectedCorrection` says why in as many words, "the correct Chinese sentence is not a label" — so
# `_component` files a failed `headline_fidelity` / `why_support` / `why_value` as `not_scored_no_gold`
# and drops it from the denominator, and `_retains` only ever runs on a dimension the reviewer *passed*.
# `factual_fidelity` is different: a failed one arms the `factual_contradiction` hard gate, which zeroes
# the case until the judge can verify the candidate's facts against the frozen evidence. That is a real,
# checkable repair, and it is the whole of "verifiable objective" on the ReaderCard side today.
_READER_CARD_TARGET_DIMENSIONS: Final[tuple[str, ...]] = ("factual_fidelity",)
# Prompt-owned, evidenced, corrected — and still unmeasurable. Excluded rather than optimized toward,
# because a target the ruler cannot see lets GEPA pick a winner without ever scoring the repair it was
# pointed at. They stay visible in readiness under their own reason; giving them a scoring path needs a
# judge-backed correction anchor, which is a Metric change and not this Issue's.
_UNSCORABLE_CARD_DIMENSIONS: Final[tuple[str, ...]] = ("headline_fidelity", "why_support", "why_value")
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

    schema_version: Literal["tracefold.news.gepa_objective_plan.v2"] = OBJECTIVE_PLAN_SCHEMA
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


def _novelty_verifiability(
    episode: DevelopmentEpisode,
    *,
    accepted_novelty: str,
    duplicate_of: str,
    production_novelty: str,
) -> tuple[bool, str]:
    """Whether an accepted novelty disagreement is a Prompt target, or which stage actually owns it.

    A restatement the reviewer names has to be provable twice: the prior must exist in the source reader
    history the selector drew from, and it must have reached the bounded ToldContext the model was given.
    Prior outside the history is `event_evidence`/`retrieval`; inside the history but unselected is
    `retrieval`. Neither is repairable by an instruction, and both used to enter the failure clusters GEPA
    was pointed at.

    The mirror case is the one where the *model* asserts the link — it called a fact a restatement or a
    progression and the reviewer disagreed. That claim is checkable without a named prior, because the
    model made it against entries it was shown, so an instruction can be held responsible for it. With an
    empty ToldContext there was nothing to match against and the defect is the model's, not the
    instruction's.

    Everything else is the reviewer asserting a link the model did not make, on a prior the rubric gives
    no way to name — `NoveltyJudgment` only accepts `duplicate_of` for `restatement`. An accepted
    `progression` the model called `new_fact` is exactly that shape, and it is precisely the "the model
    was never shown the card" defect this module exists to keep out of the trainset, so it is excluded
    rather than optimized toward.
    """

    if accepted_novelty in {"uncertain", production_novelty}:
        return False, ""
    told_ids = {entry.event_id for entry in episode.context.told.entries}
    if accepted_novelty == "restatement":
        if not duplicate_of:
            return False, "accepted_novelty_target_not_verifiable"
        seen_ids = {str(row.get("event_id") or "") for row in (episode.policy_metric.get("seen") or ())}
        if duplicate_of not in seen_ids:
            return False, "novelty_prior_outside_source_history"
        if duplicate_of not in told_ids:
            return False, "novelty_prior_not_selected"
        return True, ""
    if production_novelty not in {"restatement", "progression"} or not told_ids:
        return False, "accepted_novelty_target_not_verifiable"
    return True, ""


def _classify(episode: DevelopmentEpisode) -> tuple[ObjectiveCase, dict[str, Any]]:
    """One case's disposition, plus the facts readiness reports about it. Pure, and never a model call."""

    review = dict(episode.accepted_review or {})
    dimensions = {str(key): str(value) for key, value in dict(review.get("dimensions") or {}).items()}
    failed = frozenset(name for name, value in dimensions.items() if value == "fail")
    expected = dict(review.get("expected") or {})
    correction = bool(str(review.get("expected_correction") or "").strip())
    has_evidence_refs = bool([str(ref) for ref in (review.get("evidence_refs") or ()) if str(ref).strip()])
    should_push = str(review.get("should_push") or "uncertain")
    novelty = dict(review.get("novelty") or {})
    accepted_novelty = str(novelty.get("judgment") or "uncertain")
    duplicate_of = str(novelty.get("duplicate_of") or "")
    owner, owner_source = _owner_identity(review)
    explicit_prompt_owned = owner_source == "explicit" and owner == PROMPT_OWNER

    semantics_targets = tuple(
        name
        for name in _EVENT_SEMANTICS_TARGET_DIMENSIONS
        if name in failed and _gold_value(expected, name) is not _NO_GOLD
    )
    card_targets = tuple(
        name for name in _READER_CARD_TARGET_DIMENSIONS if name in failed and has_evidence_refs and correction
    )
    gold_facts: dict[str, Any] = {
        "failed": tuple(sorted(failed)),
        "failed_with_exact_gold": tuple(sorted(name for name in failed if _gold_value(expected, name) is not _NO_GOLD)),
    }

    def _case(
        disposition: Disposition,
        *,
        reason: str,
        predictors: Sequence[str] = (),
        dims: Sequence[str] = (),
    ) -> tuple[ObjectiveCase, dict[str, Any]]:
        return (
            ObjectiveCase(
                case_id=episode.case_id,
                cluster_id=episode.cluster_id,
                stratum=episode.stratum,
                disposition=disposition,
                owner=owner,
                owner_source=owner_source,
                predictors=tuple(predictors),
                dimensions=tuple(dims),
                reason=reason,
            ),
            {**gold_facts, "reason": reason, "disposition": disposition},
        )

    judgment = episode.production_judgment
    policy_verifiable = True
    try:
        verify_policy_projection(episode.policy_metric)
    except ValueError:
        policy_verifiable = False
    decision = (
        production_decision(judgment, episode.policy_metric) if judgment is not None and policy_verifiable else None
    )
    production_novelty = str(judgment.verdict.novelty or "") if judgment is not None else ""
    reaches_reader = decision is not None and decision.final in _PUSH_ACTIONS
    # The owner-blind superset, computed for every case including the ones no optimizer will ever see. It
    # is what a *policy* candidate is held to, and what readiness reports as the gap between the errors the
    # corpus contains and the ones a Prompt may be asked to repair. The rule is the predecessor's, to the
    # letter: a case with no stable output pushed nothing and claimed no novelty.
    decision_failed = (should_push in {"must_push", "should_push"} and not reaches_reader) or (
        should_push in {"must_hold", "should_hold"} and reaches_reader
    )
    novelty_failed = accepted_novelty not in ("uncertain", production_novelty)
    observed_failure = bool(failed) or decision_failed or novelty_failed or correction
    observed_dimensions = set(failed)
    if decision_failed:
        observed_dimensions.add("should_push")
    if novelty_failed:
        observed_dimensions.add("novelty")
    if correction and not failed:
        observed_dimensions.add("factual_fidelity")
    gold_facts["observed_failure"] = observed_failure
    gold_facts["observed_dimensions"] = tuple(sorted(observed_dimensions))

    # Decided here, before the guards below, because a novelty disagreement is one of the three things that
    # can make a case a target — and "would this have been a target" is what decides whether an
    # unreplayable case blocks the run or merely shrinks the corpus.
    novelty_target, novelty_reason = _novelty_verifiability(
        episode,
        accepted_novelty=accepted_novelty,
        duplicate_of=duplicate_of,
        production_novelty=production_novelty,
    )
    would_be_target = explicit_prompt_owned and bool(semantics_targets or card_targets or novelty_target)

    # A case with no stable output is out of scope rather than broken: the accepted external miss is a fact
    # the Gate never admitted, so its `TriageContext` is synthesized rather than replayed, there is no
    # stable answer to preserve, and — the decisive part — `_component` credits every passed retention
    # anchor whose production field is simply absent. Left in the optimizer corpus those cases pay full
    # marks to any candidate at all, which is the one thing a control must never do.
    if judgment is None:
        return _case("excluded", reason="stable_output_absent")
    # An unverifiable policy projection is different: the case *is* in scope and the metric will raise on
    # it mid-compile, after the budget is spent. If it would otherwise have been a target, that blocks the
    # run rather than shrinking it, which is exactly what a zero-call readiness exists to say in advance.
    if not policy_verifiable:
        return _case(
            "excluded", reason="non_replayable_target" if would_be_target else "non_replayable_policy_unverifiable"
        )

    if not observed_failure:
        # Reached only past both guards above, so the decision exists; naming that here rather than
        # widening `stable_hard_gate` to accept an absence it can never be handed.
        if decision is None:  # pragma: no cover - both guards above return first
            raise ValueError("news_program_objective_decision_unavailable")
        gate = stable_hard_gate(episode, decision)
        if gate:
            return _case("excluded", reason=f"stable_hard_gate:{gate}")
        return _case("control", reason="stable_correct_under_accepted_review")

    if not explicit_prompt_owned:
        if owner_source == "explicit":
            reason = "unknown_owner" if owner == "unknown" else f"non_prompt_owner:{owner}"
        elif owner_source == "derived":
            reason = "owner_unknown_and_not_confirmed" if owner == "unknown" else f"owner_derived_only:{owner}"
        else:
            reason = "owner_absent"
        return _case("excluded", reason=reason)

    predictors: list[str] = []
    dims: list[str] = []
    if semantics_targets or novelty_target:
        predictors.append("event_semantics")
        dims.extend(semantics_targets)
        if novelty_target:
            dims.append("novelty")
    if card_targets:
        predictors.append("reader_card")
        dims.extend(card_targets)
    if dims:
        return _case(
            "target", reason="explicit_prompt_owner_with_verifiable_objective", predictors=predictors, dims=dims
        )

    if novelty_reason:
        return _case("excluded", reason=novelty_reason)
    if failed & set(_EVENT_SEMANTICS_TARGET_DIMENSIONS):
        return _case("excluded", reason="failed_typed_dimension_without_exact_gold")
    if failed & set(_READER_CARD_TARGET_DIMENSIONS):
        return _case("excluded", reason="reader_card_failure_without_evidence_and_correction")
    if failed & set(_UNSCORABLE_CARD_DIMENSIONS):
        return _case("excluded", reason="reader_card_failure_not_scorable")
    if failed:
        return _case("excluded", reason="failed_dimension_not_prompt_repairable")
    if decision_failed:
        return _case("excluded", reason="should_push_only_failure")
    return _case("excluded", reason="correction_without_owned_failed_dimension")


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

    classified = _elect_cluster_representatives([_classify(episode) for episode in episodes], episodes)
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
            "explicit_prompt_owner_n": int(owner_distribution["explicit"].get(PROMPT_OWNER, 0)),
        },
        exclusion_reasons=dict(sorted(exclusion_reasons.items())),
        exact_gold_coverage={
            "failed_by_dimension": dict(sorted(gold_failed.items())),
            "failed_with_exact_gold_by_dimension": dict(sorted(gold_with_value.items())),
            "target_dimension_gold_n": sum(gold_with_value.get(name, 0) for name in _EVENT_SEMANTICS_TARGET_DIMENSIONS),
        },
        reader_card_targets_require_semantic_judge=any("reader_card" in case.predictors for case in targets),
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
READINESS_SCHEMA: Literal["tracefold.news.gepa_readiness_report.v2"] = "tracefold.news.gepa_readiness_report.v2"
# The Program answers two Predictors per metric call, in a fixed order. Not an estimate — it is the graph.
_TASK_CALLS_PER_METRIC_CALL: Final = 2


def _judge_call_ceiling(episode: DevelopmentEpisode) -> int:
    """The most equivalence-judge calls one metric call can make on this case.

    A judge call happens for a free-text dimension the reviewer *passed* whose text the candidate changed,
    and once more when a failed `factual_fidelity` has to be verified against the frozen evidence. A
    candidate that keeps every anchor byte-for-byte makes none of them, so this is a ceiling and is
    published as one.
    """

    dimensions = dict(dict(episode.accepted_review or {}).get("dimensions") or {})
    passed = sum(1 for name in _FREE_TEXT_DIMENSIONS if str(dimensions.get(name) or "") == "pass")
    return passed + int(str(dimensions.get("factual_fidelity") or "") == "fail")


def _strata_coverage(episodes: Sequence[DevelopmentEpisode]) -> dict[str, int]:
    counts = dict.fromkeys(_REQUIRED_STRATA, 0)
    for episode in episodes:
        for stratum in _episode_strata(episode):
            counts[stratum] += 1
    return counts


def _half_counts(plan: GepaObjectivePlan, half: Sequence[DevelopmentEpisode]) -> dict[str, Any]:
    targets = set(plan.target_case_ids)
    controls = set(plan.control_case_ids)
    return {
        "case_n": len(half),
        "cluster_n": len({episode.cluster_id for episode in half}),
        "target_case_n": sum(1 for episode in half if episode.case_id in targets),
        "target_cluster_n": len({episode.cluster_id for episode in half if episode.case_id in targets}),
        "control_case_n": sum(1 for episode in half if episode.case_id in controls),
        "control_cluster_n": len({episode.cluster_id for episode in half if episode.case_id in controls}),
        "strata": _strata_coverage(half),
        "metric_judge_model_calls_max": sum(_judge_call_ceiling(episode) for episode in half),
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

    optimizer = plan.optimizer_episodes
    return {
        "schema": READINESS_SCHEMA,
        "identity": dict(identity),
        "outcome": "ready" if plan.optimizer_ready else "insufficient",
        "blocking_reasons": list(plan.blocking_reasons),
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
        "exact_gold_coverage": dict(plan.exact_gold_coverage),
        "split": plan.split,
        "split_error": plan.split_error,
        "train": _half_counts(plan, plan.train_episodes),
        "development_selection": _half_counts(plan, plan.development_selection_episodes),
        # Retrieval is reported on the *whole* corpus, not the optimizer half: "the model called it new" and
        # "the model was never shown the card" are different defects, and the second one is precisely what
        # the objective plan just excluded. Narrowing this to the optimizer corpus would hide it.
        "retrieval": _retrieval_receipt(episodes),
        "call_envelope": {
            "note": "ceilings computed from the corpus; GEPA chooses how many rounds fit in --max-metric-calls",
            "task_model_calls_per_metric_call": _TASK_CALLS_PER_METRIC_CALL,
            "metric_judge_model_calls_per_metric_call_max": max(
                (_judge_call_ceiling(episode) for episode in optimizer), default=0
            ),
            "metric_calls_per_full_selection_evaluation": len(plan.development_selection_episodes),
            "metric_calls_per_reflection_minibatch": min(10, len(plan.train_episodes)),
            "task_model_calls_per_full_selection_evaluation": _TASK_CALLS_PER_METRIC_CALL
            * len(plan.development_selection_episodes),
            "reflection_model_calls_per_proposal_round": 1,
        },
        "case_dispositions": [case.model_dump(mode="json") for case in plan.cases],
    }


__all__ = [
    "OBJECTIVE_PLAN_SCHEMA",
    "PROMPT_OWNER",
    "READINESS_SCHEMA",
    "DevelopmentEpisode",
    "Disposition",
    "GepaObjectivePlan",
    "ObjectiveCase",
    "build_gepa_objective_plan",
    "build_readiness_report",
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
