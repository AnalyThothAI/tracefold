"""Offline reader calibration: fit, independent certification, then read-only report.

Run each phase separately. No provider, database, cache or sender is constructed.
Old importance scores are historical evidence, never converted into new answers.
Claude labels can train a candidate; only independent owner labels certify it.
Install optional fitting dependencies with ``uv sync --group research``.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.news_reader_diagnostics import baseline_auc, diagnostic_report
from scripts.news_reader_io import read_jsonl, write_jsonl
from scripts.news_reader_labeling import GUIDE_VERSION
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, ReaderNovelty, reader_novelty
from tracefold.news.notifications.policy import (
    KIND_FLOOR,
    LOGIT_EPSILON,
    PUSHABLE_KINDS,
    ReaderCalibration,
    novelty_outcome,
    reader_decision,
    reader_scores,
)
from tracefold.news.notifications.reader import READER_QUESTIONS_IDENTITY, ReaderBackend, ReaderInput, ReaderJudgment
from tracefold.news.updates.contracts import ClaimFields
from tracefold.news.updates.identity import digest

PROTOCOL = "news_reader_calibration_v2"
FIT_CONFIG: dict[str, Any] = {
    "C": 1.0,
    "solver": "lbfgs",
    "max_iter": 2000,
    "materiality_floors": [1, 2, 3],
    "folds": 5,
    "sklearn_version": "1.9.1",
}
# This order is fixed before seeing certification labels. Stop at the first failure.
CUT_SEQUENCE = (0.99, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5)
PUSH_TARGET, KEY_TARGET, DELTA = 0.65, 0.75, 0.1
PUSH_MINIMUM, KEY_MINIMUM = 150, 60


def load(path: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    if not rows or len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("news_reader_eval_empty_or_duplicate_cases")
    for row in rows:
        ReaderInput.model_validate(row["reader_input"])
        if not row.get("story_id") or not row.get("guide_version") or not row.get("labeler"):
            raise ValueError("news_reader_eval_label_provenance_required")
        if row["guide_version"] != GUIDE_VERSION:
            raise ValueError("news_reader_eval_owner_guide_changed")
        label = row["label"]
        if label.get("push") not in {"push", "borderline", "feed"} or not isinstance(label.get("key"), bool):
            raise ValueError("news_reader_eval_current_labels_required")
        if label["key"] and label["push"] != "push":
            raise ValueError("news_reader_eval_key_requires_push")
        if label.get("kind") not in PUSHABLE_KINDS or label.get("anchor") not in {
            "none",
            *(f"m{i + 1}" for i in range(len(row["reader_input"]["messages"]))),
        }:
            raise ValueError("news_reader_eval_label_invalid")
        probability = row.get("inclusion_probability")
        if probability is not None and (not isinstance(probability, (int, float)) or not 0 < probability <= 1):
            raise ValueError("news_reader_eval_inclusion_probability_invalid")
        novelty = reader_novelty(
            row["claim_ref"],
            [ClaimLink.model_validate(link) for link in row.get("links", [])],
            [LinkedReceipt.model_validate(receipt) for receipt in row.get("receipts", [])],
        )
        if "novelty" in row and novelty != ReaderNovelty.model_validate(row["novelty"]):
            raise ValueError("news_reader_eval_novelty_drift")
        if len(row["message_intents"]) != len(row["reader_input"]["messages"]):
            raise ValueError("news_reader_eval_message_intents_mismatch")
        row["reader_novelty"] = novelty
        row.setdefault("split_story_id", row["story_id"])
    return rows


def assemble(
    cases: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    journals: Mapping[ReaderBackend, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Join frozen inputs, labels and exact-input reasks, preserving failed-call absence.

    Overlapping owner labels replace proxies; keep both original label journals
    for agreement reporting. Sampling metadata must describe the final label's
    joint inclusion probability, including selection for owner review.
    """
    inputs = {row["case_id"]: dict(row) for row in cases}
    if not inputs or len(inputs) != len(cases):
        raise ValueError("news_reader_eval_empty_or_duplicate_frozen_case")
    chosen: dict[str, dict[str, Any]] = {}
    proxy_stories: dict[str, str] = {}
    seen = set()
    for raw in labels:
        case, labeler = raw["case_id"], raw["labeler"]
        if case not in inputs or raw.get("reader_input_sha256") != digest(inputs[case]["reader_input"]):
            raise ValueError("news_reader_eval_label_input_changed")
        if (case, labeler) in seen:
            raise ValueError("news_reader_eval_duplicate_case_labeler")
        seen.add((case, labeler))
        if raw.get("guide_version") != GUIDE_VERSION:
            raise ValueError("news_reader_eval_owner_guide_changed")
        if labeler != "owner" and not labeler.startswith("claude:"):
            raise ValueError("news_reader_eval_labeler_invalid")
        if labeler != "owner":
            if case in proxy_stories and proxy_stories[case] != raw["story_id"]:
                raise ValueError("news_reader_eval_frozen_proxy_story_changed")
            proxy_stories[case] = raw["story_id"]
        if case not in chosen or labeler == "owner":
            chosen[case] = dict(raw)
    assembled = []
    for case, annotation in chosen.items():
        assembled.append(
            {
                **inputs[case],
                "split_story_id": inputs[case].get("split_story_id", proxy_stories.get(case, annotation["story_id"])),
                "case_sampling": inputs[case].get("case_sampling")
                or {
                    key: inputs[case].get(key)
                    for key in (
                        "sampling_design",
                        "inclusion_probability",
                        "stratum",
                        "sampling_unit",
                        "sampling_frame",
                    )
                },
                # Label inclusion includes selection for owner review; never
                # borrow a proxy pool's probability for a targeted gold subset.
                "sampling_design": "unknown",
                "inclusion_probability": None,
                "stratum": None,
                "sampling_unit": "unknown",
                "sampling_frame": None,
                **{
                    key: annotation[key]
                    for key in (
                        "label",
                        "labeler",
                        "guide_version",
                        "story_id",
                        "stratum",
                        "sampling_design",
                        "inclusion_probability",
                        "sampling_unit",
                        "sampling_frame",
                    )
                    if key in annotation
                },
                "answers": {},
            }
        )
    by_id = {row["case_id"]: row for row in assembled}
    for backend, journal in journals.items():
        latest: dict[str, Mapping[str, Any]] = {}
        for record in journal:
            case = record["case_id"]
            if case not in inputs or record.get("input_sha256") != digest(inputs[case]["reader_input"]):
                raise ValueError("news_reader_eval_journal_input_changed")
            if (
                record.get("kind") != "reader"
                or record.get("questions_identity") != READER_QUESTIONS_IDENTITY
                or record.get("requested_backend") != backend
            ):
                raise ValueError("news_reader_eval_journal_questions_or_backend_changed")
            if not record.get("program_identity"):
                raise ValueError("news_reader_eval_answer_provenance_required")
            if case in latest and not (latest[case].get("error_code") or latest[case].get("error_class")):
                raise ValueError("news_reader_eval_duplicate_successful_journal_case")
            latest[case] = record
        for case, record in latest.items():
            if case not in by_id:
                continue
            if record.get("skipped"):
                if inputs[case].get("reader_applicable") is not False:
                    raise ValueError("news_reader_eval_applicable_reader_cannot_be_skipped")
                by_id[case].setdefault("reask_skips", {})[backend] = dict(record)
            elif record.get("error_code") or record.get("error_class"):
                by_id[case].setdefault("reask_failures", {})[backend] = dict(record)
            else:
                by_id[case]["answers"][backend] = dict(record)
        if any(row["answers"].get(backend) for row in assembled):
            recorded(assembled, backend)
    return sorted(assembled, key=lambda row: row["case_id"])


