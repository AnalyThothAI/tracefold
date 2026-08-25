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
from pydantic import BaseModel

from ..artifact_identity import canonical_sha
from ..events.storyline import final_storyline_key
from ..models import TRIAGE_POLICY_VERSION, TriageVerdict, base_symbol
from ..program.artifact import render_model_evidence_json
from ..program.contracts import EditorialEnvelope, ScoredJudgment, TriageContext
from ..triage_rules import DecisionResult, decide
from .objective import (
    _CARD_DIMENSIONS,
    _DELIVERY_DIMENSIONS,
    _FREE_TEXT_DIMENSIONS,
    _NO_GOLD,
    _RELEVANCE_DIMENSIONS,
    _SEMANTICS_DIMENSIONS,
    DevelopmentEpisode,
    _gold_value,
    _labelled,
    production_decision,
)

# v3 (#150): the scored dimension set lost `timeliness`, the policy moved from process-global
# `DEFAULT_POLICY` to the exact frozen values carried by each example, and the metric now returns typed
# per-dimension outcomes. The receipt embeds the function source, so two rulers already produce two report
# addresses — but a version label that stays put while the definition moves is a label that lies.
METRIC_ID = "tracefold.news.production_action_trade_relevance_v4"


# The four components of the candidate-selection score. Code-owned and content-addressed: they are hashed into
# the metric receipt, so an optimizer run cannot silently reweight what "better" means.
_ACTION_WEIGHT = 0.45
_RELEVANCE_WEIGHT = 0.35
_SEMANTICS_WEIGHT = 0.10
_CARD_WEIGHT = 0.10

COMPONENT_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "final_action": ("should_push",),
    "trade_relevance": _RELEVANCE_DIMENSIONS,
    "semantics_novelty": (*_SEMANTICS_DIMENSIONS, "novelty"),
    "reader_card": _CARD_DIMENSIONS,
}
# Which stage of the pipeline a rubric label describes. Deliberately *not* named "owner":
# `review._OWNER_BY_DIMENSION` already owns that word for a different question — who is to blame (`gate`,
# `triage_prompt`, `delivery`, `retrieval`) — under which `asset_grounding` is a Gate defect while here it is
# an EventSemantics-scored field. Publishing this as "owner" would invite a join with the stored
# `first_bad_owner` that is wrong for every Gate-owned dimension.
LABEL_GROUP: dict[str, str] = {
    **{name: "event_semantics" for name in _RELEVANCE_DIMENSIONS},
    **{name: "event_semantics" for name in _SEMANTICS_DIMENSIONS},
    **{name: "reader_card" for name in _CARD_DIMENSIONS},
    # Scored by nobody — `TriageVerdict` has no timeliness field — but the label is still corpus truth, and
    # #150 asks for it under the stage that owns it rather than in the catch-all.
    **{name: "delivery" for name in _DELIVERY_DIMENSIONS},
}
# The catch-all for a rubric dimension nobody has placed yet. A hand-written list of "the others" would have
# dropped the next new one silently.
UNGROUPED_LABEL = "not_scored"
_DIMENSION_FIELD = {
    "asset_grounding": "assets",
    "direction": "direction",
    "magnitude": "magnitude",
    "headline_fidelity": "headline_zh",
    "why_support": "why_zh",
    "why_value": "why_zh",
    "trade_impact_breadth": "impact_breadth",
    "trade_tradability": "tradability",
    "trade_surprise": "surprise",
    "trade_development_delta": "development_delta",
    "trade_channels": "channels",
    "trade_affected_markets": "affected_markets",
    "reader_value": "reader_value",
}


def _reader_card_owns_action_feedback(decision: DecisionResult, projection: Mapping[str, Any]) -> bool:
    """Whether this live action changed solely because ReaderCard produced a duplicate headline."""

    throttled_by = str(decision.throttled_by or "")
    return (
        not isinstance(projection.get("recorded_decision_result"), Mapping)
        and decision.final == "throttled"
        and decision.seen_scope == "all"
        and decision.seen_against >= 0
        and throttled_by.startswith("storyline:")
        and throttled_by.endswith(":seen")
    )


