"""One owner for the three per-target rulers and the denominators they are read under (#651 §8, §7.3).

Before this module there were two implementations of "is this taxonomy right" (the composite
production-action metric and the GEPA classification ruler), one of "did the card keep the reviewer's
facts" (literal containment), and no implementation at all of "did the model name the right instrument in
the right market".  Three rulers that disagree cannot be compared, so the number an operator reads before
a prompt edit and the number GEPA maximizes came from different bytes.  Everything that scores a News
Program answer against accepted Gold now lives here; ``learning/metric.py``, ``learning/optimizer.py``,
``learning/baseline.py`` and ``learning/evaluate.py`` import it and none of them re-implement it.

Two things are deliberately explicit.

**A score without a denominator is not a measurement.**  Every ruler returns an ``outcome`` naming what
kind of answer this case produced, and ``summarize_target_outcomes`` turns a run's outcomes into the
counts a report publishes.  A case the corpus cannot ask (``not_applicable``), one no reviewer answered
(``no_gold``), one the judge could not answer (``judge_unavailable``) and one whose Gold target never
reached the model (``retrieval_miss``) are all *excluded* from the mean and *counted* separately, because
scoring any of them as zero charges a candidate for a question it was never asked.  The two failures that
are the candidate's own — a truncated output and an output that does not validate — score zero and stay
in the mean.

**A ruler that needs a judge says so.**  ``judge`` is an argument, never ambient state: the rulers are
pure given one, and callers bind it with ``functools.partial`` exactly as ``metric.bind_metric`` does.
``judge=None`` means "this caller has no metric-judge route" — the offline optimizer has a task endpoint
and a reflection endpoint and no third one — and the rulers then use their deterministic arms, which can
report a false miss on a paraphrase but never a false pass.  A judge that is configured and *fails* is a
different situation entirely and is reported as ``judge_unavailable``, never as a zero.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import ValidationError

from ..program.signatures import EventSemantics, ReaderCard
from ..taxonomy import ModelTaxonomyV1
from .card_lint import lint_reader_card
from .objective import (
    TypedAssetClaim,
    asset_claims_match,
    known_wrong_markets,
    typed_asset_claims,
)
from .taxonomy_metric import TAXONOMY_AXES, TaxonomyComparison, compare_taxonomy

TARGET_METRICS_ID: Final = "tracefold.news.target_metrics.v1"

# The two candidate-local task failures `optimizer._LearningStudent` converts into ordinary Predictions.
# They live here because the ruler is what decides they are worth zero, and the wrapper is only what
# stops them from crashing the run.
TASK_OUTPUT_TRUNCATED: Final = "news_program_compile_task_model_output_truncated"
TASK_OUTPUT_INVALID: Final = "news_program_compile_task_model_output_invalid"

TargetOutcome = Literal[
    # The candidate answered and the answer was scored.
    "scored",
    # The candidate's own failures. Both score 0 and stay in the mean.
    "schema_failure",
    "technical_failure",
    # A `taxonomy` Predictor that did not answer while the rest of the judgment did (#651 §5.3). Named
    # for its cause rather than folded into `technical_failure`, because an operator repairs the two
    # differently, and counted with the failures because the reader lost a classification either way.
    "taxonomy_unavailable",
    # Excluded from the mean. The first two are properties of the corpus, the last two of the run.
    "no_gold",
    "not_applicable",
    "judge_unavailable",
    "retrieval_miss",
]

TARGET_OUTCOMES: Final[tuple[TargetOutcome, ...]] = (
    "scored",
    "schema_failure",
    "technical_failure",
    "taxonomy_unavailable",
    "no_gold",
    "not_applicable",
    "judge_unavailable",
    "retrieval_miss",
)
FAILURE_OUTCOMES: Final[frozenset[str]] = frozenset({"schema_failure", "technical_failure", "taxonomy_unavailable"})
EXCLUDED_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"no_gold", "not_applicable", "judge_unavailable", "retrieval_miss"}
)

# Above this share of applicable cases, the explanation evaluation is `unavailable` rather than a number:
# a run that could not ask its judge on more than one case in five has not measured explanation quality,
# and publishing the mean of the cases it *did* reach would report the easy cases as the corpus. One
# constant, read by `summarize_target_outcomes` and by every report that forwards its verdict.
JUDGE_UNAVAILABLE_SHARE_MAX: Final = 0.2

# The reviewer error classes that make a card wrong rather than merely thin (#651 §7.2). An unsupported
# card on one of these is already zero, because `F1(0, coverage)` is zero; the list is published so a
# report can say which class the corpus was about.
SEVERE_ERROR_TYPES: Final[frozenset[str]] = frozenset(
    {"entity", "number_unit", "condition", "status_plan_vs_executed", "unsupported_cause"}
)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _round(value: float) -> float:
    return round(float(value), 6)


def _f1(left: float, right: float) -> float:
    """The harmonic mean of two rates, which is zero when either is."""

    return 0.0 if left <= 0 or right <= 0 else 2 * left * right / (left + right)


def _set_f1(expected: frozenset[Any], observed: frozenset[Any]) -> float:
    if not expected and not observed:
        return 1.0
    if not expected or not observed:
        return 0.0
    overlap = len(expected & observed)
    if not overlap:
        return 0.0
    precision = overlap / len(observed)
    recall = overlap / len(expected)
    return 2 * precision * recall / (precision + recall)


def _set_precision_recall(expected: frozenset[Any], observed: frozenset[Any]) -> tuple[float, float]:
    overlap = len(expected & observed)
    precision = 1.0 if not observed else overlap / len(observed)
    recall = 1.0 if not expected else overlap / len(expected)
    return precision, recall


def _result(
    *,
    score: float | None,
    feedback: str,
    outcome: TargetOutcome,
    components: Mapping[str, Any],
    objective_scores: Mapping[str, float] | None = None,
) -> dspy.Prediction:
    """The one shape every ruler returns.

    `objective_scores` is GEPA's per-axis breakdown and is only meaningful for a scored or failed case;
    an excluded case carries none, because an axis nobody could measure is not an axis that scored zero.
    """

    return dspy.Prediction(
        score=None if score is None else _round(score),
        feedback=feedback,
        outcome=outcome,
        components=dict(components),
        objective_scores=dict(objective_scores or {}),
    )


def _task_output_failure(pred: Any, *, objectives: Mapping[str, float]) -> dspy.Prediction | None:
    """Score the two candidate-local failures the student wrapper converts, or None for a real answer."""

    failure = getattr(pred, "task_output_failure", None)
    if failure == TASK_OUTPUT_TRUNCATED:
        return _result(
            score=0.0,
            feedback="output truncated: the candidate did not finish this example's JSON.",
            outcome="technical_failure",
            components={"failure": TASK_OUTPUT_TRUNCATED},
            objective_scores=objectives,
        )
    if failure == TASK_OUTPUT_INVALID:
        return _result(
            score=0.0,
            feedback=str(getattr(pred, "task_output_feedback", "Typed output is invalid.")),
            outcome="schema_failure",
            components={"failure": TASK_OUTPUT_INVALID},
            objective_scores=objectives,
        )
    return None


def _not_applicable(gold: Any, target: str) -> dspy.Prediction | None:
    """A case whose sealed review never answered this target's question is not this ruler's to score."""

    applicable = getattr(gold, "applicable_targets", None)
    if applicable is None or target in tuple(applicable):
        return None
    return _result(
        score=None,
        feedback=f"This case is not evidence for {target}; its review answered {sorted(applicable) or 'nothing'}.",
        outcome="not_applicable",
        components={"applicable_targets": sorted(str(name) for name in applicable)},
    )


# --- classification -------------------------------------------------------------------------------

CLASSIFICATION_AXES: Final[tuple[str, ...]] = (
    "subject_codes_set_f1",
    "event_family_accuracy",
    "change_state_accuracy",
    "assertion_status_accuracy",
    # Kept as a published axis and demoted out of every gate (#651 §8): the share of clusters whose four
    # axes are all right at once is a diagnostic an operator reads, not the thing a release turns on.
    "four_axis_exact_accuracy",
)

_AXIS_BY_FIELD: Final[dict[str, str]] = {
    "subject_codes": "subject_codes_set_f1",
    "event_family": "event_family_accuracy",
    "change_state": "change_state_accuracy",
    "assertion_status": "assertion_status_accuracy",
}


def zero_classification_objectives() -> dict[str, float]:
    return dict.fromkeys(CLASSIFICATION_AXES, 0.0)


def classification_axis_values(comparison: TaxonomyComparison) -> dict[str, float]:
    """One comparison's value on each published axis, from the one comparison every caller already has."""

    return {
        "subject_codes_set_f1": float(comparison.subject_f1),
        "event_family_accuracy": float(comparison.event_family_match),
        "change_state_accuracy": float(comparison.change_state_match),
        "assertion_status_accuracy": float(comparison.assertion_status_match),
        "four_axis_exact_accuracy": float(comparison.exact),
    }


