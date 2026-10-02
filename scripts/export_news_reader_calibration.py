"""Build a reviewable runtime calibration envelope from recorded certificates.

No database, model, cache or notification is accessed. Outputs remain inactive
unless a matching owner review supplies every external release requirement.
The production package file is never overwritten by this offline command.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from scripts.eval_news_reader import KEY_MINIMUM, KEY_TARGET, PROTOCOL, PUSH_MINIMUM, PUSH_TARGET
from scripts.news_reader_labeling import GUIDE_VERSION
from tracefold.news.notifications.policy import PUSHABLE_KINDS, ReaderPolicy
from tracefold.news.notifications.reader import READER_QUESTIONS_IDENTITY
from tracefold.news.updates.identity import digest

EXTERNAL_GATES = (
    "event_card_daily_replay",
    "volume_targets_or_explicit_owner_waiver",
    "owner_review",
    "holdout_usage_review",
)
MEASURED_GATES = (
    "statistical_precision",
    "paired_discrimination",
    "probability_sampling_population",
    "reader_coverage_complete",
    "type_false_ineligible_at_most_five_percent",
    "end_to_end_push_recall_at_least_v3",
    "materiality_ambiguity_below_twenty_percent",
    "paired_p90_latency_within_500ms",
)
PRODUCTION_FILE = Path(__file__).resolve().parents[1] / "tracefold/news/notifications/reader_calibration.json"


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
        or certificate.get("guide_versions") != [GUIDE_VERSION]
    ):
        raise ValueError("news_reader_runtime_export_certificate_identity_mismatch")
    if not reader_identity.strip() or not report_ref.strip() or not report_bytes:
        raise ValueError("news_reader_runtime_export_report_and_production_identity_required")
    calibration = dict(certificate["calibration"])
    status = certificate["certification_status"]
    if calibration["certification_status"] != status:
        raise ValueError("news_reader_runtime_export_certificate_status_mismatch")
    if status == "certified":
        for name, target, minimum in (("push", PUSH_TARGET, PUSH_MINIMUM), ("key", KEY_TARGET, KEY_MINIMUM)):
            proof = certificate.get(f"{name}_certificate") or {}
            selected = proof.get("selected") or {}
            lower_bound = selected.get("lower_bound")
            stories = selected.get("independent_stories")
            if (
                proof.get("status") != "certified"
                or proof.get("target") != target
                or proof.get("minimum_independent_stories") != minimum
                or selected.get("passed") is not True
                or isinstance(lower_bound, bool)
                or not isinstance(lower_bound, (int, float))
                or not math.isfinite(lower_bound)
                or not target <= lower_bound <= 1
                or isinstance(stories, bool)
                or not isinstance(stories, int)
                or stories < minimum
                or selected.get("cut") != calibration[f"{name}_cut"]
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
        gates = certificate.get("release_gates") or {}
        if set(gates) != {*MEASURED_GATES, *EXTERNAL_GATES} or any(gates[key] is not True for key in MEASURED_GATES):
            raise ValueError("news_reader_runtime_export_measured_release_gate_failed")
        evidence = review.get("external_gates") or {}
        if set(evidence) != set(EXTERNAL_GATES) or any(
            not isinstance(item, Mapping)
            or item.get("passed") is not True
            or not item.get("evidence_ref")
            or re.fullmatch(r"[0-9a-f]{64}", str(item.get("evidence_sha256", ""))) is None
            for item in evidence.values()
        ):
            raise ValueError("news_reader_runtime_export_external_release_evidence_required")
        if status != "certified":
            raise ValueError("news_reader_runtime_export_uncertified_release")
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
            "guide_version": GUIDE_VERSION,
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
        parser.add_argument(f"--{backend}-certificate", type=Path, required=True)
        parser.add_argument(f"--{backend}-report", type=Path, required=True)
    parser.add_argument(
        "--reader-identity", required=True, help="Reviewed production planner composite judge identity."
    )
    parser.add_argument(
        "--review", type=Path, help="Owner review with matching backend identities and external evidence."
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == PRODUCTION_FILE.resolve():
        parser.error("offline export cannot overwrite the production calibration file")
    reviews = {} if args.review is None else json.loads(args.review.read_text("utf-8"))
    if args.review is not None and set(reviews) != {"native", "generated"}:
        raise ValueError("news_reader_runtime_export_both_backend_reviews_required")
    entries = {}
    for backend in ("native", "generated"):
        source = getattr(args, f"{backend}_certificate")
        report = getattr(args, f"{backend}_report")
        entries[backend] = backend_entry(
            json.loads(source.read_text("utf-8")),
            backend=backend,
            report_ref=str(report),
            report_bytes=report.read_bytes(),
            reader_identity=args.reader_identity,
            review=reviews.get(backend),
            review_ref=None if args.review is None else str(args.review),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"version": "news_reader_calibration_v1", "backends": entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
