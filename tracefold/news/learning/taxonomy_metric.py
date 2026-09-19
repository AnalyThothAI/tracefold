"""Pure accepted-Gold versus predicted taxonomy comparison."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from ..taxonomy import (
    ASSERTION_STATUSES,
    CHANGE_STATES,
    EVENT_FAMILIES,
    IPTC_SUBJECT_CODES,
    IPTC_SUBJECT_LABELS_EN,
    ModelTaxonomyV1,
    NewsTaxonomyV1,
    ReviewTaxonomyV1,
    precedence_rules_for,
    subject_code_precedence_rules,
    taxonomy_definition,
)

TAXONOMY_AXES: Final = ("subject_codes", "event_family", "change_state", "assertion_status")
TAXONOMY_TARGET_DIMENSIONS: Final = tuple(f"taxonomy.{axis}" for axis in TAXONOMY_AXES)


@dataclass(frozen=True, slots=True)
class TaxonomyComparison:
    score: float
    subject_f1: float
    event_family_match: bool
    change_state_match: bool
    assertion_status_match: bool
    missing_subjects: tuple[str, ...]
    extra_subjects: tuple[str, ...]
    wrong_axes: tuple[str, ...]
    feedback: str

    @property
    def exact(self) -> bool:
        return self.score == 1.0


def _subject_f1(gold: frozenset[str], predicted: frozenset[str]) -> float:
    if not gold and not predicted:
        return 1.0
    if not gold or not predicted:
        return 0.0
    return 2 * len(gold & predicted) / (len(gold) + len(predicted))


def compare_taxonomy(
    gold: ModelTaxonomyV1 | ReviewTaxonomyV1 | Mapping[str, Any],
    predicted: ModelTaxonomyV1 | Mapping[str, Any],
) -> TaxonomyComparison:
    """Score the four model-owned axes; code-owned source authority is intentionally unread."""

    accepted = gold if isinstance(gold, (ModelTaxonomyV1, ReviewTaxonomyV1)) else ReviewTaxonomyV1.model_validate(gold)
    stated = accepted.model_dump(exclude_none=True)
    if isinstance(predicted, ModelTaxonomyV1):
        observed = predicted
    elif "taxonomy_version" in predicted:
        # The persisted/read shape: four axes plus the codebook identity. Since #651 that is all it is --
        # the code-owned source authority moved to the editorial envelope beside it.
        observed = NewsTaxonomyV1.model_validate(predicted)
    else:
        observed = ModelTaxonomyV1.model_validate(predicted)
    gold_subjects = frozenset(accepted.subject_codes or ())
    predicted_subjects = frozenset(observed.subject_codes)
    subject_f1 = _subject_f1(gold_subjects, predicted_subjects) if "subject_codes" in stated else 1.0
    axis_matches = {
        "event_family": "event_family" not in stated or accepted.event_family == observed.event_family,
        "change_state": "change_state" not in stated or accepted.change_state == observed.change_state,
        "assertion_status": "assertion_status" not in stated or accepted.assertion_status == observed.assertion_status,
    }
    missing = tuple(sorted(gold_subjects - predicted_subjects)) if "subject_codes" in stated else ()
    extra = tuple(sorted(predicted_subjects - gold_subjects)) if "subject_codes" in stated else ()
    wrong_axes = tuple(axis for axis, match in axis_matches.items() if not match)
    score = round(
        sum(subject_f1 if axis == "subject_codes" else float(axis_matches[axis]) for axis in stated) / len(stated), 6
    )
    # Feedback quotes the codebook (#501 D3): the definition of what was expected and of what was
    # predicted, and any precedence rule written for exactly that confusion, so the reflection model
    # reads the rule the seed already states instead of inventing one from the minibatch's titles.
    feedback: list[str] = []
    if missing:
        feedback.append(
            "missing subjects: " + ", ".join(f"{code} ({IPTC_SUBJECT_LABELS_EN[code]})" for code in missing)
        )
    if extra:
        feedback.append("extra subjects: " + ", ".join(f"{code} ({IPTC_SUBJECT_LABELS_EN[code]})" for code in extra))
    # #567: subject codes are an axis like the other three, so the codebook rule written for a code miss —
    # the broad parent answered where one of its descendants was expected — is quoted the same way.
    feedback.extend(f"rule (subject_codes): {rule}" for rule in subject_code_precedence_rules(missing, extra))
    for axis in wrong_axes:
        expected_label = str(getattr(accepted, axis))
        predicted_label = str(getattr(observed, axis))
        feedback.append(
            f"{axis}: expected={expected_label} ({taxonomy_definition(axis, expected_label)}); "
            f"predicted={predicted_label} ({taxonomy_definition(axis, predicted_label)})"
        )
        feedback.extend(
            f"rule ({axis}): {rule}" for rule in precedence_rules_for(axis, expected_label, predicted_label)
        )
    return TaxonomyComparison(
        score=score,
        subject_f1=round(subject_f1, 6),
        event_family_match=axis_matches["event_family"],
        change_state_match=axis_matches["change_state"],
        assertion_status_match=axis_matches["assertion_status"],
        missing_subjects=missing,
        extra_subjects=extra,
        wrong_axes=wrong_axes,
        feedback="; ".join(feedback) or "Taxonomy matches accepted Gold.",
    )


def model_taxonomy(value: Any) -> ModelTaxonomyV1:
    """Read one taxonomy as its four model axes, whatever wider shape the caller happened to hold.

    Public because every ruler needs it: the persisted shape carries `taxonomy_version` and
    `codebook_sha256` beside the four axes, and `ModelTaxonomyV1` forbids extras, so a ruler that
    validated the stored mapping directly reported a schema failure on a perfectly good label."""

    if isinstance(value, ModelTaxonomyV1):
        return ModelTaxonomyV1.model_validate({field: getattr(value, field) for field in ModelTaxonomyV1.model_fields})
    axes = dict(value or {})
    return ModelTaxonomyV1.model_validate(
        {field: axes[field] for field in ModelTaxonomyV1.model_fields if field in axes}
    )


def summarize_taxonomy(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Score every labelled case; report cases and independent split groups separately."""
    legal = {
        "subject_codes": IPTC_SUBJECT_CODES,
        "event_family": EVENT_FAMILIES,
        "change_state": CHANGE_STATES,
        "assertion_status": ASSERTION_STATUSES,
    }
    support: dict[str, Counter[str]] = {axis: Counter() for axis in TAXONOMY_AXES}
    confusion: dict[str, Counter[tuple[str, str]]] = {axis: Counter() for axis in TAXONOMY_AXES[1:]}
    values: dict[str, list[float]] = {axis: [] for axis in TAXONOMY_AXES}
    axis_groups: dict[str, dict[str, list[float]]] = {axis: {} for axis in TAXONOMY_AXES}
    exact_groups: dict[str, list[float]] = {}
    overall: list[float] = []
    exact: list[float] = []
    groups: dict[str, list[float]] = {}
    for row in rows:
        if not row.get("case_id") or not row.get("cluster_id"):
            raise ValueError("news_taxonomy_summary_identity_missing")
        raw = row["gold"]
        raw = raw.model_dump(exclude_none=True) if isinstance(raw, (ModelTaxonomyV1, ReviewTaxonomyV1)) else raw
        gold = ReviewTaxonomyV1.model_validate(raw)
        stated = gold.model_dump(exclude_none=True)
        predicted = model_taxonomy(row["predicted"])
        comparison = compare_taxonomy(gold, predicted)
        overall.append(comparison.score)
        groups.setdefault(str(row["cluster_id"]), []).append(comparison.score)
        if len(stated) == 4:
            exact.append(float(comparison.exact))
            exact_groups.setdefault(str(row["cluster_id"]), []).append(float(comparison.exact))
        for axis, label in stated.items():
            if axis == "subject_codes":
                support[axis].update(label)
                values[axis].append(comparison.subject_f1)
            else:
                support[axis].update([label])
                observed = str(getattr(predicted, axis))
                confusion[axis][(label, observed)] += 1
                values[axis].append(float(label == observed))
            axis_groups[axis].setdefault(str(row["cluster_id"]), []).append(values[axis][-1])

    def mean(items: Sequence[float]) -> float | None:
        return round(sum(items) / len(items), 6) if items else None

    def group_mean(items: Mapping[str, Sequence[float]]) -> float | None:
        return mean([sum(group) / len(group) for group in items.values()])

    return {
        "schema": "tracefold.news.taxonomy_summary.v3",
        "case_n": len(rows),
        "cluster_n": len(groups),
        "shadowed_case_n": 0,
        "taxonomy_overall": group_mean(groups),
        "case_mean": mean(overall),
        "group_mean": group_mean(groups),
        "subject_codes_set_f1": group_mean(axis_groups["subject_codes"]),
        **{f"{axis}_accuracy": group_mean(axis_groups[axis]) for axis in TAXONOMY_AXES[1:]},
        "four_axis_exact_accuracy": group_mean(exact_groups),
        "axis_cluster_n": {axis: len(items) for axis, items in axis_groups.items()},
        "axis_case_n": {axis: len(items) for axis, items in values.items()},
        "support": {axis: dict(support[axis]) for axis in legal},
        "zero_support": {
            axis: [label for label in labels if not support[axis][label]] for axis, labels in legal.items()
        },
        "confusion": {
            axis: [{"gold": gold, "predicted": pred, "n": n} for (gold, pred), n in sorted(pairs.items())]
            for axis, pairs in confusion.items()
        },
    }