def recorded(rows: Sequence[Mapping[str, Any]], backend: ReaderBackend) -> dict[str, ReaderJudgment]:
    answers = {}
    for row in rows:
        record = row.get("answers", {}).get(backend)
        if record is None:
            continue
        if "importance" in record:
            raise ValueError("news_reader_eval_historical_scores_require_real_reask")
        if record.get("questions_identity") != READER_QUESTIONS_IDENTITY:
            raise ValueError("news_reader_eval_questions_changed")
        if record.get("input_sha256") != digest(row["reader_input"]):
            raise ValueError("news_reader_eval_answer_input_changed")
        if (
            record.get("kind") != "reader"
            or record.get("requested_backend") != backend
            or not record.get("program_identity")
        ):
            raise ValueError("news_reader_eval_answer_provenance_required")
        judgment = ReaderJudgment.model_validate(record["judgment"])
        if judgment.status != "available" or judgment.backend != backend:
            raise ValueError("news_reader_eval_requested_backend_unavailable")
        count = len(row["reader_input"]["messages"])
        if (judgment.anchor is None) != (count == 0) or (
            judgment.anchor is not None and len(judgment.anchor.probabilities) != count + 1
        ):
            raise ValueError("news_reader_eval_answer_shape_mismatch")
        answers[row["case_id"]] = judgment
    answer_provenance(rows, answers, backend)
    return answers


def answer_record(
    judgment: ReaderJudgment, reader_input: Mapping[str, Any], *, program_identity: str
) -> dict[str, Any]:
    if judgment.status != "available":
        raise ValueError("news_reader_eval_answer_unavailable")
    return {
        "questions_identity": READER_QUESTIONS_IDENTITY,
        "kind": "reader",
        "input_sha256": digest(reader_input),
        "program_identity": program_identity,
        "requested_backend": judgment.backend,
        "judgment": judgment.model_dump(mode="json"),
    }


def answer_provenance(
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, ReaderJudgment],
    backend: ReaderBackend,
) -> dict[str, Any]:
    identities = {
        (
            answers[row["case_id"]].identity,
            answers[row["case_id"]].served_model,
            row["answers"][backend]["program_identity"],
        )
        for row in rows
        if row["case_id"] in answers
    }
    if len(identities) != 1:
        raise ValueError("news_reader_eval_single_model_adapter_identity_required")
    adapter, served_model, program = next(iter(identities))
    return {"adapter_identity": adapter, "served_model": served_model, "program_identity": program}


def fit_cut_sequences(predictions: Sequence[Mapping[str, Any]], holdout_count: int) -> dict[str, list[float]]:
    """Freeze support-aware score quantiles from fitting OOF data, without holdout labels."""
    result = {}
    for field, minimum in (("p_push", PUSH_MINIMUM), ("p_key", KEY_MINIMUM)):
        values = sorted(row[field] for row in predictions)
        if not values:
            raise ValueError("news_reader_eval_fitting_predictions_required")
        # Approximate the highest quantile with the required holdout support;
        # the actual independent sample count is still tested, never assumed.
        start = max(0.0, 1 - minimum / max(1, holdout_count))
        quantiles = [start * (1 - step / 10) for step in range(11)]
        result[field.removeprefix("p_")] = sorted(
            {math.floor(values[min(len(values) - 1, math.ceil(q * len(values)))] * 1000) / 1000 for q in quantiles},
            reverse=True,
        )
    return result


def _time(row: Mapping[str, Any]) -> int:
    if "first_available_at_ms" in row:
        return int(row["first_available_at_ms"])
    return int(datetime.fromisoformat(row["reader_input"]["as_of"]).replace(tzinfo=UTC).timestamp() * 1000)


def split_cases(rows: Sequence[Mapping[str, Any]], fraction: float = 0.7) -> dict[str, Any]:
    """Preserve temporal order and keep boundary-spanning stories out of certification."""
    if len(rows) < 2 or not 0 < fraction < 1:
        raise ValueError("news_reader_eval_split_requires_cases")
    ordered = sorted(rows, key=lambda row: (_time(row), row["case_id"]))
    boundary = _time(ordered[min(len(rows) - 1, max(1, int(len(rows) * fraction)))])
    stories: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in ordered:
        stories[row.get("split_story_id", row["story_id"])].append(row)
    fit: list[str] = []
    certification: list[str] = []
    excluded: list[str] = []
    for story, cases in stories.items():
        if max(_time(row) for row in cases) < boundary:
            fit.extend(row["case_id"] for row in cases)
        elif min(_time(row) for row in cases) >= boundary and all(
            row.get("sampling_design") != "hard_case" for row in cases
        ):
            certification.extend(row["case_id"] for row in cases)
        else:
            # Boundary stories cannot leak into the later holdout. Hard examples
            # are allowed in fitting only when every case predates the boundary.
            excluded.append(story)
    if not fit or not certification:
        raise ValueError("news_reader_eval_independent_time_story_split_unavailable")
    return {
        "fit": sorted(fit),
        "certification": sorted(certification),
        "boundary_ms": boundary,
        "excluded_story_ids": sorted(excluded),
    }


def dataset_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    return digest(
        [
            {key: value for key, value in row.items() if key != "reader_novelty"}
            for row in sorted(rows, key=lambda row: row["case_id"])
        ]
    )


HOLDOUT_ANNOTATION_FIELDS = {
    "label",
    "labeler",
    "guide_version",
    "inclusion_probability",
    "sampling_design",
    "sampling_unit",
    "sampling_frame",
    "stratum",
    "story_id",
}


def frozen_dataset_digest(rows: Sequence[Mapping[str, Any]], certification: Sequence[str]) -> str:
    """Permit later gold only in held-out rows; inputs, story grouping, answers and fit labels stay frozen."""
    ids = set(certification)
    return dataset_digest(
        [
            {
                **{
                    key: value
                    for key, value in row.items()
                    if row["case_id"] not in ids or key not in HOLDOUT_ANNOTATION_FIELDS
                },
                "split_story_id": row.get("split_story_id", row["story_id"]),
            }
            for row in rows
        ]
    )