def _stated_axes(gold: Any) -> tuple[str, ...]:
    """Which of the four axes this Gold actually states.

    `ModelTaxonomyV1` requires three of the four and defaults `subject_codes` to empty, so an accepted
    review that validates states all four and the mask below is the identity — which is the honest answer
    while the rubric is all-or-nothing.  The mask exists because a raw mapping that omits an axis is a
    partial Gold, and charging a candidate for an axis nobody labelled is the defect this module exists
    to stop; when the rubric admits partial taxonomy Gold, this is already the rule that scores it.
    """

    if isinstance(gold, ModelTaxonomyV1):
        return TAXONOMY_AXES
    stated = tuple(axis for axis in TAXONOMY_AXES if axis in dict(gold or {}))
    return stated or TAXONOMY_AXES


def classification_score(gold: Any, comparison: TaxonomyComparison) -> float:
    """The partial classification score: the mean over the axes this Gold states."""

    values = classification_axis_values(comparison)
    stated = _stated_axes(gold)
    return _mean([values[_AXIS_BY_FIELD[axis]] for axis in stated])


def _axis_label(taxonomy: Any, axis: str) -> str:
    if isinstance(taxonomy, ModelTaxonomyV1):
        return str(getattr(taxonomy, axis))
    return str(dict(taxonomy or {}).get(axis) or "")


