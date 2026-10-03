"""Certificates only enter runtime through matching provenance and release review."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

import pytest

from scripts import export_news_reader_calibration as bridge
from scripts.eval_news_reader import KEY_MINIMUM, KEY_TARGET, PROTOCOL, PUSH_MINIMUM, PUSH_TARGET
from scripts.export_news_reader_calibration import (
    EXTERNAL_GATES,
    HARD_GATES,
    MEASURED_GATES,
    backend_entry,
    placeholder_entry,
)
from tracefold.news.notifications.policy import PUSHABLE_KINDS, ReaderCalibration, ReaderPolicy
from tracefold.news.notifications.reader import READER_QUESTIONS_IDENTITY
from tracefold.news.updates.identity import digest

EARLIER_GUIDE = "news_reader_owner_guide_v5:" + "5" * 64


def certificate(*, key: bool = True):
    calibration = ReaderCalibration(push_cut=0.6, key_cut=0.8 if key else None, certification_status="certified")
    result = {
        "protocol": PROTOCOL,
        "phase": "certify",
        "backend": "generated",
        "questions_identity": READER_QUESTIONS_IDENTITY,
        "eligibility_table_sha256": digest(PUSHABLE_KINDS),
        "guide_version": EARLIER_GUIDE,
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
        "key_certificate": {"status": "uncalibrated", "selected": None},
    }
    for name, target, minimum, cut in (("push", PUSH_TARGET, PUSH_MINIMUM, 0.6), ("key", KEY_TARGET, KEY_MINIMUM, 0.8)):
        if name == "key" and not key:
            continue
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


def review_for(artifact):
    return {
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


def test_export_keeps_precision_certificate_inactive_without_owner_review() -> None:
    result = entry(certificate())
    policy = ReaderPolicy.model_validate(result)
    assert policy.calibration.certification_status == "certified"
    assert policy.release_ready is False
    assert policy.reader_identity == "production-native-generated-composite"
    assert policy.answer_identity == "fixture-adapter"
    assert policy.guide_version == EARLIER_GUIDE
    assert policy.report_sha256 == sha256(b"synthetic only").hexdigest()


def test_push_only_certificate_exports_without_a_key_cut() -> None:
    policy = ReaderPolicy.model_validate(entry(certificate(key=False)))
    assert policy.calibration.push_cut == 0.6 and policy.calibration.key_cut is None
    artifact = certificate(key=False)
    artifact["push_certificate"]["selected"]["cut"] = 0.5
    with pytest.raises(ValueError, match="precision_evidence_invalid"):
        entry(artifact)


def test_release_review_binds_full_evidence_and_requires_evidenced_waivers_for_open_gates() -> None:
    artifact = certificate()
    review = review_for(artifact)
    first = ReaderPolicy.model_validate(entry(artifact, review))
    assert first.release_ready is True
    assert first.review_ref == "synthetic-owner-review.json"
    assert first.review_sha256 == digest(review)
    changed_review = deepcopy(review)
    changed_review["external_gates"]["owner_review"]["evidence_sha256"] = "c" * 64
    second = ReaderPolicy.model_validate(entry(artifact, changed_review))
    assert second.identity != first.identity
    altered = deepcopy(artifact)
    altered["release_gates"]["end_to_end_push_recall_at_least_v3"] = None
    altered["release_gates"]["paired_p90_latency_within_500ms"] = False
    review = review_for(altered)
    with pytest.raises(ValueError, match="measured_release_gate_failed"):
        entry(altered, review)
    waiver = {"reason": "owner accepts", "evidence_ref": "synthetic/waiver.json", "evidence_sha256": "d" * 64}
    review["waived_gates"] = {"end_to_end_push_recall_at_least_v3": waiver}
    with pytest.raises(ValueError, match="measured_release_gate_failed"):
        entry(altered, review)
    review["waived_gates"]["paired_p90_latency_within_500ms"] = waiver
    assert ReaderPolicy.model_validate(entry(altered, review)).release_ready is True
    review["waived_gates"]["reader_coverage_complete"] = waiver
    with pytest.raises(ValueError, match="measured_release_gate_failed"):
        entry(altered, review)
    review["dataset_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="review_identity_mismatch"):
        entry(artifact, review)


@pytest.mark.parametrize("gate", HARD_GATES)
def test_precision_and_probability_sample_can_never_be_waived(gate: str) -> None:
    artifact = certificate()
    artifact["release_gates"][gate] = False
    review = review_for(artifact)
    review["waived_gates"] = {
        gate: {"reason": "not allowed", "evidence_ref": "synthetic/waiver.json", "evidence_sha256": "d" * 64}
    }
    with pytest.raises(ValueError, match="measured_release_gate_failed"):
        entry(artifact, review)


def test_export_refuses_changed_questions_or_invalid_precision_evidence() -> None:
    artifact = certificate()
    artifact["push_certificate"]["selected"]["independent_stories"] = 149
    with pytest.raises(ValueError, match="precision_evidence_invalid"):
        entry(artifact)
    for field, value in (("questions_identity", "old-questions"), ("guide_version", "unversioned-guide")):
        artifact = certificate()
        artifact[field] = value
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


def test_cli_keeps_a_backend_without_certificate_as_the_unfitted_placeholder(tmp_path: Path, monkeypatch) -> None:
    cert_path, report_path, output = tmp_path / "cert.json", tmp_path / "report.md", tmp_path / "out.json"
    artifact = certificate(key=False)
    artifact["backend"] = "native"
    cert_path.write_text(json.dumps(artifact))
    report_path.write_bytes(b"synthetic only")
    monkeypatch.setattr(
        "sys.argv",
        [
            "export_news_reader_calibration",
            "--native-certificate",
            str(cert_path),
            "--native-report",
            str(report_path),
            "--reader-identity",
            "production-native-generated-composite",
            "--output",
            str(output),
        ],
    )
    bridge.main()
    policies = ReaderPolicy.load(output)
    assert policies["native"].calibration.certification_status == "certified"
    assert policies["native"].calibration.key_cut is None and not policies["native"].release_ready
    assert json.loads(output.read_text())["backends"]["generated"] == placeholder_entry()
    assert policies["generated"].calibration == ReaderCalibration()
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"generated": review_for(artifact)}))
    monkeypatch.setattr("sys.argv", [*sys.argv, "--review", str(review)])
    with pytest.raises(ValueError, match="review_without_certificate"):
        bridge.main()