def applicable(row: Mapping[str, Any]) -> bool:
    """Frozen production pre-reader decision, never reconstructed from model scores."""
    if not isinstance(row.get("reader_applicable"), bool) or not row.get("pre_reader_reason"):
        raise ValueError("news_reader_eval_production_applicability_required")
    expected = row["pre_reader_reason"] in {"reader_unavailable", "reader_unassessed"}
    if row["reader_applicable"] != expected:
        raise ValueError("news_reader_eval_production_applicability_changed")
    if not expected and row.get("deterministic_decision") not in {"notify", "drop", "deferred"}:
        raise ValueError("news_reader_eval_deterministic_decision_required")
    return expected


def _logit(probability: float) -> float:
    p = min(1 - LOGIT_EPSILON, max(LOGIT_EPSILON, probability))
    return math.log(p) - math.log1p(-p)


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1 + exp)


def features(row: Mapping[str, Any], judgment: ReaderJudgment, floor: int) -> tuple[list[float], list[float]]:
    # Reuse the production held definition, including effective-action bypass.
    result = reader_decision(
        row["reader_novelty"],
        judgment,
        first_available_at_ms=_time(row),
        message_intents=row["message_intents"],
        calibration=ReaderCalibration(materiality_floor=floor),
        claim_fields=ClaimFields.model_validate(row["reader_input"]["claim"]["fields"]),
    )
    if result.scores is None:
        raise ValueError("news_reader_eval_deterministic_exception_not_calibration_case")
    scores = result.scores
    return [_logit(scores.e), _logit(scores.m), float(scores.held)], [_logit(scores.i), _logit(scores.e)]


def _fit_logistic(x: list[list[float]], y: list[int], weights: list[float]) -> tuple[float, ...]:
    if set(y) != {0, 1}:
        raise ValueError("news_reader_eval_two_label_classes_required")
    try:
        from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ValueError("news_reader_eval_install_research_dependency_group") from exc
    model = LogisticRegression(C=FIT_CONFIG["C"], solver=FIT_CONFIG["solver"], max_iter=FIT_CONFIG["max_iter"])
    # Normalize inverse-probability weights so regularization is independent
    # of the absolute sampling fraction.
    normalized = [value * len(weights) / sum(weights) for value in weights]
    model.fit(x, y, sample_weight=normalized)
    return (float(model.intercept_[0]), *(float(value) for value in model.coef_[0]))


def _predict(coefficients: Sequence[float], x: Sequence[Sequence[float]]) -> list[float]:
    return [_sigmoid(coefficients[0] + sum(a * b for a, b in zip(coefficients[1:], row, strict=True))) for row in x]


def _weight(row: Mapping[str, Any]) -> float:
    return 1 / row["inclusion_probability"] if row.get("inclusion_probability") else 1.0


def auc(labels: Sequence[int], probabilities: Sequence[float], weights: Sequence[float]) -> float | None:
    positive = [(p, w) for y, p, w in zip(labels, probabilities, weights, strict=True) if y]
    negative = [(p, w) for y, p, w in zip(labels, probabilities, weights, strict=True) if not y]
    total = sum(w for _, w in positive) * sum(w for _, w in negative)
    if not total:
        return None
    return sum(pw * nw * (1 if p > n else 0.5 if p == n else 0) for p, pw in positive for n, nw in negative) / total


def probability_report(labels: list[int], probabilities: list[float], weights: list[float]) -> dict[str, Any]:
    total = sum(weights)
    if not total:
        return {
            "cases": 0,
            "auc": None,
            "brier": None,
            "log_loss": None,
            "reliability": [],
            "calibration_slope": None,
            "calibration_intercept": None,
        }
    bins = []
    for index in range(10):
        positions = [i for i, p in enumerate(probabilities) if min(9, int(p * 10)) == index]
        weight = sum(weights[i] for i in positions)
        if weight:
            bins.append(
                {
                    "lower": index / 10,
                    "upper": (index + 1) / 10,
                    "cases": len(positions),
                    "predicted": sum(weights[i] * probabilities[i] for i in positions) / weight,
                    "observed": sum(weights[i] * labels[i] for i in positions) / weight,
                }
            )
    slope = intercept = None
    if set(labels) == {0, 1}:
        try:
            intercept, slope = _fit_logistic([[_logit(p)] for p in probabilities], labels, weights)
        except ValueError as exc:
            if str(exc) != "news_reader_eval_install_research_dependency_group":
                raise
    clipped = [min(1 - LOGIT_EPSILON, max(LOGIT_EPSILON, p)) for p in probabilities]
    return {
        "cases": len(labels),
        "auc": auc(labels, probabilities, weights),
        "brier": sum(w * (p - y) ** 2 for y, p, w in zip(labels, probabilities, weights, strict=True)) / total,
        "log_loss": -sum(
            w * (y * math.log(p) + (1 - y) * math.log1p(-p)) for y, p, w in zip(labels, clipped, weights, strict=True)
        )
        / total,
        "reliability": bins,
        "calibration_slope": slope,
        "calibration_intercept": intercept,
    }


