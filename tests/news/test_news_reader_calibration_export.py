"""Certificates only enter runtime through matching provenance and release review."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256

import pytest

from scripts.eval_news_reader import KEY_MINIMUM, KEY_TARGET, PROTOCOL, PUSH_MINIMUM, PUSH_TARGET
from scripts.export_news_reader_calibration import EXTERNAL_GATES, MEASURED_GATES, backend_entry
from scripts.news_reader_labeling import GUIDE_VERSION
from tracefold.news.notifications.policy import PUSHABLE_KINDS, ReaderCalibration, ReaderPolicy
from tracefold.news.notifications.reader import READER_QUESTIONS_IDENTITY
from tracefold.news.updates.identity import digest


def certificate():
    calibration = ReaderCalibration(push_cut=0.6, key_cut=0.8, certification_status="certified")
    result = {
        "protocol": PROTOCOL,
        "phase": "certify",
        "backend": "generated",
        "questions_identity": READER_QUESTIONS_IDENTITY,
        "eligibility_table_sha256": digest(PUSHABLE_KINDS),
        "guide_versions": [GUIDE_VERSION],
        "calibration": asdict(calibration),
        "certification_status": "certified",
        "dataset_sha256": "a" * 64,
        "candidate_identity": "synthetic-candidate",
        "holdout_identity": "synthetic-holdout",
        "answer_provenance": {
            "adapter_identity": "fixture-adapter",
            "served_model": None,
            "program_identity": "forced-generated",
        },
        "release_gates": {**{key: True for key in MEASURED_GATES}, **{key: None for key in EXTERNAL_GATES}},
    }
    for name, target, minimum, cut in (("push", PUSH_TARGET, PUSH_MINIMUM, 0.6), ("key", KEY_TARGET, KEY_MINIMUM, 0.8)):
        result[f"{name}_certificate"] = {
            "status": "certified",
            "target": target,
            "minimum_independent_stories": minimum,
            "selected": {"passed": True, "lower_bound": target + 0.01, "independent_stories": minimum, "cut": cut},
        }
    return result


def entry(artifact, review=None):
    return backend_entry(
        artifact,
        backend="generated",
        report_ref="synthetic-report.md",
        report_bytes=b"synthetic only",
        reader_identity="production-native-generated-composite",
        review=review,
        review_ref=None if review is None else "synthetic-owner-review.json",
    )


def test_export_keeps_precision_certificate_inactive_without_owner_review() -> None:
    result = entry(certificate())
    policy = ReaderPolicy.model_validate(result)
    assert policy.calibration.certification_status == "certified"
    assert policy.release_ready is False
    assert policy.reader_identity == "production-native-generated-composite"
    assert policy.answer_identity == "fixture-adapter"
    assert policy.report_sha256 == sha256(b"synthetic only").hexdigest()


def test_release_review_binds_full_evidence_and_refuses_failed_recall() -> None:
    artifact = certificate()
    review = {
        "dataset_sha256": artifact["dataset_sha256"],
        "candidate_identity": artifact["candidate_identity"],
        "holdout_identity": artifact["holdout_identity"],
        "certificate_sha256": digest(artifact),
        "report_sha256": sha256(b"synthetic only").hexdigest(),
        "reader_identity": "production-native-generated-composite",
        "reviewed_by": "synthetic-owner",
        "reviewed_at": "2026-10-02T18:00:00+00:00",
        "external_gates": {
            gate: {"passed": True, "evidence_ref": f"synthetic/{gate}.json", "evidence_sha256": "b" * 64}
            for gate in EXTERNAL_GATES
        },
    }
    first = ReaderPolicy.model_validate(entry(artifact, review))
    assert first.release_ready is True
    assert first.review_ref == "synthetic-owner-review.json"
    assert first.review_sha256 == digest(review)
    changed_review = deepcopy(review)
    changed_review["external_gates"]["owner_review"]["evidence_sha256"] = "c" * 64
    second = ReaderPolicy.model_validate(entry(artifact, changed_review))
    assert second.identity != first.identity
    altered = deepcopy(artifact)
    altered["release_gates"]["end_to_end_push_recall_at_least_v3"] = False
    review["certificate_sha256"] = digest(altered)
    with pytest.raises(ValueError, match="measured_release_gate_failed"):
        entry(altered, review)
    review["dataset_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="review_identity_mismatch"):
        entry(artifact, review)


def test_export_refuses_changed_questions_or_invalid_precision_evidence() -> None:
    artifact = certificate()
    artifact["push_certificate"]["selected"]["independent_stories"] = 149
    with pytest.raises(ValueError, match="precision_evidence_invalid"):
        entry(artifact)
    artifact = certificate()
    artifact["questions_identity"] = "old-questions"
    with pytest.raises(ValueError, match="certificate_identity_mismatch"):
        entry(artifact)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2.0, -0.1, True, None])
def test_export_rejects_invalid_precision_lower_bound(value) -> None:
    artifact = certificate()
    artifact["push_certificate"]["selected"]["lower_bound"] = value
    with pytest.raises(ValueError, match="precision_evidence_invalid"):
        entry(artifact)


@pytest.mark.parametrize("value", [float("nan"), 150.5, True, None])
def test_export_requires_integer_independent_story_count(value) -> None:
    artifact = certificate()
    artifact["push_certificate"]["selected"]["independent_stories"] = value
    with pytest.raises(ValueError, match="precision_evidence_invalid"):
        entry(artifact)
