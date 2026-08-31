from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.learning.taxonomy_metric import compare_taxonomy


def _gold(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "subject_codes": [],
        "event_family": "other",
        "change_state": "unknown",
        "assertion_status": "unknown",
    }
    value.update(updates)
    return value


def _prediction(**updates: Any) -> dict[str, Any]:
    value = {**_gold(), "source_authority": "unknown"}
    value.update(updates)
    return value


@pytest.mark.parametrize(
    ("gold_subjects", "predicted_subjects", "expected"),
    [
        ([], [], 1.0),
        (["medtop:20000178"], [], 0.0),
        ([], ["medtop:20000178"], 0.0),
        (
            ["medtop:20000178", "medtop:20001279"],
            ["medtop:20001279", "medtop:20001164"],
            0.5,
        ),
    ],
)
def test_subject_set_f1_edges(gold_subjects: list[str], predicted_subjects: list[str], expected: float) -> None:
    comparison = compare_taxonomy(
        _gold(subject_codes=gold_subjects),
        _prediction(subject_codes=predicted_subjects),
    )

    assert comparison.subject_f1 == expected


def test_four_axis_score_and_feedback_come_from_one_comparison() -> None:
    comparison = compare_taxonomy(
        _gold(
            subject_codes=["medtop:20000178", "medtop:20001279"],
            event_family="financial_results",
            change_state="reported",
            assertion_status="confirmed",
        ),
        _prediction(
            subject_codes=["medtop:20001279", "medtop:20001164"],
            event_family="market_access",
            change_state="reported",
            assertion_status="claimed",
            source_authority="regulatory_filing",
        ),
    )

    assert comparison.score == 0.375
    assert comparison.missing_subjects == ("medtop:20000178",)
    assert comparison.extra_subjects == ("medtop:20001164",)
    assert comparison.wrong_axes == ("event_family", "assertion_status")
    assert "missing subjects: medtop:20000178" in comparison.feedback
    assert "extra subjects: medtop:20001164" in comparison.feedback
    assert "event_family" in comparison.feedback and "assertion_status" in comparison.feedback
    assert "source_authority" not in comparison.feedback


def test_source_authority_never_changes_the_model_score_or_feedback() -> None:
    gold = _gold(event_family="financial_results")
    first = compare_taxonomy(gold, _prediction(event_family="other", source_authority="unknown"))
    second = compare_taxonomy(
        gold,
        _prediction(event_family="other", source_authority="issuer_first_party"),
    )

    assert first == second