def fit(rows: Sequence[Mapping[str, Any]], backend: ReaderBackend) -> dict[str, Any]:
    """Select m* by story-disjoint, out-of-fold fitting data; never inspect holdout labels."""
    try:
        import sklearn  # type: ignore[import-untyped]
        from sklearn.model_selection import GroupKFold  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ValueError("news_reader_eval_install_research_dependency_group") from exc
    if sklearn.__version__ != FIT_CONFIG["sklearn_version"]:
        raise ValueError("news_reader_eval_research_dependency_version_changed")

    split = split_cases(rows)
    if any(row["guide_version"] != GUIDE_VERSION for row in rows):
        raise ValueError("news_reader_eval_owner_guide_changed")
    answers = recorded(rows, backend)
    training = [
        row
        for row in rows
        if row["case_id"] in split["fit"]
        and applicable(row)
        and row["case_id"] in answers
        and row["label"]["push"] != "borderline"
        and novelty_outcome(row["reader_novelty"], first_available_at_ms=_time(row)) is None
    ]
    groups = [row.get("split_story_id", row["story_id"]) for row in training]
    if len(set(groups)) < 3:
        raise ValueError("news_reader_eval_three_training_stories_required")
    weights = [_weight(row) for row in training]
    push_y, key_y = (
        [int(row["label"]["push"] == "push") for row in training],
        [int(row["label"]["key"]) for row in training],
    )
    candidates: list[dict[str, Any]] = []
    for floor in FIT_CONFIG["materiality_floors"]:
        vectors = [features(row, answers[row["case_id"]], floor) for row in training]
        push_x, key_x = [x for x, _ in vectors], [x for _, x in vectors]
        push_oof, key_oof = [0.0] * len(training), [0.0] * len(training)
        for train, validate in GroupKFold(n_splits=min(FIT_CONFIG["folds"], len(set(groups)))).split(
            push_x, push_y, groups
        ):
            for x, y, oof in ((push_x, push_y, push_oof), (key_x, key_y, key_oof)):
                coefficients = _fit_logistic([x[i] for i in train], [y[i] for i in train], [weights[i] for i in train])
                for index, p in zip(validate, _predict(coefficients, [x[i] for i in validate]), strict=True):
                    oof[index] = p
        candidates.append(
            {
                "floor": floor,
                "push_coefficients": _fit_logistic(push_x, push_y, weights),
                "key_coefficients": _fit_logistic(key_x, key_y, weights),
                "push_oof": push_oof,
                "key_oof": key_oof,
                "push_report": probability_report(push_y, push_oof, weights),
                "key_report": probability_report(key_y, key_oof, weights),
            }
        )
    selected = min(candidates, key=lambda candidate: (candidate["push_report"]["log_loss"], candidate["floor"]))
    calibration = ReaderCalibration(
        materiality_floor=selected["floor"],
        push_coefficients=(
            selected["push_coefficients"][0],
            selected["push_coefficients"][1],
            selected["push_coefficients"][2],
            selected["push_coefficients"][3],
        ),
        key_coefficients=(
            selected["key_coefficients"][0],
            selected["key_coefficients"][1],
            selected["key_coefficients"][2],
        ),
    )
    predictions = [
        {
            "case_id": row["case_id"],
            "input_sha256": digest(row["reader_input"]),
            "guide_version": GUIDE_VERSION,
            "p_push": p,
            "p_key": k,
        }
        for row, p, k in zip(training, selected["push_oof"], selected["key_oof"], strict=True)
    ]
    artifact = {
        "protocol": PROTOCOL,
        "phase": "fit",
        "backend": backend,
        "questions_identity": READER_QUESTIONS_IDENTITY,
        "eligibility_table_sha256": digest(PUSHABLE_KINDS),
        "kind_floor": KIND_FLOOR,
        "dataset_sha256": dataset_digest(rows),
        "guide_versions": sorted({row["guide_version"] for row in rows}),
        "split": split,
        "calibration": asdict(calibration),
        "cut_sequence": fit_cut_sequences(predictions, len(split["certification"])),
        "answer_provenance": answer_provenance(rows, answers, backend),
        "fit_cases": [row["case_id"] for row in training],
        "materiality_candidates": [
            {key: candidate[key] for key in ("floor", "push_report", "key_report")} for candidate in candidates
        ],
        "oof_predictions": predictions,
        "label_sources": dict(Counter(row["labeler"] for row in training)),
        "certification_status": "uncalibrated",
        "fit_config": {**FIT_CONFIG, "materiality_floors": list(FIT_CONFIG["materiality_floors"])},
        "fit_dataset_sha256": dataset_digest([row for row in rows if row["case_id"] in split["fit"]]),
    }
    # Use the canonical frozen holdout including provenance, omitting only the derived object.
    artifact["holdout_identity"] = digest(
        {
            "backend": backend,
            "dataset": frozen_dataset_digest(
                [row for row in rows if row["case_id"] in split["certification"]], split["certification"]
            ),
        }
    )
    artifact["frozen_dataset_sha256"] = frozen_dataset_digest(rows, split["certification"])
    artifact["candidate_identity"] = candidate_identity(artifact)
    return artifact


DERIVATION_FIELDS = (
    "protocol",
    "backend",
    "questions_identity",
    "eligibility_table_sha256",
    "kind_floor",
    "frozen_dataset_sha256",
    "guide_versions",
    "split",
    "calibration",
    "cut_sequence",
    "answer_provenance",
    "fit_cases",
    "materiality_candidates",
    "oof_predictions",
    "fit_config",
    "fit_dataset_sha256",
    "holdout_identity",
)


def candidate_identity(artifact: Mapping[str, Any]) -> str:
    return digest({field: artifact[field] for field in DERIVATION_FIELDS})


def verify_candidate(rows: Sequence[Mapping[str, Any]], artifact: Mapping[str, Any]) -> None:
    """Rebuild from frozen fit data; a new self-hash cannot legitimize edited coefficients."""
    if any(field not in artifact for field in DERIVATION_FIELDS):
        raise ValueError("news_reader_eval_candidate_derivation_required")
    rebuilt = fit(rows, artifact["backend"])
    if (
        candidate_identity(artifact) != rebuilt["candidate_identity"]
        or artifact.get("candidate_identity") != rebuilt["candidate_identity"]
    ):
        raise ValueError("news_reader_eval_candidate_derivation_changed")


