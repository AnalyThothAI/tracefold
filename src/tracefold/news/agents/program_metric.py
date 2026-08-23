"""Code-owned scoring truth shared by the cold optimizer and the offline baseline.

`accepted_review_metric` and its projection helpers used to live inside
`program_compiler`, which the architecture boundary lets exactly one module
import (`program_compiler_runner`).  A baseline harness that re-implemented the
same score would defeat the purpose of having one: the number the optimizer
maximizes and the number an operator reads before and after a RulePack edit
have to come from the same bytes.  Moving them here keeps that literal identity
while leaving the optimizer itself sandboxed.

Nothing here has database, artifact-writer, proposal or promotion authority.
"""

from __future__ import annotations

import functools
import importlib.metadata
import inspect
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from ..artifact_identity import canonical_sha
from ..models import TRIAGE_POLICY_VERSION, TriageVerdict, base_symbol
from ..storyline import final_storyline_key
from ..triage_rules import DEFAULT_POLICY, GateFacts, decide, storyline_status
from .semantic_program import TriageContext, render_model_evidence_json

METRIC_ID = "tracefold.news.production_action_feedback_v2"


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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
# A sentinel, because `None` is a legitimate absence of a reviewer opinion and must not read as "gold = null".
_NO_GOLD: Final = object()
# `news_review_v3` gold keys, per dimension. `why_support`/`why_value`/`headline_fidelity`/`factual_fidelity`/
# `timeliness` have no scalar gold: a reviewer can say the Chinese copy is wrong, but "the correct sentence" is
# not a value a rubric can hold, so those stay on the weak fallback and are counted as ungolded.
_GOLD_KEY = {"direction": "direction", "magnitude": "magnitude"}
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

    ``recorded_action`` pins the action a retired cohort actually shipped. Only the offline baseline in
    ``action_source=recorded`` mode sets it, so that "what the reader saw under policy v7" stays exactly
    reproducible after policy v8 landed. The optimizer's projection always carries Gate facts and the sent
    ledger instead, and therefore always re-runs the live ``decide()``.
    """

    recorded = str(projection.get("recorded_action") or "")
    if recorded:
        return recorded
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
            # #154: `news learning baseline` scores the production action, so this metric has to be able to
            # reach `stale_source_artifact` too.
            source_age_s=gate.get("source_age_s"),
        ),
        storyline_status(key, told=told, seen=seen),
        policy=DEFAULT_POLICY,
    )
    return decision.final


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
    key = _GOLD_KEY.get(name)
    if key is None:
        return _NO_GOLD
    value = expected.get(key, _NO_GOLD)
    return _NO_GOLD if value is None else value


def _observed_value(verdict: Mapping[str, Any], name: str) -> Any:
    if name == "asset_grounding":
        assets = verdict.get("assets")
        if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
            return frozenset()
        return frozenset(
            base_symbol(str(dict(asset).get("symbol") or "")) for asset in assets if isinstance(asset, Mapping)
        )
    return verdict.get(_DIMENSION_FIELD.get(name, ""), _NO_GOLD)


# Dimensions whose accepted value is a whole Chinese sentence, so "did the candidate keep it?" cannot be
# answered by `==`. `timeliness` is not one of them — it is handled separately in `_retains`, and only when a
# judge is present, so that the no-judge arm stays byte-for-byte the pre-#148 rule.
_FREE_TEXT_DIMENSIONS = ("headline_fidelity", "why_support", "why_value", "factual_fidelity")


def _same_value(field: str | None, verdict: Mapping[str, Any], production: Mapping[str, Any]) -> bool:
    """Literal equality. A `None` field means the dimension judges the whole card, not one value."""

    if field is None:
        return bool(production) and verdict == production
    return bool(verdict.get(field) == production.get(field))


def _retains(
    name: str,
    field: str | None,
    verdict: Mapping[str, Any],
    production: Mapping[str, Any],
    judge: Any,
) -> bool:
    """Whether the candidate kept a reviewer's `pass` on one dimension."""

    literal = _same_value(field, verdict, production)
    if literal:
        return True
    if judge is None:
        # No judge means the pre-#148 rule, exactly. Relaxing anything here would silently change what
        # `bind_metric(None)` measures, and that arm is what the receipt reports as `score_byte_equality`
        # and what every baseline recorded before this change was scored with.
        return False
    if name == "timeliness":
        # Timeliness is about when the Event arrived, not about copy: no rewriting can change it, so losing
        # the anchor over wording would charge the candidate for something it does not control.
        return True
    if name not in _FREE_TEXT_DIMENSIONS:
        return False
    # Only now is a model call worth making: the texts differ, and the question is whether they mean the same.
    return bool(judge.retains(name, production, verdict))