def _cohen_kappa(left: Sequence[str], right: Sequence[str]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("news_taxonomy_calibration_pairs_invalid")
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
    left_counts = Counter(left)
    right_counts = Counter(right)
    expected = sum(left_counts[label] * right_counts[label] for label in left_counts | right_counts) / len(left) ** 2
    return 1.0 if expected == 1.0 and observed == 1.0 else round((observed - expected) / (1 - expected), 6)


def calibrate_taxonomy(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Inter-drafter agreement for one source-only, one-cluster-one-vote dual-labelled set.

    Reported, never gated (#501 D8): the freeze publishes it beside the corpus so an operator can decide
    to repair the codebook before spending a run, and the holdout remains the gate.
    """

    representatives: dict[str, tuple[str, ModelTaxonomyV1, ModelTaxonomyV1]] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        cluster_id = str(row.get("cluster_id") or "")
        if not task_id or not cluster_id:
            raise ValueError("news_taxonomy_calibration_identity_missing")
        left = ModelTaxonomyV1.model_validate(row.get("reviewer_a"))
        right = ModelTaxonomyV1.model_validate(row.get("reviewer_b"))
        if cluster_id in representatives:
            raise ValueError(f"news_taxonomy_calibration_cluster_duplicate:{cluster_id}")
        representatives[cluster_id] = (task_id, left, right)
    if not representatives:
        raise ValueError("news_taxonomy_calibration_empty")
    pairs = list(representatives.values())
    return {
        "schema": "tracefold.news.taxonomy_calibration.v1",
        "task_n": len(rows),
        "cluster_n": len(pairs),
        "kappa": {
            axis: _cohen_kappa(
                [str(getattr(left, axis)) for _task, left, _right in pairs],
                [str(getattr(right, axis)) for _task, _left, right in pairs],
            )
            for axis in ("event_family", "change_state", "assertion_status")
        },
        "subject_mean_set_f1": round(
            sum(
                _subject_f1(frozenset(left.subject_codes), frozenset(right.subject_codes))
                for _task, left, right in pairs
            )
            / len(pairs),
            6,
        ),
    }


__all__ = [
    "TAXONOMY_AXES",
    "TAXONOMY_TARGET_DIMENSIONS",
    "TaxonomyComparison",
    "calibrate_taxonomy",
    "compare_taxonomy",
    "model_taxonomy",
    "summarize_taxonomy",
]