def _taxonomy_unavailable(pred: Any) -> bool:
    """Whether the taxonomy Predictor declined this case while the rest of the judgment answered."""

    if str(getattr(pred, "taxonomy_status", "") or "") == "unavailable":
        return True
    editorial = getattr(pred, "editorial", None)
    if isinstance(editorial, Mapping):
        return str(editorial.get("taxonomy_status") or "") == "unavailable"
    return False


def classification_metric(
    gold: Any,
    pred: Any,
    trace: Any = None,
    pred_name: str | None = None,
    pred_trace: Any = None,
    *,
    judge: Any = None,
) -> dspy.Prediction:
    """Score one taxonomy answer against accepted Gold, masked to the axes that Gold states.

    `judge` is accepted and unread: classification is a closed-vocabulary comparison against a codebook,
    and a model opinion about it would be a second codebook.  The argument is in the signature so every
    target binds the same way.
    """

    del trace, pred_name, pred_trace, judge
    skip = _not_applicable(gold, "classification")
    if skip is not None:
        return skip
    zero = zero_classification_objectives()
    expected = getattr(gold, "gold_taxonomy", None)
    if expected is None:
        return _result(
            score=None,
            feedback="No accepted taxonomy Gold on this case.",
            outcome="no_gold",
            components={},
        )
    failure = _task_output_failure(pred, objectives=zero)
    if failure is not None:
        return failure
    if _taxonomy_unavailable(pred):
        return _result(
            score=0.0,
            feedback=(
                "The taxonomy Predictor returned no label on a case whose reviewer did: an absent "
                "classification is a task failure, not an abstention."
            ),
            outcome="taxonomy_unavailable",
            components={"taxonomy_status": "unavailable"},
            objective_scores=zero,
        )
    try:
        observed = ModelTaxonomyV1.model_validate(getattr(pred, "taxonomy", None))
        comparison = compare_taxonomy(expected, observed)
    except ValueError as exc:
        return _result(
            score=0.0,
            feedback=f"Typed ModelTaxonomyV1 is invalid: {exc}",
            outcome="schema_failure",
            components={"failure": TASK_OUTPUT_INVALID},
            objective_scores=zero,
        )
    axes = classification_axis_values(comparison)
    stated = _stated_axes(expected)
    return _result(
        score=classification_score(expected, comparison),
        feedback=comparison.feedback,
        outcome="scored",
        components={
            "stated_axes": list(stated),
            "wrong_axes": list(comparison.wrong_axes),
            "missing_subjects": list(comparison.missing_subjects),
            "extra_subjects": list(comparison.extra_subjects),
            "subject_f1": _round(comparison.subject_f1),
            "four_axis_exact": bool(comparison.exact),
            "gold_event_family": str(_axis_label(expected, "event_family")),
            "predicted_event_family": str(observed.event_family),
            "axes": {name: _round(value) for name, value in axes.items()},
        },
        objective_scores=axes,
    )


def asset_grounding_outcome(
    observed: frozenset[TypedAssetClaim],
    expected: frozenset[TypedAssetClaim],
) -> tuple[bool, str]:
    """Whether one asset answer is the accepted one, and which of the three outcomes it is.

    The composite production-action metric and the understanding ruler ask the same question of the same
    typed claims, so they ask it here. A `known_wrong_market` is reported apart from an ordinary miss
    because it is a different defect with a different repair (#651 §6.2).
    """

    if asset_claims_match(observed, expected):
        return True, "gold_hit"
    return False, "known_wrong_market" if known_wrong_markets(observed, expected) else "gold_miss"


# --- understanding --------------------------------------------------------------------------------

UNDERSTANDING_AXES: Final[tuple[str, ...]] = (
    "typed_semantics_valid",
    "primary_asset_f1",
    "asset_role_accuracy",
    "novelty_accuracy",
)


