from __future__ import annotations

from typing import Any

from tests.support.news_judgment import news_taxonomy
from tracefold.news.learning.evaluate import _taxonomy_release_evidence


def _taxonomy(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "subject_codes": ["medtop:20000205"],
        "event_family": "product_service_change",
        "change_state": "announced",
        "assertion_status": "confirmed",
    }
    values.update(overrides)
    return values


def _observation(
    index: int,
    *,
    stable: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    def arm(taxonomy: dict[str, Any]) -> dict[str, Any]:
        return {
            "editorial": {
                "taxonomy": news_taxonomy(
                    **taxonomy,
                    source_authority="reputable_secondary",
                ).model_dump(mode="json")
            }
        }

    return {
        "case_ref": {
            "case_id": f"case-{index}",
            "cluster_id": f"cluster-{index}",
            "review_id": f"review-{index}",
        },
        "stable": arm(stable),
        "candidate": arm(candidate),
    }


def test_release_taxonomy_evidence_blocks_any_axis_and_stable_control_regression() -> None:
    gold = _taxonomy()
    evidence = _taxonomy_release_evidence(
        [_observation(1, stable=gold, candidate=_taxonomy(event_family="other"))],
        {"review-1": {"payload": {"taxonomy": gold, "first_bad_owner": None}}},
    )

    assert evidence["regressed_axes"] == ["event_family_accuracy", "four_axis_exact_accuracy"]
    assert evidence["stable_correct_control_cluster_n"] == 1
    assert evidence["stable_correct_control_regression_n"] == 1
    assert evidence["stable_correct_control_regression_cluster_ids"] == ["cluster-1"]
    assert evidence["delta"]["event_family_accuracy"] == -1.0


def test_release_taxonomy_evidence_allows_target_improvement_without_control_regression() -> None:
    gold = _taxonomy()
    observations = [
        _observation(1, stable=_taxonomy(event_family="other"), candidate=gold),
        _observation(2, stable=gold, candidate=gold),
    ]
    evidence = _taxonomy_release_evidence(
        observations,
        {
            "review-1": {"payload": {"taxonomy": gold, "first_bad_owner": "taxonomy"}},
            "review-2": {"payload": {"taxonomy": gold, "first_bad_owner": None}},
        },
    )

    assert evidence["regressed_axes"] == []
    assert evidence["stable_correct_control_cluster_n"] == 1
    assert evidence["stable_correct_control_regression_n"] == 0
    assert evidence["candidate"]["four_axis_exact_accuracy"] == 1.0
    assert evidence["candidate"]["taxonomy_overall"] > evidence["stable"]["taxonomy_overall"]


def test_explicitly_owned_exact_case_is_not_misreported_as_a_control() -> None:
    gold = _taxonomy()
    evidence = _taxonomy_release_evidence(
        [_observation(1, stable=gold, candidate=_taxonomy(event_family="other"))],
        {"review-1": {"payload": {"taxonomy": gold, "first_bad_owner": "taxonomy"}}},
    )

    assert evidence["stable_correct_control_cluster_n"] == 0
    assert evidence["stable_correct_control_regression_n"] == 0