def _component(
    dimensions: Mapping[str, Any],
    names: Sequence[str],
    verdict: Mapping[str, Any],
    production: Mapping[str, Any],
    expected: Mapping[str, Any] | None = None,
    judge: Any = None,
) -> tuple[float, int, int] | None:
    """Score one Predictor's accepted dimensions, or ``None`` when the reviewer labelled none of them.

    Three branches, in strict order of how much the reviewer actually told us:

    1. ``pass`` — a retention anchor: keep what the reviewer accepted.
    2. ``fail`` **with gold** — the reviewer stated the correct value, so only that value scores. This is the
       DSPy-idiomatic case and the only one where the score means "right", not "different".
    3. ``fail`` **without gold** — the weak fallback the predecessor applied everywhere: any change scores.
       It cannot distinguish a repair from a coin flip, which is exactly why an optimizer left alone with it
       can learn "when a case smells like a failure, change something". Its share of the denominator is
       reported as ``gold_coverage`` rather than hidden inside a scalar.

    Returns ``(score, gold_scored_n, labelled_n)``.
    """

    labelled = _labelled(dimensions, names)
    if not labelled:
        return None
    gold = dict(expected or {})
    hits = 0.0
    gold_scored = 0
    for name, label in labelled:
        field = _DIMENSION_FIELD.get(name)
        if label == "fail":
            wanted = _gold_value(gold, name)
            if wanted is not _NO_GOLD:
                gold_scored += 1
                hits += float(_observed_value(verdict, name) == wanted)
                continue
        if field is not None and field not in production:
            hits += label == "pass"
            continue
        if label == "pass":
            hits += _retains(name, field, verdict, production, judge)
            continue
        # A `fail` with no gold: any change scores. `factual_fidelity` and `timeliness` are judgments about the
        # whole card, not one field, so "changed" means the card changed at all — scoring them as an automatic
        # pass would leave GEPA no gradient between a candidate that fixed the fact and one that changed
        # nothing. The judge is not consulted here: it answers "is this still the same?", and the reviewer has
        # already said the old value was wrong.
        same = _same_value(field, verdict, production)
        hits += not same
    return hits / len(labelled), gold_scored, len(labelled)