def zero_understanding_objectives() -> dict[str, float]:
    return dict.fromkeys(UNDERSTANDING_AXES, 0.0)


def _primaries(claims: frozenset[TypedAssetClaim]) -> frozenset[tuple[str, str]]:
    """The typed identity of every claim the answer calls the subject: `(symbol, market_type)`."""

    return frozenset((symbol, market) for role, symbol, market in claims if role == "primary")


def _roles(claims: frozenset[TypedAssetClaim]) -> dict[str, str]:
    return {symbol: role for role, symbol, _market in claims}


def _role_accuracy(expected: frozenset[TypedAssetClaim], observed: frozenset[TypedAssetClaim]) -> float:
    """Of the symbols both sides name, the share the candidate gave the accepted role.

    Separate from the typed primary F1 because they are different repairs: naming `V` at all is grounding,
    calling it a mention when the reviewer said it was the subject is a role error, and saying `SEI` is a
    coin when the reviewer said it is the listed insurer is `known_wrong_market`.
    """

    expected_roles = _roles(expected)
    observed_roles = _roles(observed)
    shared = sorted(set(expected_roles) & set(observed_roles))
    if not shared:
        return 1.0 if not expected_roles else 0.0
    return _mean([float(expected_roles[symbol] == observed_roles[symbol]) for symbol in shared])


def _told_event_ids(gold: Any) -> tuple[str, ...]:
    return tuple(str(value) for value in (getattr(gold, "gold_told_event_ids", ()) or ()))


def _novelty_target_ok(
    *,
    restates: int,
    told_event_ids: Sequence[str],
    duplicate_of: str,
    cluster_event_ids: frozenset[str],
) -> tuple[bool, str]:
    """Whether a restatement points at the told entry the reviewer restated.

    Label equality alone scored a candidate that answered `restatement` and pointed at an unrelated card
    as fully correct, which is the one thing `restates` exists to decide: `triage_rules` reads it to
    compare directions and to refuse a flip, so the index *is* the answer.  A same-cluster entry counts,
    because a connected fact cluster is exactly the set of Events the corpus considers one fact, and the
    reviewer's `duplicate_of` is one representative of it rather than the only admissible one.
    """

    if not 0 <= restates < len(told_event_ids):
        return False, f"`restates`={restates} points outside the {len(told_event_ids)} cards this Event was shown."
    named = str(told_event_ids[restates])
    if duplicate_of and named == duplicate_of:
        return True, ""
    if named in cluster_event_ids:
        return True, ""
    return False, (
        f"`restates`={restates} points at told entry {named or '(none)'}; "
        f"the reviewer restated {duplicate_of or '(an entry in this fact cluster)'}."
    )