def register_holdout_use(path: Path, artifact: Mapping[str, Any], *, certification_dataset_sha256: str) -> None:
    """Local append-only protocol journal. External/repeated holdout access still needs owner review."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    try:
        reservation = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ValueError("news_reader_eval_holdout_ledger_busy") from exc
    try:
        with reservation:
            existing = read_jsonl(path) if path.exists() else []
            same = [row for row in existing if row["holdout_identity"] == artifact["holdout_identity"]]
            if any(row["candidate_identity"] != artifact["candidate_identity"] for row in same):
                raise ValueError("news_reader_eval_holdout_already_used_by_another_candidate")
            if any(row.get("certification_dataset_sha256") != certification_dataset_sha256 for row in same):
                raise ValueError("news_reader_eval_holdout_gold_sample_changed_after_use")
            if not same:
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "holdout_identity": artifact["holdout_identity"],
                                "candidate_identity": artifact["candidate_identity"],
                                "certification_dataset_sha256": certification_dataset_sha256,
                            }
                        )
                        + "\n"
                    )
    finally:
        lock.unlink()


def clopper_pearson_lower(successes: int, trials: int, delta: float = DELTA) -> float:
    """Invert the exact binomial upper tail; no pseudo-counts or fractional weights."""
    if not 0 <= successes <= trials or not 0 < delta < 1:
        raise ValueError("news_reader_eval_binomial_arguments_invalid")
    if not successes:
        return 0.0
    if successes == trials:
        return math.exp(math.log(delta) / trials)
    low, high = 0.0, 1.0
    constants = [
        math.lgamma(trials + 1) - math.lgamma(k + 1) - math.lgamma(trials - k + 1) for k in range(successes, trials + 1)
    ]
    for _ in range(60):
        p = (low + high) / 2
        terms = [
            constant + k * math.log(p) + (trials - k) * math.log1p(-p)
            for k, constant in zip(range(successes, trials + 1), constants, strict=True)
        ]
        maximum = max(terms)
        tail = math.exp(maximum) * sum(math.exp(term - maximum) for term in terms)
        if tail < delta:
            low = p
        else:
            high = p
    return (low + high) / 2


def _sampling_strata(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    if not rows or any(
        row.get("sampling_design") not in {"uniform", "stratified"} or row.get("inclusion_probability") is None
        for row in rows
    ):
        raise ValueError("news_reader_eval_probability_sample_required")
    designs = {row["sampling_design"] for row in rows}
    if len(designs) != 1:
        raise ValueError("news_reader_eval_mixed_sampling_design")
    if designs == {"uniform"}:
        if len({row["inclusion_probability"] for row in rows}) != 1:
            raise ValueError("news_reader_eval_uniform_probability_changed")
        return ["uniform"]
    strata = sorted({row.get("stratum", "") for row in rows})
    if not all(strata):
        raise ValueError("news_reader_eval_stratum_required")
    for stratum in strata:
        if len({row["inclusion_probability"] for row in rows if row["stratum"] == stratum}) != 1:
            raise ValueError("news_reader_eval_probability_must_be_constant_within_stratum")
    return strata


def sampling_frame(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """A claim sample with distinct observed story IDs is not a story probability frame."""
    frames = [row.get("sampling_frame") for row in rows]
    if not frames or not isinstance(frames[0], Mapping) or any(frame != frames[0] for frame in frames):
        raise ValueError("news_reader_eval_frozen_sampling_frame_required")
    frame = frames[0]
    if (
        not frame.get("frame_id")
        or frame.get("unit") != "independent_story_representative"
        or any(row.get("sampling_unit") != frame["unit"] for row in rows)
        or frame.get("selection_frozen_before_labels") is not True
        or frame.get("story_grouping_reviewed") is not True
        or frame.get("scope") != "holdout"
    ):
        raise ValueError("news_reader_eval_independent_owner_story_sampling_frame_required")
    sizes = frame.get("stratum_sizes")
    if (
        not isinstance(sizes, Mapping)
        or not sizes
        or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in sizes.values())
        or set(frame.get("certification_strata", [])) != set(sizes)
        or frame.get("units") != sum(sizes.values())
    ):
        raise ValueError("news_reader_eval_population_stratum_sizes_required")
    selected_ids = frame.get("selected_case_ids")
    if (
        not isinstance(selected_ids, list)
        or len(selected_ids) != len(set(selected_ids))
        or set(selected_ids) != {row["case_id"] for row in rows}
    ):
        raise ValueError("news_reader_eval_selected_owner_labels_incomplete")
    for stratum, population in sizes.items():
        observed = [
            row for row in rows if ("uniform" if row["sampling_design"] == "uniform" else row["stratum"]) == stratum
        ]
        if len(observed) > population:
            raise ValueError("news_reader_eval_sample_exceeds_population_frame")
        if frame.get("selection_design") == "stratified_srs_without_replacement" and any(
            not math.isclose(row["inclusion_probability"], len(observed) / population, abs_tol=1e-12)
            for row in observed
        ):
            raise ValueError("news_reader_eval_joint_selection_probability_changed")
    return frame


def sample_owner(
    rows: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    *,
    per_stratum: int,
    seed: int,
    story_grouping_reviewed: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Freeze gold selection before gold labels, from a genuine census only.

    Proxy grouping proposes the finite story frame; it is not evidence that the
    stories are independent. The separate grouping-review attestation concerns
    the whole frame, before seeing owner push/key labels.
    """
    if candidate.get("phase") != "fit":
        raise ValueError("news_reader_eval_frozen_fit_candidate_required")
    if per_stratum <= 0 or any(row["labeler"] == "owner" for row in rows):
        raise ValueError("news_reader_eval_owner_selection_must_precede_gold_labels")
    sources = [row.get("case_sampling", row) for row in rows]
    frames = [source.get("sampling_frame") for source in sources]
    if (
        not frames
        or not isinstance(frames[0], Mapping)
        or any(frame != frames[0] for frame in frames)
        or frames[0].get("unit") != "claim_decision"
        or frames[0].get("units") != len(rows)
        or any(source.get("inclusion_probability") != 1 for source in sources)
    ):
        raise ValueError("news_reader_eval_complete_claim_census_required_for_story_frame")
    verify_candidate(rows, candidate)
    holdout = set(candidate["split"]["certification"])
    stories: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["case_id"] in holdout:
            stories[row.get("split_story_id", row["story_id"])].append(row)
    representatives = [min(group, key=lambda row: (_time(row), row["case_id"])) for group in stories.values()]
    strata: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in representatives:
        source = row.get("case_sampling", row)
        if not source.get("stratum"):
            raise ValueError("news_reader_eval_owner_frame_stratum_required")
        strata[source["stratum"]].append(row)
    sizes = {stratum: len(group) for stratum, group in sorted(strata.items())}
    boundary_date = datetime.fromtimestamp(candidate["split"]["boundary_ms"] / 1000, UTC).date().isoformat()
    source_days = frames[0].get("days")
    if not isinstance(source_days, list) or not source_days:
        raise ValueError("news_reader_eval_frozen_census_calendar_required")
    holdout_days = sorted(day for day in source_days if day >= boundary_date)
    frame = {
        "unit": "independent_story_representative",
        "scope": "holdout",
        "boundary_ms": candidate["split"]["boundary_ms"],
        "units": len(representatives),
        "stratum_sizes": sizes,
        "certification_strata": sorted(sizes),
        "days": holdout_days,
        "partial_first_day": candidate["split"]["boundary_ms"] % 86400000 != 0,
        "selection_frozen_before_labels": True,
        "story_grouping_reviewed": story_grouping_reviewed,
        "source_claim_frame_id": frames[0]["frame_id"],
        "selection_design": "stratified_srs_without_replacement",
        "candidate_identity": candidate["candidate_identity"],
        "population_target": (
            "one predetermined representative per frozen census holdout story; not all production claims or Event/cards"
        ),
    }
    rng = random.Random(seed)  # noqa: S311 -- reproducible sampling, not a cryptographic operation.
    selected, roster = [], []
    for stratum, group in sorted(strata.items()):
        ordered_group = sorted(group, key=lambda row: row["case_id"])
        count = min(per_stratum, len(ordered_group))
        picked = {row["case_id"] for row in rng.sample(ordered_group, count)}
        for row in ordered_group:
            roster.append(
                {
                    "case_id": row["case_id"],
                    "split_story_id": row.get("split_story_id", row["story_id"]),
                    "stratum": stratum,
                }
            )
            if row["case_id"] in picked:
                selected.append(
                    {
                        "case_id": row["case_id"],
                        "reader_input": row["reader_input"],
                        "sampling_design": "stratified",
                        "sampling_unit": frame["unit"],
                        "sampling_frame": frame,
                        "stratum": stratum,
                        "inclusion_probability": count / len(ordered_group),
                    }
                )
    frame["selected_case_ids"] = sorted(row["case_id"] for row in selected)
    frame["frame_id"] = digest({"frame": frame, "representatives": sorted(row["case_id"] for row in representatives)})
    manifest = {
        "protocol": PROTOCOL,
        "candidate_identity": candidate["candidate_identity"],
        "holdout_identity": candidate["holdout_identity"],
        "sampling_frame": frame,
        "selection": {"per_stratum": per_stratum, "seed": seed},
        "story_roster": roster,
        "selected": [{"case_id": row["case_id"], "input_sha256": digest(row["reader_input"])} for row in selected],
        "independence_status": "reviewed" if story_grouping_reviewed else "proxy_grouping_unverified_cannot_certify",
    }
    return selected, manifest


