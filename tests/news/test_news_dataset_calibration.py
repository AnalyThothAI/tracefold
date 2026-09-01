from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.news.learning import dataset as dataset_module
from tracefold.news.learning.contracts import DatasetCaseRef
from tracefold.news.learning.dataset import DevelopmentDatasetStore

_GOLD = {
    "subject_codes": ["medtop:20000205"],
    "event_family": "product_service_change",
    "change_state": "announced",
    "assertion_status": "confirmed",
}


class _Repository:
    def __init__(self, *, duplicate_reviewer: bool = False) -> None:
        self.duplicate_reviewer = duplicate_reviewer

    def reviews_by_id(self, review_ids: list[str]) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for review_id in review_ids:
            index = 0 if review_id == "unrelated-review" else int(review_id.split("-")[1])
            records[review_id] = {
                "review_id": review_id,
                "task_id": f"task-{index}",
                "task_version": "b" * 64,
            }
        return records

    def review(self, review_id: str) -> dict[str, Any]:
        index = int(review_id.split("-")[1])
        return {
            "review_id": review_id,
            "subject_kind": "event",
            "event_id": f"event-{index}",
            "evidence_version": 1,
        }

    def review_task_source(self, *, event_id: str, evidence_version: int, **_identities: Any) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "evidence_version": evidence_version,
            "evidence_sha256": "a" * 64,
            "evidence_snapshot": {"focus_fact": {"fact_id": "fact-0"}},
            "focus_fact_id": "fact-0",
            "verdict_evidence_version": evidence_version,
            "trace": {
                "evidence_version": evidence_version,
                "evidence_sha256": "a" * 64,
                "focus_fact_id": "fact-0",
            },
            "verdict": None,
            "model_editorial": None,
            "opened_at_ms": 1,
            "settled_at_ms": None,
        }

    def event_task_reviews(self, *, task_id: str, task_version: str) -> tuple[dict[str, Any], ...]:
        del task_version
        index = int(task_id.removeprefix("task-"))

        def review(suffix: str, reviewer: str) -> dict[str, Any]:
            return {
                "review_id": f"review-{index}-{suffix}",
                "reviewer": reviewer,
                "payload": {
                    "taxonomy": _GOLD,
                    "taxonomy_review": {"review_role": "primary"},
                },
            }

        return (
            review("a", "reviewer-a"),
            review("b", "reviewer-a" if self.duplicate_reviewer else "reviewer-b"),
        )


def _source_only(row: dict[str, Any]) -> dict[str, Any]:
    index = int(str(row["event_id"]).removeprefix("event-"))
    return {
        "projection_sha256": "c" * 64,
        "task": {
            "task_id": f"task-{index}",
            "task_version": "b" * 64,
            "event_id": row["event_id"],
            "evidence_version": row["evidence_version"],
        },
    }


def _cases() -> tuple[DatasetCaseRef, ...]:
    return tuple(
        DatasetCaseRef(
            case_id=f"case-{index}",
            subject_kind="event",
            event_id=f"event-{index}",
            evidence_version=1,
            evidence_sha256="a" * 64,
            review_id=f"review-{index}-b",
            cluster_id=f"cluster-{index}",
            stratum="taxonomy_calibration",
            should_push="uncertain",
            opened_at_ms=index,
        )
        for index in range(50)
    )


def _request() -> dict[str, Any]:
    return {"tasks": [{"task_id": f"task-{index}", "source_projection_sha256": "c" * 64} for index in range(50)]}


def _store(repository: _Repository) -> DevelopmentDatasetStore:
    store = object.__new__(DevelopmentDatasetStore)
    store._repository = repository
    store._stable = SimpleNamespace(
        program_version="program",
        program_sha256="d" * 64,
        bundle_sha="e" * 64,
    )
    return store


def test_calibration_receipt_requires_and_counts_fifty_independent_source_only_pairs(monkeypatch: Any) -> None:
    monkeypatch.setattr(dataset_module, "source_only_event_projection", _source_only)

    receipt = _store(_Repository())._calibration_receipt(_request(), _cases())

    assert len(receipt["tasks"]) == 50
    assert receipt["statistics"]["cluster_n"] == 50
    assert receipt["statistics"]["task_n"] == 50
    assert receipt["statistics"]["subject_mean_set_f1"] == 1.0
    assert receipt["statistics"]["kappa"] == {
        "event_family": 1.0,
        "change_state": 1.0,
        "assertion_status": 1.0,
    }
    assert receipt["statistics"]["disagreement_unadjudicated_n"] == 0
    assert all(set(task) >= {"source_projection_sha256", "primary_reviewers"} for task in receipt["tasks"])


def test_calibration_rejects_two_labels_from_the_same_reviewer(monkeypatch: Any) -> None:
    monkeypatch.setattr(dataset_module, "source_only_event_projection", _source_only)

    with pytest.raises(ValueError, match="news_learning_calibration_two_primary_reviewers_required"):
        _store(_Repository(duplicate_reviewer=True))._calibration_receipt(_request(), _cases())


def test_calibration_request_cannot_carry_labels_or_duplicate_contract_clusters(monkeypatch: Any) -> None:
    monkeypatch.setattr(dataset_module, "source_only_event_projection", _source_only)
    request = _request() | {"labels": [_GOLD]}

    with pytest.raises(ValueError, match="news_learning_calibration_request_fields_invalid"):
        _store(_Repository())._calibration_receipt(request, _cases())

    cases = list(_cases())
    cases[1] = cases[1].model_copy(update={"cluster_id": cases[0].cluster_id})
    with pytest.raises(ValueError, match="news_learning_calibration_cluster_duplicate"):
        _store(_Repository())._calibration_receipt(_request(), tuple(cases))


def test_calibration_consensus_must_be_the_dataset_gold(monkeypatch: Any) -> None:
    monkeypatch.setattr(dataset_module, "source_only_event_projection", _source_only)
    cases = list(_cases())
    cases[0] = cases[0].model_copy(update={"review_id": "unrelated-review"})

    with pytest.raises(ValueError, match="news_learning_calibration_consensus_not_dataset_gold"):
        _store(_Repository())._calibration_receipt(_request(), tuple(cases))


def test_load_case_rejects_review_evidence_and_verdict_identity_tampering() -> None:
    case = _cases()[0]
    store = _store(_Repository())

    with pytest.raises(ValueError, match="news_learning_review_identity_mismatch"):
        store.load_case(case.model_copy(update={"event_id": "different-event"}))
    with pytest.raises(ValueError, match="news_learning_evidence_changed"):
        store.load_case(case.model_copy(update={"evidence_sha256": "f" * 64}))

    class _VerdictMismatch(_Repository):
        def review_task_source(self, **values: Any) -> dict[str, Any]:
            row = super().review_task_source(**values)
            row["trace"] = dict(row["trace"], evidence_version=2)
            return row

    with pytest.raises(ValueError, match="news_learning_verdict_identity_mismatch"):
        _store(_VerdictMismatch()).load_case(case)