def understanding_metric(
    gold: Any,
    pred: Any,
    trace: Any = None,
    pred_name: str | None = None,
    pred_trace: Any = None,
    *,
    judge: Any = None,
) -> dspy.Prediction:
    """Score one typed EventSemantics answer: which instruments it names, and what it says is new.

    `judge` is accepted and unread for the same reason as in classification — every fact here is a typed
    comparison against an accepted answer, and there is nothing for a model to have an opinion about.
    """

    del trace, pred_name, pred_trace, judge
    skip = _not_applicable(gold, "understanding")
    if skip is not None:
        return skip
    zero = zero_understanding_objectives()
    failure = _task_output_failure(pred, objectives=zero)
    if failure is not None:
        return failure
    try:
        semantics = EventSemantics.model_validate(getattr(pred, "semantics", None))
    except ValidationError as exc:
        return _result(
            score=0.0,
            feedback=f"Typed EventSemantics is invalid: {exc}",
            outcome="schema_failure",
            components={"failure": TASK_OUTPUT_INVALID},
            objective_scores=zero,
        )
    objectives = dict(zero)
    objectives["typed_semantics_valid"] = 1.0
    scored: list[float] = []
    notes: list[str] = []
    components: dict[str, Any] = {"typed_semantics_valid": True}

    expected_assets = getattr(gold, "gold_assets", None)
    if expected_assets is not None:
        expected_claims = frozenset(expected_assets)
        observed_claims = typed_asset_claims([asset.model_dump(mode="json") for asset in semantics.assets])
        expected_primaries = _primaries(expected_claims)
        observed_primaries = _primaries(observed_claims)
        precision, recall = _set_precision_recall(expected_primaries, observed_primaries)
        primary_f1 = _set_f1(expected_primaries, observed_primaries)
        role = _role_accuracy(expected_claims, observed_claims)
        wrong_markets = known_wrong_markets(observed_claims, expected_claims)
        objectives["primary_asset_f1"] = primary_f1
        objectives["asset_role_accuracy"] = role
        scored.extend([primary_f1, role])
        components["primary_precision"] = _round(precision)
        components["primary_recall"] = _round(recall)
        components["primary_f1"] = _round(primary_f1)
        components["role_accuracy"] = _round(role)
        components["known_wrong_market"] = list(wrong_markets)
        components["gold_primaries"] = sorted(f"{symbol}/{market}" for symbol, market in expected_primaries)
        components["predicted_primaries"] = sorted(f"{symbol}/{market}" for symbol, market in observed_primaries)
        if wrong_markets:
            notes.append(
                "You named the right symbol in the wrong market: "
                + ", ".join(wrong_markets)
                + ". The accepted identity is "
                + ", ".join(sorted(f"{symbol}/{market}" for symbol, market in expected_primaries))
                + "."
            )
        elif primary_f1 < 1.0:
            notes.append(
                "Accepted primary instruments are "
                + (", ".join(sorted(f"{symbol}/{market}" for symbol, market in expected_primaries)) or "none")
                + "; you named "
                + (", ".join(sorted(f"{symbol}/{market}" for symbol, market in observed_primaries)) or "none")
                + "."
            )
        if role < 1.0:
            notes.append("At least one instrument carries the wrong role (subject versus mention).")

    expected_novelty = getattr(gold, "gold_novelty", None)
    if expected_novelty is not None:
        told_ids = _told_event_ids(gold)
        duplicate_of = str(getattr(gold, "gold_duplicate_of", "") or "")
        cluster_event_ids = frozenset(str(value) for value in (getattr(gold, "gold_cluster_event_ids", ()) or ()))
        if str(expected_novelty) == "restatement" and duplicate_of and duplicate_of not in told_ids:
            # The reviewer's answer was never in the frozen ledger this candidate read, so no answer it
            # could have given would have been right. Counted, excluded, and never charged to the model.
            return _result(
                score=None,
                feedback=(
                    f"The restated card {duplicate_of} was not in the {len(told_ids)} entries this Event "
                    "was shown; retrieval, not the model, decided this case."
                ),
                outcome="retrieval_miss",
                components={**components, "gold_duplicate_of": duplicate_of, "told_n": len(told_ids)},
            )
        label_hit = str(semantics.novelty) == str(expected_novelty)
        novelty = float(label_hit)
        if label_hit and str(expected_novelty) == "restatement":
            target_ok, target_note = _novelty_target_ok(
                restates=int(semantics.restates),
                told_event_ids=told_ids,
                duplicate_of=duplicate_of,
                cluster_event_ids=cluster_event_ids,
            )
            components["restatement_target_correct"] = target_ok
            if not target_ok:
                # The whole axis, not a fraction of it: a restatement that points at the wrong card is a
                # wrong answer about which fact is being repeated, and `decide()` acts on the index.
                novelty = 0.0
                notes.append(target_note)
        objectives["novelty_accuracy"] = novelty
        scored.append(novelty)
        components["novelty_accuracy"] = _round(novelty)
        components["gold_novelty"] = str(expected_novelty)
        components["predicted_novelty"] = str(semantics.novelty)
        components["told_n"] = len(told_ids)
        if not label_hit:
            notes.append(f"Accepted novelty is {expected_novelty}; you answered {semantics.novelty}.")

    if not scored:
        return _result(
            score=None,
            feedback="No accepted asset or novelty answer on this case.",
            outcome="no_gold",
            components=components,
            objective_scores=objectives,
        )
    return _result(
        score=_mean(scored),
        feedback=" ".join(notes) or "The typed semantics match every accepted answer on this case.",
        outcome="scored",
        components=components,
        objective_scores=objectives,
    )


# --- explanation ----------------------------------------------------------------------------------

EXPLANATION_AXES: Final[tuple[str, ...]] = (
    "typed_card_valid",
    "card_lint_pass_rate",
    "evidence_support",
    "key_facts_covered",
)


def zero_explanation_objectives() -> dict[str, float]:
    return dict.fromkeys(EXPLANATION_AXES, 0.0)


def _card_of(pred: Any) -> Mapping[str, Any] | None:
    card = getattr(pred, "card", None)
    if card is None:
        return None
    if isinstance(card, ReaderCard):
        return card.model_dump(mode="json")
    return ReaderCard.model_validate(card).model_dump(mode="json")


def _literal_hits(needles: Sequence[str], haystack: str) -> list[bool]:
    text = " ".join(str(haystack).split())
    return [" ".join(str(needle).split()) in text for needle in needles]