def _observed_value(verdict: Mapping[str, Any], name: str) -> Any:
    if name == "asset_grounding":
        assets = verdict.get("assets")
        if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
            return frozenset()
        return frozenset(
            base_symbol(str(dict(asset).get("symbol") or "")) for asset in assets if isinstance(asset, Mapping)
        )
    value = verdict.get(_DIMENSION_FIELD.get(name, ""), _NO_GOLD)
    if name in {"trade_channels", "trade_affected_markets"} and value is not _NO_GOLD:
        return tuple(str(item) for item in value or ())
    return value


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


def _scoring_anchors(
    dimensions: Mapping[str, Any],
    names: Sequence[str],
    expected: Mapping[str, Any],
) -> tuple[tuple[str, str, Any], ...]:
    """Return every labelled field and the exact repair value, when one exists."""

    return tuple(
        (name, label, _gold_value(expected, name) if label == "fail" else _NO_GOLD)
        for name, label in _labelled(dimensions, names)
    )


def _component_diagnostics(
    *,
    should_push: str,
    objective_guard: str,
    relevance_anchors: Sequence[tuple[str, str, Any]],
    semantics_anchors: Sequence[tuple[str, str, Any]],
    card_anchors: Sequence[tuple[str, str, Any]],
    expected_novelty: str,
) -> dict[str, dict[str, Any]]:
    """Describe exactly how much accepted truth supports each weighted component."""

    action_n = int(
        objective_guard == "none" and should_push in {"must_push", "should_push", "must_hold", "should_hold"}
    )
    novelty_n = int(expected_novelty != "uncertain")
    anchors = {
        "trade_relevance": tuple(relevance_anchors),
        "semantics_novelty": tuple(semantics_anchors),
        "reader_card": tuple(card_anchors),
    }
    weights = {
        "final_action": _ACTION_WEIGHT,
        "trade_relevance": _RELEVANCE_WEIGHT,
        "semantics_novelty": _SEMANTICS_WEIGHT,
        "reader_card": _CARD_WEIGHT,
    }
    diagnostics: dict[str, dict[str, Any]] = {}
    for component, fields in COMPONENT_FIELDS.items():
        component_anchors = anchors.get(component, ())
        field_n = {field: 0 for field in fields}
        for field, _label, _wanted in component_anchors:
            field_n[field] += 1
        if component == "final_action":
            field_n["should_push"] = action_n
            labelled_n = action_n
            gold_scored_n = action_n
            denominator = action_n
        else:
            labelled_n = len(component_anchors) + (novelty_n if component == "semantics_novelty" else 0)
            gold_scored_n = sum(wanted is not _NO_GOLD for _field, _label, wanted in component_anchors)
            denominator = sum(label == "pass" or wanted is not _NO_GOLD for _field, label, wanted in component_anchors)
            if component == "semantics_novelty":
                field_n["novelty"] = novelty_n
                gold_scored_n += novelty_n
                denominator += novelty_n
        diagnostics[component] = {
            "denominator": denominator,
            "effective_weight_mass": weights[component] if denominator else 0.0,
            "gold_scored_n": gold_scored_n,
            "labelled_n": labelled_n,
            "gold_coverage": round(gold_scored_n / labelled_n, 6) if labelled_n else None,
            "field_n": field_n,
        }
    return diagnostics


