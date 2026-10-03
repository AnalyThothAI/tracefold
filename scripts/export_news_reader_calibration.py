"""Build a reviewable runtime calibration file from recorded certificates.

No database, model, cache or notification is accessed. A backend without a certificate stays the explicit
unfitted placeholder, whose answers only reach the feed. A certificate is active only when an owner review
binds it and its report and supplies every external release requirement; a measured gate that is not true
needs an explicit, evidenced owner waiver in that review. Precision and the probability sample can never be
waived. The production package file is never overwritten by this offline command.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from scripts.eval_news_reader import KEY_MINIMUM, KEY_TARGET, PROTOCOL, PUSH_MINIMUM, PUSH_TARGET
from tracefold.news.notifications.policy import PUSHABLE_KINDS, ReaderCalibration, ReaderPolicy
from tracefold.news.notifications.reader import READER_QUESTIONS_IDENTITY
from tracefold.news.updates.identity import digest

EXTERNAL_GATES = (
    "event_card_daily_replay",
    "volume_targets_or_explicit_owner_waiver",
    "owner_review",
    "holdout_usage_review",
)
# Never waivable: without them there is no certificate.
HARD_GATES = ("statistical_precision", "probability_sampling_population")
MEASURED_GATES = (
    *HARD_GATES,
    "paired_discrimination",
    "reader_coverage_complete",
    "type_false_ineligible_at_most_five_percent",
    "end_to_end_push_recall_at_least_v3",
    "materiality_ambiguity_below_twenty_percent",
    "paired_p90_latency_within_500ms",
)
PRODUCTION_FILE = Path(__file__).resolve().parents[1] / "tracefold/news/notifications/reader_calibration.json"
_GUIDE = re.compile(r"^news_reader_owner_guide_v\d+:[0-9a-f]{64}$")


def placeholder_entry() -> dict[str, Any]:
    """The explicit unfitted backend: zero coefficients, no cuts, uncalibrated."""
    policy = ReaderPolicy.model_validate(
        {
            "calibration": asdict(ReaderCalibration()),
            "questions_identity": READER_QUESTIONS_IDENTITY,
            "eligibility_table_sha256": digest(PUSHABLE_KINDS),
        }
    )
    return policy.model_dump(mode="json", exclude={"artifact_sha256"})


def _evidenced(item: Any) -> bool:
    return (
        isinstance(item, Mapping)
        and isinstance(item.get("evidence_ref"), str)
        and bool(item["evidence_ref"].strip())
        and re.fullmatch(r"[0-9a-f]{64}", str(item.get("evidence_sha256", ""))) is not None
    )


def _precision_proof(certificate: Mapping[str, Any], name: str, target: float, minimum: int, cut: Any) -> bool:
    proof = certificate.get(f"{name}_certificate") or {}
    selected = proof.get("selected") or {}
    lower_bound = selected.get("lower_bound")
    stories = selected.get("independent_stories")
    return (
        proof.get("status") == "certified"
        and proof.get("target") == target
        and proof.get("minimum_independent_stories") == minimum
        and selected.get("passed") is True
        and not isinstance(lower_bound, bool)
        and isinstance(lower_bound, (int, float))
        and math.isfinite(lower_bound)
        and target <= lower_bound <= 1
        and not isinstance(stories, bool)
        and isinstance(stories, int)
        and stories >= minimum
        and selected.get("cut") == cut
    )


def backend_entry(
    certificate: Mapping[str, Any],
    *,
    backend: str,
    report_ref: str,
    report_bytes: bytes,
    reader_identity: str,
    review: Mapping[str, Any] | None = None,
    review_ref: str | None = None,
) -> dict[str, Any]:
    if (
        certificate.get("protocol") != PROTOCOL
        or certificate.get("phase") != "certify"
        or certificate.get("backend") != backend
        or certificate.get("questions_identity") != READER_QUESTIONS_IDENTITY
        or certificate.get("eligibility_table_sha256") != digest(PUSHABLE_KINDS)
        or not _GUIDE.match(str(certificate.get("guide_version")))
    ):
        raise ValueError("news_reader_runtime_export_certificate_identity_mismatch")
    if not reader_identity.strip() or not report_ref.strip() or not report_bytes:
        raise ValueError("news_reader_runtime_export_report_and_production_identity_required")
    calibration = dict(certificate["calibration"])
    status = certificate["certification_status"]
    if calibration["certification_status"] != status:
        raise ValueError("news_reader_runtime_export_certificate_status_mismatch")
    if status == "certified":
        # A push-only certificate leaves key_cut empty; then no claim is ever key.
        key_cut = calibration["key_cut"]
        if not _precision_proof(certificate, "push", PUSH_TARGET, PUSH_MINIMUM, calibration["push_cut"]) or (
            key_cut is not None and not _precision_proof(certificate, "key", KEY_TARGET, KEY_MINIMUM, key_cut)
        ):
            raise ValueError("news_reader_runtime_export_precision_evidence_invalid")
    provenance = certificate["answer_provenance"]
    report_sha = sha256(report_bytes).hexdigest()
    reviewed_by = reviewed_at = None
    if review is not None:
        if not isinstance(review_ref, str) or not review_ref.strip():
            raise ValueError("news_reader_runtime_export_review_source_required")
        if (
            review.get("dataset_sha256") != certificate["dataset_sha256"]
            or review.get("candidate_identity") != certificate["candidate_identity"]
            or review.get("holdout_identity") != certificate["holdout_identity"]
            or review.get("certificate_sha256") != digest(certificate)
            or review.get("report_sha256") != report_sha
            or review.get("reader_identity") != reader_identity
        ):
            raise ValueError("news_reader_runtime_export_review_identity_mismatch")
        if status != "certified":
            raise ValueError("news_reader_runtime_export_uncertified_release")
        gates = certificate.get("release_gates") or {}
        waivers = review.get("waived_gates") or {}
        open_gates = {key for key in MEASURED_GATES if gates.get(key) is not True}
        if (
            set(gates) != {*MEASURED_GATES, *EXTERNAL_GATES}
            or any(gates[key] is not True for key in HARD_GATES)
            or not isinstance(waivers, Mapping)
            or set(waivers) != open_gates
            or any(not _evidenced(item) or not str(item.get("reason", "")).strip() for item in waivers.values())
        ):
            raise ValueError("news_reader_runtime_export_measured_release_gate_failed")
        evidence = review.get("external_gates") or {}
        if set(evidence) != set(EXTERNAL_GATES) or any(
            not _evidenced(item) or item.get("passed") is not True for item in evidence.values()
        ):
            raise ValueError("news_reader_runtime_export_external_release_evidence_required")
        reviewed_by, reviewed_at = review.get("reviewed_by"), review.get("reviewed_at")
    policy = ReaderPolicy.model_validate(
        {
            "calibration": calibration,
            "questions_identity": certificate["questions_identity"],
            "eligibility_table_sha256": certificate["eligibility_table_sha256"],
            # Offline generated reasks disable native fallback. Their program
            # identity cannot stand in for the reviewed production composite.
            "reader_identity": reader_identity,
            "answer_identity": provenance["adapter_identity"],
            "served_model": provenance["served_model"],
            "dataset_sha256": certificate["dataset_sha256"],
            # The guide the certifying owner labels used, recorded by the candidate.
            "guide_version": certificate["guide_version"],
            "report_ref": report_ref,
            "report_sha256": report_sha,
            "release_ready": review is not None,
            "review_ref": review_ref,
            "review_sha256": None if review is None else digest(review),
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
        }
    )
    return policy.model_dump(mode="json", exclude={"artifact_sha256"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for backend in ("native", "generated"):
        parser.add_argument(f"--{backend}-certificate", type=Path, help="Omit to keep the unfitted placeholder.")
        parser.add_argument(f"--{backend}-report", type=Path, help="Report bound to that certificate.")
    parser.add_argument("--reader-identity", help="Reviewed production planner composite judge identity.")
    parser.add_argument("--review", type=Path, help="Owner review per certified backend, with release evidence.")
    parser.add_argument(
        "--review-ref", help="Public reference recorded for the review (default: the review file name, never its path)."
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == PRODUCTION_FILE.resolve():
        parser.error("offline export cannot overwrite the production calibration file")
    certified = [backend for backend in ("native", "generated") if getattr(args, f"{backend}_certificate")]
    if any(
        (getattr(args, f"{name}_certificate") is None) != (getattr(args, f"{name}_report") is None)
        for name in ("native", "generated")
    ):
        parser.error("each certificate needs its report")
    if certified and not args.reader_identity:
        parser.error("--reader-identity is required with a certificate")
    reviews = {} if args.review is None else json.loads(args.review.read_text("utf-8"))
    if not set(reviews) <= set(certified):
        raise ValueError("news_reader_runtime_export_review_without_certificate")
    entries = {}
    for backend in ("native", "generated"):
        source = getattr(args, f"{backend}_certificate")
        if source is None:
            entries[backend] = placeholder_entry()
            continue
        report = getattr(args, f"{backend}_report")
        entries[backend] = backend_entry(
            json.loads(source.read_text("utf-8")),
            backend=backend,
            report_ref=str(report),
            report_bytes=report.read_bytes(),
            reader_identity=args.reader_identity,
            review=reviews.get(backend),
            review_ref=None if backend not in reviews else (args.review_ref or args.review.name),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"version": "news_reader_calibration_v1", "backends": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
