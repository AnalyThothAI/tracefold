"""Pure accepted-Gold versus predicted taxonomy comparison."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from ..taxonomy import ModelTaxonomyV1, NewsTaxonomyV1

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
    predicted: NewsTaxonomyV1 | Mapping[str, Any],
) -> TaxonomyComparison:
    """Score the four model-owned axes; code-owned source authority is intentionally unread."""

    accepted = gold if isinstance(gold, ModelTaxonomyV1) else ModelTaxonomyV1.model_validate(gold)
    observed = predicted if isinstance(predicted, NewsTaxonomyV1) else NewsTaxonomyV1.model_validate(predicted)
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


__all__ = [
    "TAXONOMY_AXES",
    "TAXONOMY_TARGET_DIMENSIONS",
    "TaxonomyComparison",
    "compare_taxonomy",
]