def _component(
    dimensions: Mapping[str, Any],
    names: Sequence[str],
    verdict: Mapping[str, Any],
    production: Mapping[str, Any],
    expected: Mapping[str, Any] | None = None,
    judge: Any = None,
    outcomes: list[tuple[str, str]] | None = None,
) -> tuple[float | None, int, int, int] | None:
    """Score one Predictor's accepted dimensions, or ``None`` when the reviewer labelled none of them.

    Three branches, in strict order of how much the reviewer actually told us:

    1. ``pass`` — a retention anchor: keep what the reviewer accepted.
    2. ``fail`` **with gold** — the reviewer stated the correct value, so only that value scores. This is the
       DSPy-idiomatic case and the only one where the score means "right", not "different".
    3. ``fail`` **without gold** — visible in corpus/field counts but absent from the effective denominator.
       It never earns credit merely for changing something.

    Returns ``(score, gold_scored_n, effective_n, labelled_n)``. ``score`` is ``None`` when labels exist but
    none has an exact scoring anchor.
    """

    anchors = _scoring_anchors(dimensions, names, dict(expected or {}))
    if not anchors:
        return None
    outcomes = [] if outcomes is None else outcomes
    hits = 0.0
    gold_scored = 0
    scored_n = 0
    for name, label, wanted in anchors:
        field = _DIMENSION_FIELD.get(name)
        if label == "fail":
            if wanted is not _NO_GOLD:
                gold_scored += 1
                scored_n += 1
                hit = _observed_value(verdict, name) == wanted
                hits += float(hit)
                outcomes.append((name, "gold_hit" if hit else "gold_miss"))
                continue
            outcomes.append((name, "not_scored_no_gold"))
            continue
        scored_n += 1
        if field is not None and field not in production:
            hits += label == "pass"
            outcomes.append((name, "field_absent"))
            continue
        if label == "pass":
            kept = _retains(name, field, verdict, production, judge)
            hits += kept
            outcomes.append((name, "retention_hit" if kept else "retention_miss"))
            continue
    return (hits / scored_n if scored_n else None, gold_scored, scored_n, len(anchors))


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
    grounded restatement drop, a similarity throttle, the former pre-v10 priority rescue, a watchlist rescue —
    so an offline gain could not predict what the reader would see, and a `must_push` miss could be averaged
    away by four retention anchors agreeing.

    Hard gates come first and are not averaged with anything. This metric proposes a candidate; it is never a
    release decision and never sees the future holdout.
    """

    del trace, pred_trace, program_trace
    review = dict(gold.get("accepted_review") or {})
    production_raw = gold.get("production_judgment")
    production_judgment = (
        production_raw
        if isinstance(production_raw, ScoredJudgment)
        else ScoredJudgment.model_validate(production_raw)
        if production_raw
        else None
    )
    production_verdict = production_judgment.verdict.model_dump(mode="json") if production_judgment is not None else {}
    production_relevance = (
        production_judgment.editorial.relevance.model_dump(mode="json")
        if production_judgment is not None and production_judgment.editorial.relevance is not None
        else {}
    )
    production = {**production_verdict, **production_relevance}
    projection = dict(gold.get("policy_metric") or {})
    dimensions = dict(review.get("dimensions") or {})
    should_push = str(review.get("should_push") or "uncertain")
    # v4 exact gold. A failed dimension without a stated correct value is visible
    # in corpus metadata but contributes neither a hit nor a denominator.
    expected = dict(review.get("expected") or {})
    gate_facts = dict(projection.get("gate") or {})
    grounded_values = {base_symbol(str(value)) for value in gate_facts.get("grounded_assets") or ()}
    watchlist_values = {base_symbol(str(value)) for value in gate_facts.get("watchlist_symbols") or ()}
    admission = str(gate_facts.get("admission") or "candidate")
    objective_guard = (
        admission
        if admission in {"listing_deterministic", "telemetry_deterministic"}
        else "watchlist"
        if grounded_values & watchlist_values
        else "none"
    )
    # Early schema/advisory gates have no exact DecisionResult to attribute. ReaderCard action feedback starts
    # disabled and is enabled below only when the live decision proves its own headline caused a seen throttle.
    action_feedback_allowed = pred_name != "reader_card"
    relevance_anchors = (
        ()
        if objective_guard in {"listing_deterministic", "telemetry_deterministic"}
        else _scoring_anchors(dimensions, _RELEVANCE_DIMENSIONS, expected)
    )
    semantics_anchors = _scoring_anchors(dimensions, _SEMANTICS_DIMENSIONS, expected)
    card_anchors = _scoring_anchors(dimensions, _CARD_DIMENSIONS, expected)
    expected_novelty = str((review.get("novelty") or {}).get("judgment") or "uncertain")
    novelty_denominator = int(expected_novelty != "uncertain")
    component_diagnostics = _component_diagnostics(
        should_push=should_push,
        objective_guard=objective_guard,
        relevance_anchors=relevance_anchors,
        semantics_anchors=semantics_anchors,
        card_anchors=card_anchors,
        expected_novelty=expected_novelty,
    )
    component_denominators = {
        name: int(diagnostic["denominator"]) for name, diagnostic in component_diagnostics.items()
    }
    effective_weight_mass = round(
        sum(float(diagnostic["effective_weight_mass"]) for diagnostic in component_diagnostics.values()), 6
    )
    included_anchors = (*relevance_anchors, *semantics_anchors, *card_anchors)
    scored_names = tuple(name for name, _, _ in included_anchors)
    labelled_n = len(included_anchors) + novelty_denominator
    gold_scored = sum(wanted is not _NO_GOLD for _, _, wanted in included_anchors) + novelty_denominator

    def _zero(
        feedback: str,
        *,
        gate: str,
        action: str = "",
        outcomes: Sequence[tuple[str, str]] | None = None,
        production_rule: str = "",
        production_throttled_by: str = "",
    ) -> dspy.Prediction:
        """A hard-gated case still says what it did.

        The first version returned a bare `score`/`feedback` here, so a `must_hold` violation reached the
        report with `production_action=""` — which `_action_confusion` reads as withheld, and therefore filed
        the single most dangerous error class as *agreement 1.0*. The same omission dropped these cases out
        of `prediction_dimensions` entirely, so a candidate with more hard failures could publish a higher
        per-dimension hit rate: the zeros left the denominator instead of entering it.
        """

        action_gates = {
            "must_push_miss",
            "must_hold_send",
            "background_realtime_send",
            "relevance_inconsistent",
            "known_duplicate_leak",
        }
        if pred_name is not None and objective_guard != "none" and gate in action_gates:
            routed_feedback = (
                "No Predictor-owned correction applies; the code-owned objective guard action is reported "
                "as policy evidence only."
            )
        elif gate not in action_gates or action_feedback_allowed:
            routed_feedback = feedback
        else:
            routed_feedback = "No ReaderCard-owned correction applies; retain factual headline and why copy."
        return dspy.Prediction(
            score=0.0,
            feedback=routed_feedback,
            hard_gate=gate,
            production_action=action,
            production_rule=production_rule,
            production_throttled_by=production_throttled_by,
            objective_guard=objective_guard,
            gold_scored_n=gold_scored,
            labelled_n=labelled_n,
            component_scores={
                name: 0.0 if denominator else None for name, denominator in component_denominators.items()
            },
            component_denominators=component_denominators,
            component_diagnostics=component_diagnostics,
            effective_weight_mass=effective_weight_mass,
            dimension_outcomes=tuple(outcomes)
            if outcomes is not None
            else tuple((name, "unscored") for name in scored_names),
        )

    rejected = str(pred.get("advisory_rejected") or "")
    if rejected:
        # Name the wall. Without this the candidate scored zero and the reflection model was told nothing, so
        # it proposed text that tripped the same bound again on the next round.
        return _zero(
            f"The proposed advisory was rejected by the code-owned safety bounds ({rejected}). "
            "Rewrite it without URLs, template braces, credential-shaped text, or any claim of authority "
            "over the QualityKernel, the RulePacks or the schema, and keep it under 8192 bytes.",
            gate="advisory_rejected",
        )
    try:
        verdict_value = pred.get("verdict")
        verdict = (
            verdict_value.model_dump(mode="json") if isinstance(verdict_value, BaseModel) else dict(verdict_value or {})
        )
        if not verdict:
            raise ValueError("verdict_missing")
        typed = TriageVerdict.model_validate(verdict)
        editorial_value = pred.get("editorial")
        editorial = (
            editorial_value
            if isinstance(editorial_value, EditorialEnvelope)
            else EditorialEnvelope.model_validate(editorial_value)
        )
        judgment = ScoredJudgment.issue(verdict=typed, editorial=editorial)
    except Exception:
        return _zero("Return one complete, schema-valid semantic judgment and card.", gate="schema_invalid")

    feedback: list[str] = []
    decision = production_decision(judgment, projection)
    if pred_name == "reader_card":
        action_feedback_allowed = _reader_card_owns_action_feedback(decision, projection)
    action = decision.final
    reaches_reader = action in {"push", "escalate"}
    decision_metadata = {
        "production_rule": decision.override_rule or "",
        "production_throttled_by": decision.throttled_by or "",
    }
    relevance = editorial.relevance.model_dump(mode="json") if editorial.relevance is not None else {}
    observed = {**verdict, **relevance}

    # What the candidate did, dimension by dimension. Computed before the gates, because a gated case still
    # produced a verdict and its dimensions are still comparable — the gate decides the score, not whether
    # the candidate is observable.
    outcomes: list[tuple[str, str]] = []
    relevance_component = (
        None
        if objective_guard in {"listing_deterministic", "telemetry_deterministic"}
        else _component(dimensions, _RELEVANCE_DIMENSIONS, observed, production, expected, judge, outcomes)
    )
    semantics = _component(dimensions, _SEMANTICS_DIMENSIONS, observed, production, expected, judge, outcomes)
    card = _component(dimensions, _CARD_DIMENSIONS, observed, production, expected, judge, outcomes)

    # ---- hard gates: a dangerous miss cannot be averaged away ----
    if should_push == "must_push" and not reaches_reader:
        return _zero(
            f"The reader must receive this fact; the production policy resolved it to {action}.",
            gate="must_push_miss",
            action=action,
            outcomes=outcomes,
            **decision_metadata,
        )
    if should_push == "must_hold" and reaches_reader:
        return _zero(
            f"The reader must not receive this fact; the production policy resolved it to {action}.",
            gate="must_hold_send",
            action=action,
            outcomes=outcomes,
            **decision_metadata,
        )
    if dimensions.get("factual_fidelity") == "fail":
        evidence_json = str(gold.get("card_evidence_json") or "")
        verify_facts = getattr(judge, "facts_supported", None)
        try:
            facts_supported = bool(
                evidence_json and callable(verify_facts) and verify_facts(evidence_json, typed.model_dump(mode="json"))
            )
        except Exception:
            facts_supported = False
        if not facts_supported:
            return _zero(
                "The candidate's factual repair could not be verified against the immutable Event evidence.",
                gate="factual_contradiction",
                action=action,
                outcomes=outcomes,
                **decision_metadata,
            )
    # Symbol sets, canonicalized on both sides. Gate grounding carries the provider's raw tag (`XYZ-CL`), and
    # a raw `.upper()` comparison would zero a candidate that correctly named `CL`.
    ungrounded = sorted(
        asset.symbol
        for asset in typed.assets
        if asset.role == "primary" and grounded_values and base_symbol(asset.symbol) not in grounded_values
    )
    if ungrounded:
        return _zero(
            f"Primary assets must be grounded in the evidence; {', '.join(ungrounded)} are not.",
            gate="ungrounded_primary_asset",
            action=action,
            outcomes=outcomes,
            **decision_metadata,
        )
    if objective_guard == "none" and relevance.get("reader_value") in {"background", "none"} and reaches_reader:
        return _zero(
            "Background material must not interrupt the reader without an objective guard.",
            gate="background_realtime_send",
            action=action,
            outcomes=outcomes,
            **decision_metadata,
        )
    if decision.override_rule == "trade_relevance_inconsistent":
        return _zero(
            "Trade relevance is internally inconsistent with the code-owned realtime eligibility contract.",
            gate="relevance_inconsistent",
            action=action,
            outcomes=outcomes,
            **decision_metadata,
        )
    if expected_novelty == "restatement" and reaches_reader and objective_guard == "none":
        return _zero(
            "A known same-fact restatement leaked through the production duplicate policy.",
            gate="known_duplicate_leak",
            action=action,
            outcomes=outcomes,
            **decision_metadata,
        )

    # ---- weighted score over what the reviewer actually labelled ----
    action_score: float | None
    if objective_guard != "none":
        action_score = None
    elif should_push in {"must_push", "should_push"}:
        action_score = float(reaches_reader)
        if not reaches_reader and action_feedback_allowed:
            feedback.append(f"Accepted review says this fact should reach the reader; policy resolved it to {action}.")
    elif should_push in {"must_hold", "should_hold"}:
        action_score = float(not reaches_reader)
        if reaches_reader and action_feedback_allowed:
            feedback.append(f"Accepted review says this fact should be held; policy resolved it to {action}.")
    else:
        action_score = None

    # Novelty is the epoch's whole subject: a candidate that answers `new_fact` for every accepted
    # `restatement` must not score the same as one that gets it right, and on an `uncertain` action label
    # nothing else would notice.
    novelty_score = None if expected_novelty == "uncertain" else float(str(verdict.get("novelty")) == expected_novelty)
    semantics_score = semantics[0] if semantics else None
    relevance_score = relevance_component[0] if relevance_component else None
    card_score = card[0] if card else None
    if novelty_score is not None:
        semantics_score = novelty_score if semantics_score is None else (semantics_score + novelty_score) / 2
    components = [
        (_ACTION_WEIGHT, action_score),
        (_RELEVANCE_WEIGHT, relevance_score),
        (_SEMANTICS_WEIGHT, semantics_score),
        (_CARD_WEIGHT, card_score),
    ]
    present = [(weight, value) for weight, value in components if value is not None]
    score = sum(weight * value for weight, value in present) / sum(weight for weight, _ in present) if present else 0.0

    # ---- per-Predictor feedback: never ask a Predictor to repair what it cannot cause ----
    owned = (
        _RELEVANCE_DIMENSIONS + _SEMANTICS_DIMENSIONS
        if pred_name == "event_semantics"
        else _CARD_DIMENSIONS
        if pred_name == "reader_card"
        else None
    )
    failed = sorted(name for name, label in dimensions.items() if label == "fail" and (owned is None or name in owned))
    if failed:
        feedback.append(f"Repair accepted failed dimensions: {', '.join(failed)}.")
    # Name the target, not just the defect. A reflection LM told "magnitude is wrong" can only guess; told
    # "the accepted magnitude is 2" it can write a rule. Only dimensions this Predictor owns are named.
    stated = [
        f"{name}={sorted(wanted) if isinstance(wanted, frozenset) else wanted}"
        for name in (_RELEVANCE_DIMENSIONS + _SEMANTICS_DIMENSIONS + _CARD_DIMENSIONS)
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
    if correction and (pred_name is None or bool(failed)):
        feedback.append(f"Reviewer correction: {correction}")

    return dspy.Prediction(
        score=round(score, 6),
        feedback=" ".join(feedback) or "Retain the accepted behavior while making the output more precise.",
        # Which gate zeroed the case, or "" when none did. The predecessor recovered this by matching the
        # feedback prose in the baseline harness, which broke silently the moment a sentence was reworded.
        hard_gate="",
        # GEPA reads only `score` and `feedback`; the baseline harness reads these to report how much of the
        # score rests on stated correct values rather than on the weak "any change scores" fallback.
        gold_scored_n=gold_scored,
        labelled_n=labelled_n,
        production_action=action,
        production_rule=decision.override_rule or "",
        production_throttled_by=decision.throttled_by or "",
        objective_guard=objective_guard,
        component_scores={
            "final_action": action_score,
            "trade_relevance": relevance_score,
            "semantics_novelty": semantics_score,
            "reader_card": card_score,
        },
        component_denominators=component_denominators,
        component_diagnostics=component_diagnostics,
        effective_weight_mass=effective_weight_mass,
        # What the candidate actually did, dimension by dimension. Without this a report can only publish the
        # corpus's own label distribution, which is byte-identical however the predictions change.
        dimension_outcomes=tuple(outcomes),
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
        source_objects: dict[str, Any] = {
            "tracefold.news.learning.metric": inspect.getmodule(accepted_review_metric),
            # #199 moved the corpus vocabulary this function reads — the dimension groups, the exact-gold
            # lookup, the frozen policy and the production action — into the framework-neutral module that
            # also decides which cases are in scope. The receipt follows it: a ruler whose definition lives
            # in two files has to commit to both, or half of it can change unobserved.
            "tracefold.news.learning.objective": inspect.getmodule(production_decision),
            "tracefold.news.models.base_symbol": base_symbol,
            "tracefold.news.events.storyline": inspect.getmodule(final_storyline_key),
            "tracefold.news.triage_rules": inspect.getmodule(decide),
        }
        if any(source is None for source in source_objects.values()):
            raise OSError("metric source module unavailable")
        source_unit_sha256 = {
            name: canonical_sha(inspect.getsource(source).replace("\r\n", "\n"))
            for name, source in source_objects.items()
        }
        metric_source = inspect.getsource(metric).replace("\r\n", "\n")
    except (OSError, TypeError) as exc:
        raise ValueError("news_program_compile_metric_source_unavailable") from exc
    return {
        "schema": "tracefold.news.compile_metric_receipt.v3",
        "metric_id": METRIC_ID,
        "gold_source": "news_reviews.payload.expected (news_review_v4 exact gold only)",
        # Which ruler measured the free-text retention anchors. Two runs judged differently are not comparable,
        # and `null` means the strict byte-equality rule that predates #148.
        "semantic_judge": judge.identity if judge is not None else None,
        "implementation": {
            "module": str(metric.__module__),
            "qualname": str(metric.__qualname__),
            "source": metric_source,
            "helper_source_root_sha256": canonical_sha(source_unit_sha256),
            "helper_qualnames": sorted(source_unit_sha256),
            "source_unit_sha256": source_unit_sha256,
        },
        # What "better" means, pinned. An optimizer run cannot reweight the components, swap the policy it is
        # scored against, or move to a different review rubric without changing this hash.
        "weights": {
            "final_action": _ACTION_WEIGHT,
            "trade_relevance": _RELEVANCE_WEIGHT,
            "semantics_novelty": _SEMANTICS_WEIGHT,
            "reader_card": _CARD_WEIGHT,
        },
        "dimensions": {
            "trade_relevance": list(_RELEVANCE_DIMENSIONS),
            "semantics_novelty": [*_SEMANTICS_DIMENSIONS, "novelty(accepted_field)"],
            "reader_card": list(_CARD_DIMENSIONS),
        },
        "hard_gates": [
            "must_push_miss",
            "must_hold_send",
            "schema_invalid",
            "factual_contradiction",
            "ungrounded_primary_asset",
            "background_realtime_send",
            "relevance_inconsistent",
            "known_duplicate_leak",
        ],
        "action_source": {
            "policy": "tracefold.news.triage_rules.decide",
            "policy_version": TRIAGE_POLICY_VERSION,
            # Deliberately not a value. The policy that scores an example travels with that example
            # (`policy_metric.policy_values` / `policy_sha256`), because the metric must follow the arm that
            # actually ran rather than whatever this process happened to import. The report pins the exact
            # values it used; recording them here too would only invite the two to disagree.
            "policy_values": "per_example: policy_metric.policy_values, verified against policy_sha256",
            "storyline": "tracefold.news.events.storyline.final_storyline_key",
            "operational_controls": "none_the_pause_mute_plane_was_removed_in_137",
        },
        "review_rubric_version": review_rubric_version,
        "dspy_version": importlib.metadata.version("dspy"),
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
        production_judgment=(
            episode.production_judgment.model_dump(mode="json") if episode.production_judgment is not None else None
        ),
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
metric_receipt = _metric_receipt


__all__ = [
    "COMPONENT_FIELDS",
    "LABEL_GROUP",
    "METRIC_ID",
    "UNGROUPED_LABEL",
    "accepted_review_metric",
    "bind_metric",
    "build_compile_example",
    "metric_receipt",
    "told_rows",
]