def explanation_metric(
    gold: Any,
    pred: Any,
    trace: Any = None,
    pred_name: str | None = None,
    pred_trace: Any = None,
    *,
    judge: Any = None,
) -> dspy.Prediction:
    """Score one ReaderCard on the two questions a reviewer can actually answer about a *why*.

    ``support`` is "is every material claim carried by the frozen evidence", asked of the sealed judge.
    ``coverage`` is "did the card keep the facts the reviewer said it must", asked as one batched
    equivalence question over the reviewer's own ``key_facts``.  The score is their F1, so a card that is
    faithful but says nothing and a card that says everything but invents a mechanism both score low, and
    only a card that is both scores high.  ``reference_why_zh`` is never compared against, and
    ``why_value`` — how *useful* the sentence is — never enters the score at all: it is the one judgment
    in the rubric with no accepted answer, and a ruler that scored it would be scoring a preference.

    A stated ``forbidden_claim`` is a zero, not a deduction.  The reviewer named an assertion this card
    must not make; a card that makes it is wrong about the fact, however well it covers the rest.
    """

    del trace, pred_name, pred_trace
    skip = _not_applicable(gold, "explanation")
    if skip is not None:
        return skip
    zero = zero_explanation_objectives()
    failure = _task_output_failure(pred, objectives=zero)
    if failure is not None:
        return failure
    try:
        card = _card_of(pred)
    except ValidationError as exc:
        return _result(
            score=0.0,
            feedback=f"Typed ReaderCard is invalid: {exc}",
            outcome="schema_failure",
            components={"failure": TASK_OUTPUT_INVALID},
            objective_scores=zero,
        )
    if card is None:
        return _result(
            score=0.0,
            feedback="The candidate produced no reader card.",
            outcome="schema_failure",
            components={"failure": TASK_OUTPUT_INVALID},
            objective_scores=zero,
        )
    headline = str(card.get("headline_zh") or "")
    why = str(card.get("why_zh") or "")
    objectives = dict(zero)
    objectives["typed_card_valid"] = 1.0
    components: dict[str, Any] = {"typed_card_valid": True}
    if not why.strip():
        return _result(
            score=0.0,
            feedback="The card has no explanation sentence, so there is nothing for a reader to act on.",
            outcome="scored",
            components={**components, "empty_why_zh": True},
            objective_scores=objectives,
        )
    lint = lint_reader_card(
        headline_zh=headline,
        why_zh=why,
        source_title=str(getattr(gold, "source_title", "") or ""),
    )
    if lint.gate:
        return _result(
            score=0.0,
            feedback=" ".join(lint.feedback) or f"Reader card rejected: {lint.gate}.",
            outcome="scored",
            components={**components, "card_lint_gate": lint.gate},
            objective_scores=objectives,
        )
    lint_rate = 1.0 if lint.score is None else float(lint.score)
    objectives["card_lint_pass_rate"] = lint_rate
    components["card_lint_pass_rate"] = _round(lint_rate)

    evidence_json = str(getattr(gold, "evidence_json", "") or "")
    key_facts = tuple(str(fact) for fact in (getattr(gold, "gold_key_facts", ()) or ()))
    forbidden = tuple(str(claim) for claim in (getattr(gold, "gold_forbidden_claims", ()) or ()))
    error_types = tuple(str(name) for name in (getattr(gold, "gold_error_types", ()) or ()))
    severe = sorted(set(error_types) & SEVERE_ERROR_TYPES)
    components["severe_error_types"] = severe
    components["why_value"] = "never_scored"
    notes: list[str] = []

    # Forbidden claims first: a card that asserts one is wrong whatever else it does, so it is not worth
    # a support call, and on the no-judge arm a literal assertion is the only one that can be proven.
    if forbidden:
        asserted: list[bool] | None
        if judge is None:
            asserted = _literal_hits(forbidden, f"{headline}\n{why}")
        else:
            answer = judge.forbidden_claims_asserted(evidence_json, card, forbidden)
            asserted = None if answer.status == "unavailable" else list(answer.answers or ())
        if asserted is None:
            return _result(
                score=None,
                feedback="The metric judge could not answer whether this card asserts the forbidden claim.",
                outcome="judge_unavailable",
                components={**components, "question": "forbidden_claims"},
            )
        stated = [claim for claim, hit in zip(forbidden, asserted, strict=True) if hit]
        components["forbidden_claims_asserted"] = stated
        if stated:
            return _result(
                score=0.0,
                feedback="The card asserts what the reviewer forbade: " + " | ".join(stated),
                outcome="scored",
                components=components,
                objective_scores=objectives,
            )

    scored: list[float] = []
    support: float | None = None
    if judge is not None and evidence_json:
        assessment = judge.facts_supported(evidence_json, card)
        if assessment.status == "unavailable" or assessment.verdict is None:
            return _result(
                score=None,
                feedback="The metric judge could not answer whether this card is supported by the evidence.",
                outcome="judge_unavailable",
                components={**components, "question": "facts_supported"},
            )
        support = float(assessment.verdict.supported_by_evidence)
        objectives["evidence_support"] = support
        components["evidence_support"] = support
        if not support:
            notes.append(
                "At least one claim in this card is not carried by the evidence; state only what the "
                "source says, with its condition, status and time basis intact."
            )

    coverage: float | None = None
    if key_facts:
        if judge is None:
            covered = _literal_hits(key_facts, f"{headline}\n{why}")
        else:
            answer = judge.key_facts_covered(evidence_json, card, key_facts)
            if answer.status == "unavailable":
                return _result(
                    score=None,
                    feedback="The metric judge could not answer which of the reviewer's key facts this card keeps.",
                    outcome="judge_unavailable",
                    components={**components, "question": "key_facts_covered"},
                )
            covered = list(answer.answers or ())
        missing = [fact for fact, hit in zip(key_facts, covered, strict=True) if not hit]
        coverage = sum(1 for hit in covered if hit) / len(key_facts)
        objectives["key_facts_covered"] = coverage
        components["key_facts_n"] = len(key_facts)
        components["key_facts_covered"] = _round(coverage)
        components["key_facts_missing"] = missing
        if missing:
            notes.append("The card drops facts the reviewer said it must keep: " + " | ".join(missing))

    if support is not None and coverage is not None:
        scored.append(_f1(support, coverage))
        components["score_basis"] = "f1_support_coverage"
    elif support is not None:
        scored.append(support)
        components["score_basis"] = "support_only"
    elif coverage is not None:
        scored.append(coverage)
        components["score_basis"] = "coverage_only"
    else:
        # No judge route and no reviewer key facts: the deterministic copy contract is the only thing this
        # ruler can honestly measure here, and it is code-owned rather than invented Gold. This is the arm
        # the offline optimizer runs on cases whose review states only copy corrections.
        scored.append(lint_rate)
        components["score_basis"] = "card_lint_only"
        notes.extend(lint.feedback)

    return _result(
        score=_mean(scored),
        feedback=" ".join(notes) or "The card is supported by the evidence and keeps every fact the reviewer named.",
        outcome="scored",
        components=components,
        objective_scores=objectives,
    )