def clopper_pearson_upper(successes: int, trials: int, delta: float = DELTA) -> float:
    return 1 - clopper_pearson_lower(trials - successes, trials, delta)


def certify_sequence(
    rows: Sequence[Mapping[str, Any]],
    probabilities: Mapping[str, float],
    *,
    target: float,
    minimum: int,
    delta: float = DELTA / 2,
    cuts: Sequence[float] = CUT_SEQUENCE,
    eligible: set[str] | None = None,
    field: str = "push",
) -> dict[str, Any]:
    if list(cuts) != sorted(set(cuts), reverse=True) or not cuts or any(not 0 <= cut <= 1 for cut in cuts):
        raise ValueError("news_reader_eval_strict_to_loose_fixed_sequence_required")
    if any(row["labeler"] != "owner" for row in rows):
        raise ValueError("news_reader_eval_proxy_labels_cannot_certify")
    if len({row["story_id"] for row in rows}) != len(rows):
        raise ValueError("news_reader_eval_independent_story_representatives_required")
    observed_strata = _sampling_strata(rows)
    frame = sampling_frame(rows)
    strata = sorted(frame["stratum_sizes"])
    if not set(observed_strata).issubset(strata) or (observed_strata == ["uniform"] and strata != ["uniform"]):
        raise ValueError("news_reader_eval_sampling_stratum_not_in_frame")
    story_strata: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        story_strata[row["story_id"]].add("uniform" if observed_strata == ["uniform"] else row["stratum"])
    if any(len(values) > 1 for values in story_strata.values()):
        raise ValueError("news_reader_eval_story_crosses_sampling_strata")
    tested, chosen = [], None
    for cut in cuts:
        selected = [
            row
            for row in rows
            if row["case_id"] in probabilities
            and probabilities[row["case_id"]] >= cut
            and (eligible is None or row["case_id"] in eligible)
        ]
        stratum_reports = []
        numerator_lower = denominator_upper = 0.0
        for stratum in strata:
            cases = [
                row for row in selected if ("uniform" if observed_strata == ["uniform"] else row["stratum"]) == stratum
            ]
            sampled = [
                row for row in rows if ("uniform" if observed_strata == ["uniform"] else row["stratum"]) == stratum
            ]
            n = len(cases)
            k = sum(row["label"][field] is True if field == "key" else row["label"][field] == "push" for row in cases)
            population_fraction = frame["stratum_sizes"][stratum] / frame["units"]
            alpha = delta / (2 * len(strata))
            joint_lower = clopper_pearson_lower(k, len(sampled), alpha)
            selection_upper = clopper_pearson_upper(n, len(sampled), alpha)
            numerator_lower += population_fraction * joint_lower
            denominator_upper += population_fraction * selection_upper
            stratum_reports.append(
                {
                    "stratum": stratum,
                    "owner_labels": len(cases),
                    "independent_stories": n,
                    "successful_stories": k,
                    "sampled_stories": len(sampled),
                    "population_stories": frame["stratum_sizes"][stratum],
                    "population_fraction": population_fraction,
                    "delta": delta if strata == ["uniform"] else alpha,
                    "lower_bound": clopper_pearson_lower(k, n, delta) if strata == ["uniform"] else None,
                    "selected_positive_joint_lower": joint_lower,
                    "selected_mass_upper": selection_upper,
                }
            )
        # Pre-frozen population masses, not sample quotas. Simultaneous bounds
        # on joint positive selection and selection mass yield a valid ratio.
        # A zero-selected stratum remains present with an upper unknown mass;
        # it is never dropped after looking at model outputs or owner labels.
        lower = (
            stratum_reports[0]["lower_bound"]
            if strata == ["uniform"]
            else numerator_lower / denominator_upper
            if denominator_upper
            else 0.0
        )
        independent = sum(report["independent_stories"] for report in stratum_reports)
        passed = lower >= target and independent >= minimum
        result = {
            "cut": cut,
            "lower_bound": lower,
            "independent_stories": independent,
            "owner_labels": len(selected),
            "passed": passed,
            "strata": stratum_reports,
        }
        tested.append(result)
        if not passed:
            break
        chosen = result
    return {
        "status": "certified" if chosen else "uncalibrated",
        "selected": chosen,
        "tested": tested,
        "target": target,
        "delta": delta,
        "minimum_independent_stories": minimum,
        "precision_estimand": "selected independent story representatives in the frozen holdout frame",
        "bound_method": "selected-binomial CP" if strata == ["uniform"] else "simultaneous joint/selection CP ratio",
        "sampling_frame_id": frame["frame_id"],
        "stop_rule": "first failure; no later cuts inspected",
        "labels": "owner only",
    }