def accepted_review_metric(
    gold: dspy.Example,
    pred: dspy.Prediction,
    trace: Any = None,
    pred_name: str | None = None,
    pred_trace: Any = None,
    program_trace: Any = None,
    *,
    judge: Any = None,
) -> dspy.Prediction:
    """Score the reader-facing action, then the two Predictors, over accepted development truth only.

    The last four parameters carry defaults because GEPA calls this metric two different ways. When it asks for
    per-Predictor feedback it passes all five, matching `GEPAFeedbackMetric`; when it scores a candidate over
    the full valset it goes through `dspy.Evaluate`, which calls `metric(example, prediction)` with two. Without
    the defaults every full-valset evaluation raised `TypeError` and GEPA recorded it as a zero — a failure the
    `_FakeGEPA` tests could not see, because they never drove the real evaluator.

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
    rejected = str(pred.get("advisory_rejected") or "")
    if rejected:
        # Name the wall. Without this the candidate scored zero and the reflection model was told nothing, so
        # it proposed text that tripped the same bound again on the next round.
        return dspy.Prediction(
            score=0.0,
            feedback=(
                f"The proposed advisory was rejected by the code-owned safety bounds ({rejected}). "
                "Rewrite it without URLs, template braces, credential-shaped text, or any claim of authority "
                "over the QualityKernel, the RulePacks or the schema, and keep it under 8192 bytes."
            ),
        )
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
    # `news_review_v3` gold. Absent on every `news_review_v2` row, which is the point: gold coverage grows
    # from zero without a migration and without re-labelling history.
    expected = dict(review.get("expected") or {})
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
    semantics = _component(dimensions, _SEMANTICS_DIMENSIONS, verdict, production, expected, judge)
    card = _component(dimensions, _CARD_DIMENSIONS, verdict, production, expected, judge)
    gold_scored = (semantics[1] if semantics else 0) + (card[1] if card else 0)
    labelled_n = (semantics[2] if semantics else 0) + (card[2] if card else 0)
    semantics_score = semantics[0] if semantics else None
    card_score = card[0] if card else None
    if novelty_score is not None:
        semantics_score = novelty_score if semantics_score is None else (semantics_score + novelty_score) / 2
        # Accepted novelty is a stated correct value, not a "the old one was wrong" flag: it is gold by
        # construction, and counting it keeps `gold_coverage` honest about the whole scored surface.
        gold_scored += 1
        labelled_n += 1
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
    # Name the target, not just the defect. A reflection LM told "magnitude is wrong" can only guess; told
    # "the accepted magnitude is 2" it can write a rule. Only dimensions this Predictor owns are named.
    stated = [
        f"{name}={sorted(wanted) if isinstance(wanted, frozenset) else wanted}"
        for name in (_SEMANTICS_DIMENSIONS + _CARD_DIMENSIONS)
        if dimensions.get(name) == "fail"
        and (owned is None or name in owned)
        and (wanted := _gold_value(expected, name)) is not _NO_GOLD
    ]
    if stated:
        feedback.append(f"Accepted correct values: {', '.join(stated)}.")
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
        # GEPA reads only `score` and `feedback`; the baseline harness reads these to report how much of the
        # score rests on stated correct values rather than on the weak "any change scores" fallback.
        gold_scored_n=gold_scored,
        labelled_n=labelled_n,
        production_action=action,
    )


def bind_metric(judge: Any) -> Callable[..., Any]:
    """The metric with a semantic judge attached, still matching GEPA's 5-argument protocol.

    A `functools.partial` rather than a closure so `_metric_receipt` can reach the one underlying function and
    keep hashing the same bytes: the number an operator reads and the number GEPA maximizes must stay the same
    implementation, judge or no judge.
    """

    return functools.partial(accepted_review_metric, judge=judge)


def _metric_receipt(metric: Callable[..., Any], *, review_rubric_version: str) -> dict[str, Any]:
    judge = getattr(metric, "keywords", {}).get("judge") if isinstance(metric, functools.partial) else None
    metric = metric.func if isinstance(metric, functools.partial) else metric
    try:
        source = inspect.getsource(metric).replace("\r\n", "\n")
    except (OSError, TypeError) as exc:
        raise ValueError("news_program_compile_metric_source_unavailable") from exc
    return {
        "schema": "tracefold.news.compile_metric_receipt.v2",
        "metric_id": METRIC_ID,
        "gold_source": "news_reviews.payload.expected (news_review_v3); absent on v2 rows",
        # Which ruler measured the free-text retention anchors. Two runs judged differently are not comparable,
        # and `null` means the strict byte-equality rule that predates #148.
        "semantic_judge": judge.identity if judge is not None else None,
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


told_rows = _told_rows
build_compile_example = _compile_example
development_split = _honest_split
metric_receipt = _metric_receipt
retrieval_receipt = _retrieval_receipt


__all__ = [
    "METRIC_ID",
    "DevelopmentEpisode",
    "accepted_review_metric",
    "bind_metric",
    "build_compile_example",
    "development_split",
    "metric_receipt",
    "retrieval_receipt",
    "told_rows",
]