# --- binding and denominators ---------------------------------------------------------------------

TARGET_METRIC: Final[dict[str, Callable[..., dspy.Prediction]]] = {
    "classification": classification_metric,
    "understanding": understanding_metric,
    "explanation": explanation_metric,
}

TARGET_AXES: Final[dict[str, tuple[str, ...]]] = {
    "classification": CLASSIFICATION_AXES,
    "understanding": UNDERSTANDING_AXES,
    "explanation": EXPLANATION_AXES,
}

ZERO_OBJECTIVES: Final[dict[str, Callable[[], dict[str, float]]]] = {
    "classification": zero_classification_objectives,
    "understanding": zero_understanding_objectives,
    "explanation": zero_explanation_objectives,
}


def bind_target_metric(target: str, judge: Any = None) -> Callable[..., dspy.Prediction]:
    """One target's ruler with its judge bound, in the exact `(gold, pred, trace, ...)` shape GEPA calls.

    The same `functools.partial` binding `metric.bind_metric` uses, for the same reason: the judge is a
    run-scoped resource with its own budget, and threading it through every call site as an argument is
    how two runs end up judged by two different objects.
    """

    metric = TARGET_METRIC.get(target)
    if metric is None:
        raise ValueError(f"news_learning_target_unknown:{target}")
    return functools.partial(metric, judge=judge)


def target_metric_receipt(
    target: str,
    *,
    review_rubric_version: str,
    judge: Any = None,
    judge_calibration_receipt_sha256: str = "",
) -> dict[str, Any]:
    """What this ruler is, in bytes a later reader can compare two runs with.

    `judge_calibration_receipt_sha256` is the address of the `news learning judge-calibration` run that
    checked this judge. It is optional and never defaulted to a hash of nothing: an explanation number
    published without it is a number whose ruler was not checked, and the receipt says so by carrying an
    empty string rather than by omitting the field.
    """

    if target not in TARGET_METRIC:
        raise ValueError(f"news_learning_target_unknown:{target}")
    scalar = {
        "classification": "mean(stated taxonomy axes)",
        "understanding": "mean(primary_asset_f1?,asset_role_accuracy?,novelty_accuracy?)",
        "explanation": "f1(evidence_support,key_facts_covered)",
    }[target]
    return {
        "schema": "tracefold.news.target_metric.v1",
        "metric_id": f"{TARGET_METRICS_ID}:{target}",
        "target": target,
        "review_rubric_version": review_rubric_version,
        "scalar": scalar,
        "axes": list(TARGET_AXES[target]),
        "outcomes": list(TARGET_OUTCOMES),
        "failure_outcomes": sorted(FAILURE_OUTCOMES),
        "excluded_outcomes": sorted(EXCLUDED_OUTCOMES),
        "judge_unavailable_share_max": JUDGE_UNAVAILABLE_SHARE_MAX,
        "judge": None if judge is None else judge.identity,
        "judge_route": "configured" if judge is not None else "none_deterministic_arm",
        "judge_calibration_receipt_sha256": str(judge_calibration_receipt_sha256 or ""),
        "invalid_prediction_score": 0.0,
        "truncated_output_score": 0.0,
        "why_value_scored": False,
        "reference_why_zh_scored": False,
    }