def certify(rows: Sequence[Mapping[str, Any]], artifact: Mapping[str, Any]) -> dict[str, Any]:
    if artifact["phase"] != "fit":
        raise ValueError("news_reader_eval_frozen_fit_candidate_required")
    if artifact["split"] != split_cases(rows):
        raise ValueError("news_reader_eval_certification_split_changed")
    if artifact.get("frozen_dataset_sha256") != frozen_dataset_digest(rows, artifact["split"]["certification"]):
        raise ValueError("news_reader_eval_frozen_dataset_changed")
    if (
        artifact["kind_floor"] != KIND_FLOOR
        or artifact["questions_identity"] != READER_QUESTIONS_IDENTITY
        or artifact["eligibility_table_sha256"] != digest(PUSHABLE_KINDS)
    ):
        raise ValueError("news_reader_eval_questions_or_eligibility_changed")
    if artifact["cut_sequence"] != fit_cut_sequences(
        artifact["oof_predictions"], len(artifact["split"]["certification"])
    ):
        raise ValueError("news_reader_eval_certification_sequence_changed")
    verify_candidate(rows, artifact)
    answers = recorded(rows, artifact["backend"])
    if artifact["answer_provenance"] != answer_provenance(rows, answers, artifact["backend"]):
        raise ValueError("news_reader_eval_model_adapter_changed")
    if artifact["guide_versions"] != [GUIDE_VERSION] or any(row["guide_version"] != GUIDE_VERSION for row in rows):
        raise ValueError("news_reader_eval_owner_guide_changed")
    owner = [row for row in rows if row["case_id"] in artifact["split"]["certification"] and row["labeler"] == "owner"]
    for row in owner:
        applicable(row)
    model_owner = [row for row in owner if applicable(row) and row["case_id"] in answers]
    calibration = ReaderCalibration(**artifact["calibration"])
    push, key, eligible = {}, {}, set()
    for row in model_owner:
        push_x, key_x = features(row, answers[row["case_id"]], calibration.materiality_floor)
        push[row["case_id"]] = _predict(calibration.push_coefficients, [push_x])[0]
        key[row["case_id"]] = _predict(calibration.key_coefficients, [key_x])[0]
        if reader_scores(answers[row["case_id"]], calibration=calibration).e >= KIND_FLOOR:
            eligible.add(row["case_id"])
    report = {
        **artifact,
        "phase": "certify",
        "fitting_source_dataset_sha256": artifact["dataset_sha256"],
        "dataset_sha256": dataset_digest(rows),
        "certification_owner_cases": len(owner),
        "certification_available_reader_cases": len(model_owner),
        "release_ready": False,
        "holdout_protocol": {
            "candidate_identity": artifact["candidate_identity"],
            "holdout_identity": artifact["holdout_identity"],
            "candidate_derivation_verified": True,
            "rule": "one frozen candidate per holdout/backend; no tuning or repeated candidate search after access",
            "limitation": (
                "reconstruction and local ledger do not prove absence of external holdout access; owner review required"
            ),
        },
        "diagnostics": diagnostic_report(owner, answers, backend=artifact["backend"], calibration=calibration),
        "population": {
            "precision": (
                "reader-selected available answers among independent story representatives in frozen owner holdout"
            ),
            "probability_quality": "available reader-applicable owner holdout, conditional on answered calls",
            "recall_and_volume": (
                "full owner holdout; fixed notifications included, failed/absent reader calls do not push"
            ),
            "fixed_case_count": len(owner) - sum(applicable(row) for row in owner),
        },
    }
    if not owner:
        report.update(certification_status="uncalibrated", certification_failure="owner_probability_sample_required")
        return finalize_report(report)
    try:
        _sampling_strata(owner)
        frame = sampling_frame(owner)
        if frame.get("boundary_ms") != artifact["split"]["boundary_ms"]:
            raise ValueError("news_reader_eval_sampling_frame_holdout_changed")
        if len({row["story_id"] for row in owner}) != len(owner):
            raise ValueError("news_reader_eval_independent_story_representatives_required")
    except ValueError as exc:
        report.update(certification_status="uncalibrated", certification_failure=str(exc))
        return finalize_report(report)
    report["sampling_frame"] = frame
    report["population_probability_sample_verified"] = True
    push_report = probability_report(
        [int(row["label"]["push"] == "push") for row in model_owner],
        [push[row["case_id"]] for row in model_owner],
        [_weight(row) for row in model_owner],
    )
    key_report = probability_report(
        [int(row["label"]["key"]) for row in model_owner],
        [key[row["case_id"]] for row in model_owner],
        [_weight(row) for row in model_owner],
    )
    # Same-input baseline is mandatory; a historical aggregate AUC is not paired evidence.
    prior_auc = baseline_auc(model_owner, artifact["backend"])
    discrimination_passed = (
        prior_auc is not None
        and push_report["auc"] is not None
        and push_report["auc"] >= prior_auc
        and key_report["auc"] is not None
        and key_report["auc"] >= 0.7
    )
    report.update(
        holdout_metrics={"push": push_report, "key": key_report, "same_input_v3_auc": prior_auc},
        discrimination_passed=discrimination_passed,
        diagnostics=diagnostic_report(owner, answers, backend=artifact["backend"], calibration=calibration),
    )
    if not discrimination_passed:
        report.update(
            certification_status="uncalibrated", certification_failure="discrimination_or_paired_baseline_unverified"
        )
        return finalize_report(report)
    push_certificate = certify_sequence(
        owner, push, target=PUSH_TARGET, minimum=PUSH_MINIMUM, eligible=eligible, cuts=artifact["cut_sequence"]["push"]
    )
    pushed = (
        set()
        if push_certificate["selected"] is None
        else {case for case in eligible if push[case] >= push_certificate["selected"]["cut"]}
    )
    # Selection of a push cut uses the same labels; protect key certification
    # against that adaptivity by testing every possible push conditioning cut
    # with Bonferroni alpha, each using its own fixed key sequence.
    key_certificates = {
        str(cut): certify_sequence(
            owner,
            key,
            target=KEY_TARGET,
            minimum=KEY_MINIMUM,
            delta=DELTA / (2 * len(artifact["cut_sequence"]["push"])),
            eligible={case for case in eligible if push[case] >= cut},
            field="key",
            cuts=artifact["cut_sequence"]["key"],
        )
        for cut in artifact["cut_sequence"]["push"]
    }
    key_certificate = (
        key_certificates[str(push_certificate["selected"]["cut"])] if push_certificate["selected"] else None
    )
    certified = (
        push_certificate["selected"] is not None
        and key_certificate is not None
        and key_certificate["selected"] is not None
    )
    reviewed = replace(
        calibration,
        push_cut=push_certificate["selected"]["cut"] if push_certificate["selected"] else None,
        key_cut=key_certificate["selected"]["cut"] if key_certificate and key_certificate["selected"] else None,
        certification_status="certified" if certified else "uncalibrated",
    )
    keys = {case for case in pushed if reviewed.key_cut is not None and key[case] >= reviewed.key_cut}
    report.update(
        calibration=asdict(reviewed),
        certification_status=reviewed.certification_status,
        push_certificate=push_certificate,
        key_certificate=key_certificate,
        key_conditioning_certificates=key_certificates,
        volume=volume_report(
            owner, pushed | {row["case_id"] for row in owner if row.get("deterministic_decision") == "notify"}, keys
        ),
        diagnostics=diagnostic_report(
            owner, answers, backend=artifact["backend"], calibration=reviewed, pushed_case_ids=pushed, key_case_ids=keys
        ),
    )
    if not certified:
        report["certification_failure"] = (
            "push_precision_or_sample_count"
            if push_certificate["selected"] is None
            else "key_precision_or_sample_count"
        )
    return finalize_report(report)


def finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Precision certification is one release gate; missing evidence never becomes a pass."""
    diagnostics = report.get("diagnostics", {})
    gates = {
        "statistical_precision": report.get("certification_status") == "certified",
        "paired_discrimination": report.get("discrimination_passed"),
        "probability_sampling_population": report.get("population_probability_sample_verified", False),
        "reader_coverage_complete": diagnostics.get("coverage", {}).get("complete"),
        "type_false_ineligible_at_most_five_percent": diagnostics.get("report_kind", {}).get(
            "five_percent_gate_passed"
        ),
        "end_to_end_push_recall_at_least_v3": diagnostics.get("recall", {}).get("at_least_paired_v3"),
        "materiality_ambiguity_below_twenty_percent": diagnostics.get("materiality", {}).get(
            "below_historical_reference"
        ),
        "paired_p90_latency_within_500ms": diagnostics.get("latency", {}).get("within_500ms"),
        "event_card_daily_replay": None,
        "volume_targets_or_explicit_owner_waiver": None,
        "owner_review": None,
        "holdout_usage_review": None,
    }
    report["release_gates"] = gates
    report["release_ready"] = all(value is True for value in gates.values())
    report["release_requirements"] = [name for name, value in gates.items() if value is not True]
    return report


def volume_report(rows: Sequence[Mapping[str, Any]], pushed: set[str], keys: set[str]) -> dict[str, Any]:
    if any(row.get("inclusion_probability") is None for row in rows):
        return {"status": "unestimated", "reason": "inclusion_probability_missing"}
    days: dict[str, dict[str, float]] = defaultdict(lambda: {"push": 0.0, "key": 0.0})
    frame_days = {day for row in rows for day in (row.get("sampling_frame") or {}).get("days", [])}
    for day in frame_days:
        days[day]
    for row in rows:
        day = row["reader_input"]["as_of"]
        days[day]  # Include sampled days with zero pushes in P10/P50/P90.
        for field, selected in (("push", pushed), ("key", keys)):
            if row["case_id"] in selected:
                days[day][field] += _weight(row)
    quantiles = {}
    for field in ("push", "key"):
        values = sorted(value[field] for value in days.values())
        quantiles[field] = {
            f"p{p}": values[min(len(values) - 1, int((len(values) - 1) * p / 100))] if values else None
            for p in (10, 50, 90)
        }
    unit = (rows[0].get("sampling_frame") or {}).get("unit") if rows else None
    return {
        "status": "estimated",
        "estimand": (
            "HT story representatives by frozen ReaderInput.as_of; no inferred all-claim/Event/card weights"
            if unit == "independent_story_representative"
            else "Horvitz-Thompson claims by frozen ReaderInput.as_of; no inferred Event/card weights"
        ),
        "days": dict(days),
        "quantiles": quantiles,
        "guardrail_only": True,
        "calendar_complete": bool(frame_days),
        "sampled_cases": len(rows),
        "volume_acceptance": "owner review required; volume never moves cuts",
    }


def render_report(artifact: Mapping[str, Any], evidence_path: str | None = None) -> str:
    """Render recorded evidence only; do not fit, select cuts or change artifacts."""
    status = artifact.get("certification_status", "uncalibrated")
    lines = [
        f"# News reader {artifact['backend']} calibration",
        "",
        f"Status: **{status}**.",
        "",
        f"Dataset SHA-256: `{artifact['dataset_sha256']}`.",
        f"Questions: `{artifact['questions_identity']}`.",
        f"Guides: {', '.join(artifact['guide_versions'])}.",
        f"Release ready: **{str(artifact.get('release_ready', False)).lower()}**.",
        "",
        "Claude is a fitting proxy. Precision certification uses owner labels and independent stories.",
        "No provider reask, production cache write, notification, deployment or empirical evidence is synthesized.",
        "",
        "```json",
        json.dumps(
            {
                key: artifact[key]
                for key in (
                    "answer_provenance",
                    "calibration",
                    "certification_failure",
                    "holdout_metrics",
                    "diagnostics",
                    "push_certificate",
                    "key_certificate",
                    "volume",
                    "release_requirements",
                    "release_gates",
                    "holdout_protocol",
                    "population",
                )
                if key in artifact
            },
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
    ]
    if evidence_path:
        lines.extend([f"Machine-readable evidence: [{Path(evidence_path).name}]({evidence_path}).", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="phase", required=True)
    assemble_parser = commands.add_parser("assemble", help="Join labels and exact-input reask journals into JSONL.")
    assemble_parser.add_argument("--input", type=Path, required=True)
    assemble_parser.add_argument("--labels", type=Path, action="append", required=True)
    assemble_parser.add_argument("--native-journal", type=Path)
    assemble_parser.add_argument("--generated-journal", type=Path)
    assemble_parser.add_argument("--output", type=Path, required=True)
    fit_parser = commands.add_parser("fit", help="Freeze time/story split and fit an uncalibrated candidate.")
    fit_parser.add_argument("--backend", choices=("native", "generated"), required=True)
    fit_parser.add_argument("--input", type=Path, required=True)
    fit_parser.add_argument("--output", type=Path, required=True)
    owner_parser = commands.add_parser(
        "owner-sample", help="Freeze blinded gold selection from a complete proxy story census before owner labels."
    )
    owner_parser.add_argument("--input", type=Path, required=True)
    owner_parser.add_argument("--candidate", type=Path, required=True)
    owner_parser.add_argument("--per-stratum", type=int, required=True)
    owner_parser.add_argument("--seed", type=int, required=True)
    owner_parser.add_argument(
        "--story-grouping-reviewed",
        action="store_true",
        help="Attest independent review of the complete proxy story grouping before gold selection.",
    )
    owner_parser.add_argument("--output", type=Path, required=True)
    owner_parser.add_argument("--manifest", type=Path, required=True)
    cert_parser = commands.add_parser("certify", help="Certify the frozen candidate with independent owner labels.")
    cert_parser.add_argument("--input", type=Path, required=True)
    cert_parser.add_argument("--candidate", type=Path, required=True)
    cert_parser.add_argument("--output", type=Path, required=True)
    cert_parser.add_argument(
        "--holdout-ledger", type=Path, required=True, help="Persistent one-candidate-per-holdout usage journal."
    )
    report_parser = commands.add_parser("report", help="Read recorded evidence and render a review document.")
    report_parser.add_argument("--artifact", type=Path, required=True)
    report_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "assemble":
        cases = read_jsonl(args.input)
        labels = [row for path in args.labels for row in read_jsonl(path)]
        journals: dict[ReaderBackend, Sequence[Mapping[str, Any]]] = {}
        if args.native_journal is not None:
            journals["native"] = read_jsonl(args.native_journal)
        if args.generated_journal is not None:
            journals["generated"] = read_jsonl(args.generated_journal)
        write_jsonl(args.output, assemble(cases, labels, journals))
        return
    elif args.phase == "fit":
        result = json.dumps(fit(load(args.input), args.backend), ensure_ascii=False, indent=2) + "\n"
    elif args.phase == "owner-sample":
        if args.output.resolve() == args.manifest.resolve():
            raise ValueError("news_reader_eval_owner_blind_and_manifest_paths_must_differ")
        selection, manifest = sample_owner(
            load(args.input),
            json.loads(args.candidate.read_text("utf-8")),
            per_stratum=args.per_stratum,
            seed=args.seed,
            story_grouping_reviewed=args.story_grouping_reviewed,
        )
        write_jsonl(args.output, selection)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        with args.manifest.open("w", encoding="utf-8") as stream:
            args.manifest.chmod(0o600)
            stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        return
    elif args.phase == "certify":
        candidate = json.loads(args.candidate.read_text("utf-8"))
        rows = load(args.input)
        verify_candidate(rows, candidate)
        register_holdout_use(args.holdout_ledger, candidate, certification_dataset_sha256=dataset_digest(rows))
        certificate = certify(rows, candidate)
        certificate["holdout_protocol"].update(
            ledger_ref=str(args.holdout_ledger.resolve()),
            local_registration=True,
            ledger_records_sha256=digest(read_jsonl(args.holdout_ledger)),
        )
        result = json.dumps(certificate, ensure_ascii=False, indent=2) + "\n"
    else:
        result = render_report(json.loads(args.artifact.read_text("utf-8")), str(args.artifact.resolve()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result, encoding="utf-8")


if __name__ == "__main__":
    main()
