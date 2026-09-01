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
    ModelTaxonomyV1,
    NewsTaxonomyV1,
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
    gold: ModelTaxonomyV1 | Mapping[str, Any],
    predicted: ModelTaxonomyV1 | Mapping[str, Any],
) -> TaxonomyComparison:
    """Score the four model-owned axes; code-owned source authority is intentionally unread."""

    accepted = gold if isinstance(gold, ModelTaxonomyV1) else ModelTaxonomyV1.model_validate(gold)
    if isinstance(predicted, ModelTaxonomyV1):
        observed = predicted
    elif "source_authority" in predicted:
        observed = NewsTaxonomyV1.model_validate(predicted)
    else:
        observed = ModelTaxonomyV1.model_validate(predicted)
    gold_subjects = frozenset(accepted.subject_codes)
    predicted_subjects = frozenset(observed.subject_codes)
    subject_f1 = _subject_f1(gold_subjects, predicted_subjects)
    axis_matches = {
        "event_family": accepted.event_family == observed.event_family,
        "change_state": accepted.change_state == observed.change_state,
        "assertion_status": accepted.assertion_status == observed.assertion_status,
    }
    missing = tuple(sorted(gold_subjects - predicted_subjects))
    extra = tuple(sorted(predicted_subjects - gold_subjects))
    wrong_axes = tuple(axis for axis, match in axis_matches.items() if not match)
    score = round((subject_f1 + sum(axis_matches.values())) / 4, 6)
    feedback: list[str] = []
    if missing:
        feedback.append("missing subjects: " + ", ".join(missing))
    if extra:
        feedback.append("extra subjects: " + ", ".join(extra))
    if wrong_axes:
        feedback.append(
            "wrong axes: "
            + ", ".join(
                f"{axis} expected={getattr(accepted, axis)} predicted={getattr(observed, axis)}" for axis in wrong_axes
            )
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


def summarize_taxonomy(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate deterministic taxonomy quality with one vote per connected fact cluster."""

    representatives: dict[str, tuple[str, ModelTaxonomyV1, NewsTaxonomyV1]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "")
        cluster_id = str(row.get("cluster_id") or "")
        if not case_id or not cluster_id:
            raise ValueError("news_taxonomy_summary_identity_missing")
        gold = ModelTaxonomyV1.model_validate(row.get("gold"))
        predicted = NewsTaxonomyV1.model_validate(row.get("predicted"))
        previous = representatives.get(cluster_id)
        if previous is not None and previous[1] != gold:
            raise ValueError(f"news_taxonomy_summary_cluster_conflict:{cluster_id}")
        if previous is None or case_id < previous[0]:
            representatives[cluster_id] = (case_id, gold, predicted)

    comparisons = [compare_taxonomy(gold, predicted) for _case, gold, predicted in representatives.values()]
    count = len(comparisons)

    def mean(values: Sequence[float]) -> float | None:
        return None if not values else round(sum(values) / len(values), 6)

    support_values: dict[str, Counter[str]] = {
        "subject_codes": Counter(
            code for _case, gold, _predicted in representatives.values() for code in gold.subject_codes
        ),
        "event_family": Counter(gold.event_family for _case, gold, _predicted in representatives.values()),
        "change_state": Counter(gold.change_state for _case, gold, _predicted in representatives.values()),
        "assertion_status": Counter(gold.assertion_status for _case, gold, _predicted in representatives.values()),
    }
    legal: dict[str, Sequence[str]] = {
        "subject_codes": IPTC_SUBJECT_CODES,
        "event_family": EVENT_FAMILIES,
        "change_state": CHANGE_STATES,
        "assertion_status": ASSERTION_STATUSES,
    }
    confusion: dict[str, list[dict[str, Any]]] = {}
    for axis in ("event_family", "change_state", "assertion_status"):
        pairs = Counter(
            (str(getattr(gold, axis)), str(getattr(predicted, axis)))
            for _case, gold, predicted in representatives.values()
        )
        confusion[axis] = [
            {"gold": gold, "predicted": predicted, "n": n} for (gold, predicted), n in sorted(pairs.items())
        ]
    return {
        "schema": "tracefold.news.taxonomy_summary.v1",
        "case_n": len(rows),
        "cluster_n": count,
        "shadowed_case_n": len(rows) - count,
        "taxonomy_overall": mean([comparison.score for comparison in comparisons]),
        "subject_codes_set_f1": mean([comparison.subject_f1 for comparison in comparisons]),
        "event_family_accuracy": mean([float(comparison.event_family_match) for comparison in comparisons]),
        "change_state_accuracy": mean([float(comparison.change_state_match) for comparison in comparisons]),
        "assertion_status_accuracy": mean([float(comparison.assertion_status_match) for comparison in comparisons]),
        "four_axis_exact_accuracy": mean([float(comparison.exact) for comparison in comparisons]),
        "support": {
            axis: {label: counts[label] for label in labels if counts[label]}
            for axis, labels in legal.items()
            for counts in (support_values[axis],)
        },
        "zero_support": {
            axis: [label for label in labels if not support_values[axis][label]] for axis, labels in legal.items()
        },
        "confusion": confusion,
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
    """Inter-reviewer agreement for one source-only, one-cluster-one-vote calibration set."""

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
    "summarize_taxonomy",
]