def summarize_target_outcomes(
    rows: Sequence[Mapping[str, Any]],
    *,
    target: str,
) -> dict[str, Any]:
    """Turn one run's per-case outcomes into the denominators a report publishes (#651 §8).

    `rows` carry `outcome`, an optional `score`, and an optional `stratum`. Every count below is over the
    same population and they add up: `applicable_n = scored_n + failure_n + no_gold_n +
    judge_unavailable_n + retrieval_miss_n`, and `not_applicable_n` sits outside it because the corpus
    never posed the question.

    `evaluation_unavailable` is the one verdict this summary reaches on its own: a run that could not ask
    its judge on more than `JUDGE_UNAVAILABLE_SHARE_MAX` of the applicable cases has not measured the
    target, and a caller that reads `score` without reading this flag would publish the reachable cases
    as if they were the corpus.
    """

    if target not in TARGET_METRIC:
        raise ValueError(f"news_learning_target_unknown:{target}")
    counts = dict.fromkeys(TARGET_OUTCOMES, 0)
    scores: list[float] = []
    strata: dict[str, int] = {}
    for row in rows:
        outcome = str(row.get("outcome") or "")
        if outcome not in counts:
            raise ValueError(f"news_learning_target_outcome_unknown:{outcome}")
        counts[outcome] += 1
        stratum = str(row.get("stratum") or "")
        if stratum and outcome != "not_applicable":
            strata[stratum] = strata.get(stratum, 0) + 1
        score = row.get("score")
        if outcome == "scored" and score is not None:
            scores.append(float(score))
        elif outcome in FAILURE_OUTCOMES:
            scores.append(0.0)
    applicable_n = len(rows) - counts["not_applicable"]
    failure_n = sum(counts[name] for name in FAILURE_OUTCOMES)
    judge_unavailable_n = counts["judge_unavailable"]
    share = judge_unavailable_n / applicable_n if applicable_n else 0.0
    return {
        "schema": "tracefold.news.target_denominators.v1",
        "target": target,
        "case_n": len(rows),
        "applicable_n": applicable_n,
        "not_applicable_n": counts["not_applicable"],
        "scored_n": counts["scored"],
        "failure_n": failure_n,
        "failures": {name: counts[name] for name in sorted(FAILURE_OUTCOMES) if counts[name]},
        "no_gold_n": counts["no_gold"],
        "judge_unavailable_n": judge_unavailable_n,
        "retrieval_miss_n": counts["retrieval_miss"],
        "judge_unavailable_share": _round(share),
        "judge_unavailable_share_max": JUDGE_UNAVAILABLE_SHARE_MAX,
        # Read by the release gate, which must never turn a judge outage into a pass or a fail.
        "evaluation_unavailable": bool(share > JUDGE_UNAVAILABLE_SHARE_MAX),
        # The mean over what was actually measured, with the candidate's own failures at zero. Excluded
        # outcomes are absent from it by construction, which is the whole point of the counts above.
        "score": None if not scores else _round(_mean(scores)),
        "strata": dict(sorted(strata.items())),
    }


__all__ = [
    "CLASSIFICATION_AXES",
    "EXCLUDED_OUTCOMES",
    "EXPLANATION_AXES",
    "FAILURE_OUTCOMES",
    "JUDGE_UNAVAILABLE_SHARE_MAX",
    "SEVERE_ERROR_TYPES",
    "TARGET_AXES",
    "TARGET_METRIC",
    "TARGET_METRICS_ID",
    "TARGET_OUTCOMES",
    "TASK_OUTPUT_INVALID",
    "TASK_OUTPUT_TRUNCATED",
    "UNDERSTANDING_AXES",
    "ZERO_OBJECTIVES",
    "asset_grounding_outcome",
    "bind_target_metric",
    "classification_axis_values",
    "classification_metric",
    "classification_score",
    "explanation_metric",
    "summarize_target_outcomes",
    "target_metric_receipt",
    "understanding_metric",
    "zero_classification_objectives",
    "zero_explanation_objectives",
    "zero_understanding_objectives",
]
