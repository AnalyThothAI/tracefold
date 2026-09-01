from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.learning.taxonomy_metric import calibrate_taxonomy, compare_taxonomy, summarize_taxonomy


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


def test_taxonomy_summary_counts_one_vote_per_contract_cluster_and_exposes_blind_spots() -> None:
    rows = [
        {
            "case_id": "case-b",
            "cluster_id": "cluster-1",
            "gold": _gold(event_family="financial_results", change_state="reported", assertion_status="confirmed"),
            "predicted": _prediction(
                event_family="financial_results", change_state="reported", assertion_status="confirmed"
            ),
        },
        {
            "case_id": "case-a",
            "cluster_id": "cluster-1",
            "gold": _gold(event_family="financial_results", change_state="reported", assertion_status="confirmed"),
            "predicted": _prediction(
                event_family="financial_results", change_state="reported", assertion_status="confirmed"
            ),
        },
        {
            "case_id": "case-c",
            "cluster_id": "cluster-2",
            "gold": _gold(
                subject_codes=["medtop:20001279"],
                event_family="market_access",
                change_state="announced",
                assertion_status="claimed",
            ),
            "predicted": _prediction(
                subject_codes=[],
                event_family="other",
                change_state="announced",
                assertion_status="rumor",
            ),
        },
    ]

    summary = summarize_taxonomy(rows)

    assert summary["case_n"] == 3
    assert summary["cluster_n"] == 2
    assert summary["shadowed_case_n"] == 1
    assert summary["taxonomy_overall"] == 0.625
    assert summary["subject_codes_set_f1"] == 0.5
    assert summary["event_family_accuracy"] == 0.5
    assert summary["change_state_accuracy"] == 1.0
    assert summary["assertion_status_accuracy"] == 0.5
    assert summary["four_axis_exact_accuracy"] == 0.5
    assert summary["support"]["event_family"] == {"financial_results": 1, "market_access": 1}
    assert "guidance_outlook" in summary["zero_support"]["event_family"]
    assert "cancelled" in summary["zero_support"]["change_state"]
    assert "conflicted" in summary["zero_support"]["assertion_status"]
    assert summary["confusion"]["event_family"] == [
        {"gold": "financial_results", "predicted": "financial_results", "n": 1},
        {"gold": "market_access", "predicted": "other", "n": 1},
    ]


def test_taxonomy_summary_rejects_conflicting_rows_for_one_cluster() -> None:
    with pytest.raises(ValueError, match="news_taxonomy_summary_cluster_conflict"):
        summarize_taxonomy(
            [
                {
                    "case_id": "case-a",
                    "cluster_id": "cluster-1",
                    "gold": _gold(event_family="other"),
                    "predicted": _prediction(event_family="other"),
                },
                {
                    "case_id": "case-b",
                    "cluster_id": "cluster-1",
                    "gold": _gold(event_family="financial_results"),
                    "predicted": _prediction(event_family="financial_results"),
                },
            ]
        )


def test_taxonomy_summary_elects_one_deterministic_prediction_per_cluster() -> None:
    summary = summarize_taxonomy(
        [
            {
                "case_id": "case-b",
                "cluster_id": "cluster-1",
                "gold": _gold(event_family="financial_results"),
                "predicted": _prediction(event_family="other"),
            },
            {
                "case_id": "case-a",
                "cluster_id": "cluster-1",
                "gold": _gold(event_family="financial_results"),
                "predicted": _prediction(event_family="financial_results"),
            },
        ]
    )

    assert summary["cluster_n"] == 1
    assert summary["shadowed_case_n"] == 1
    assert summary["event_family_accuracy"] == 1.0


def test_calibration_reports_kappa_and_subject_set_f1_per_contract_cluster() -> None:
    rows = [
        {
            "task_id": f"task-{index}",
            "cluster_id": f"cluster-{index}",
            "reviewer_a": _gold(event_family="financial_results" if index else "other"),
            "reviewer_b": _gold(event_family="financial_results"),
        }
        for index in range(4)
    ]

    receipt = calibrate_taxonomy(rows)

    assert receipt["cluster_n"] == 4
    assert receipt["kappa"]["event_family"] == 0.0
    assert receipt["kappa"]["change_state"] == 1.0
    assert receipt["subject_mean_set_f1"] == 1.0
