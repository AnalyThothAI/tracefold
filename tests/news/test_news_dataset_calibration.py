"""#501 D8: inter-drafter agreement is computed from the sealed corpus at freeze time and only reported."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.news.learning.contracts import DatasetCaseRef
from tracefold.news.learning.dataset import DevelopmentDatasetStore
from tracefold.news.learning.profile import _PROFILE, development_coverage_blockers

_GOLD = {
    "subject_codes": ["medtop:20000205"],
    "event_family": "product_service_change",
    "change_state": "announced",
    "assertion_status": "confirmed",
}
_OTHER = {
    "subject_codes": [],
    "event_family": "other",
    "change_state": "unknown",
    "assertion_status": "unknown",
}


class _Repository:
    def reviews_by_id(self, review_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {review_id: {"review_id": review_id} for review_id in review_ids}

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


def _episode(
    index: int,
    *,
    drafts: dict[str, dict[str, Any]] | None,
    cluster_id: str | None = None,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {"label_source": "model_draft", "draft_author": "a+b", "review_role": "primary"}
    if drafts is not None:
        provenance["drafts"] = drafts
    return {
        "case_id": f"case-{index}",
        "cluster_id": cluster_id or f"cluster-{index}",
        "accepted_review": {"taxonomy": _GOLD, "taxonomy_review": provenance},
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


def _store(repository: _Repository) -> DevelopmentDatasetStore:
    store = object.__new__(DevelopmentDatasetStore)
    store._repository = repository
    store._stable = SimpleNamespace(
        program_version="program",
        program_sha256="d" * 64,
        bundle_sha="e" * 64,
    )
    return store


def test_calibration_receipt_reports_kappa_over_every_dual_labelled_cluster() -> None:
    episodes = [
        *(_episode(index, drafts={"model-a": _GOLD, "model-b": _GOLD}) for index in range(3)),
        _episode(3, drafts={"model-a": _GOLD, "model-b": _OTHER}),
    ]

    receipt = DevelopmentDatasetStore._calibration_receipt(episodes)

    assert receipt is not None
    assert receipt["schema"] == "tracefold.news.dataset_calibration_receipt.v2"
    assert receipt["cluster_n"] == 4
    assert receipt["dual_labeled_n"] == 4
    assert receipt["drafter_models"] == ["model-a", "model-b"]
    assert receipt["kappa"]["event_family"] == 0.0
    assert receipt["subject_mean_set_f1"] == 0.75
    assert set(receipt) == {"schema", "cluster_n", "dual_labeled_n", "kappa", "subject_mean_set_f1", "drafter_models"}


def test_single_labelled_clusters_are_skipped_and_one_representative_votes_per_cluster() -> None:
    episodes = [
        _episode(0, drafts={"model-a": _GOLD, "model-b": _GOLD}),
        _episode(1, drafts={"model-a": _GOLD, "model-b": _OTHER}, cluster_id="cluster-0"),
        _episode(2, drafts=None),
        _episode(3, drafts={"model-a": _GOLD}),
    ]

    receipt = DevelopmentDatasetStore._calibration_receipt(episodes)

    assert receipt is not None
    assert receipt["cluster_n"] == 1
    assert receipt["kappa"]["event_family"] == 1.0


def test_a_corpus_without_dual_labels_carries_no_calibration_and_is_not_blocked_for_it() -> None:
    assert DevelopmentDatasetStore._calibration_receipt([_episode(0, drafts=None)]) is None
    counts = {
        "boundary_cluster_n": 30,
        "retention_cluster_n": 100,
        "negative_cluster_n": 50,
        "safety_cluster_n": 1,
        "stratum_n": 3,
        "train_stratum_n": 3,
        "development_selection_stratum_n": 3,
    }

    assert development_coverage_blockers(counts) == ()
    # A low κ is reported beside the corpus, never a blocker (#501 §9).
    assert (
        development_coverage_blockers({**counts, "calibration": {"cluster_n": 1, "kappa": {"event_family": -0.2}}})
        == ()
    )
    assert not any("calibration" in key for key in _PROFILE["development"])


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
