"""Offline reader calibration: fit, owner selection, certification, then read-only report.

Run each phase separately. No provider, database, cache or sender is constructed.
Old importance scores are historical evidence, never converted into new answers.
Claude labels can train a candidate; only independent owner labels certify it.
Install optional fitting dependencies with ``uv sync --group research``.

Certification population: one representative per independent story in a complete claim census after the
candidate's time boundary. Every representative is scored by the frozen candidate, so the stories any cut
selects are known exactly; owner labels only estimate the push (key) rate among them, stratum by stratum.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.news_reader_diagnostics import baseline_auc, diagnostic_report
from scripts.news_reader_io import dataset_sha256, read_jsonl, write_jsonl
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, ReaderNovelty, reader_novelty
from tracefold.news.notifications.policy import (
    KIND_FLOOR,
    LOGIT_EPSILON,
    PUSHABLE_KINDS,
    ReaderCalibration,
    _logit,
    logistic,
    novelty_outcome,
    reader_anchor_held,
    reader_scores,
    reader_vectors,
)
from tracefold.news.notifications.reader import READER_QUESTIONS_IDENTITY, ReaderBackend, ReaderInput, ReaderJudgment
from tracefold.news.updates.contracts import ClaimFields
from tracefold.news.updates.identity import digest, identity

PROTOCOL = "news_reader_calibration_v4"
FIT_CONFIG: dict[str, Any] = {
    "C": 1.0,
    "solver": "lbfgs",
    "max_iter": 2000,
    "materiality_floors": [1, 2, 3],
    "folds": 5,
    "sklearn_version": "1.9.1",
}
PUSH_TARGET, KEY_TARGET = 0.65, 0.75
# One-sided error per field; push and key together keep a family error of 0.1 per backend.
FIELD_DELTA = 0.05
PUSH_MINIMUM, KEY_MINIMUM = 150, 60
# Frozen before labels from scores only: the candidate's push region, key candidates outside it, the rest.
STRATA = ("push_region", "key_region", "rest")
STORY_RULE = "union of claim-Event membership and recorded claim links over the complete census"
REPRESENTATIVE_RULE = (
    "earliest member by (first_available_at_ms, case_id); stories with a member before the boundary excluded"
)
SCORE_TOLERANCE = 1e-12
_GUIDE = re.compile(r"^news_reader_owner_guide_v\d+:[0-9a-f]{64}$")


def load(path: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    if not rows or len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("news_reader_eval_empty_or_duplicate_cases")
    for row in rows:
        ReaderInput.model_validate(row["reader_input"])
        if not row.get("story_id") or not row.get("guide_version") or not row.get("labeler"):
            raise ValueError("news_reader_eval_label_provenance_required")
        if not _GUIDE.match(str(row["guide_version"])):
            raise ValueError("news_reader_eval_guide_version_invalid")
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


def guide_version(rows: Sequence[Mapping[str, Any]]) -> str:
    """The one labeling guide every row was labeled under; it need not be the current code guide."""
    versions = {row.get("guide_version") for row in rows}
    if len(versions) != 1 or not _GUIDE.match(str(version := next(iter(versions)))):
        raise ValueError("news_reader_eval_single_guide_version_required")
    return str(version)


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
    guide_version(labels)
    for raw in labels:
        case, labeler = raw["case_id"], raw["labeler"]
        if case not in inputs or raw.get("reader_input_sha256") != digest(inputs[case]["reader_input"]):
            raise ValueError("news_reader_eval_label_input_changed")
        if (case, labeler) in seen:
            raise ValueError("news_reader_eval_duplicate_case_labeler")
        seen.add((case, labeler))
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


def _judgment(record: Mapping[str, Any], reader_input: Mapping[str, Any], backend: ReaderBackend) -> ReaderJudgment:
    """One exact-input answer to the current questions from the requested backend."""
    if "importance" in record:
        raise ValueError("news_reader_eval_historical_scores_require_real_reask")
    if record.get("questions_identity") != READER_QUESTIONS_IDENTITY:
        raise ValueError("news_reader_eval_questions_changed")
    if record.get("input_sha256") != digest(reader_input):
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
    count = len(reader_input["messages"])
    if (judgment.anchor is None) != (count == 0) or (
        judgment.anchor is not None and len(judgment.anchor.probabilities) != count + 1
    ):
        raise ValueError("news_reader_eval_answer_shape_mismatch")
    return judgment


def recorded(rows: Sequence[Mapping[str, Any]], backend: ReaderBackend) -> dict[str, ReaderJudgment]:
    answers = {}
    for row in rows:
        record = row.get("answers", {}).get(backend)
        if record is not None:
            answers[row["case_id"]] = _judgment(record, row["reader_input"], backend)
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


def features(row: Mapping[str, Any], judgment: ReaderJudgment, floor: int) -> tuple[list[float], list[float]]:
    # Reuse the production held definition, including effective-action bypass.
    if novelty_outcome(row["reader_novelty"], first_available_at_ms=_time(row)) is not None:
        raise ValueError("news_reader_eval_deterministic_exception_not_calibration_case")
    if judgment.status != "available":
        raise ValueError("news_reader_judgment_unavailable")
    _, held = reader_anchor_held(
        row["reader_novelty"],
        judgment,
        message_intents=row["message_intents"],
        calibration=ReaderCalibration(materiality_floor=floor),
        claim_fields=ClaimFields.model_validate(row["reader_input"]["claim"]["fields"]),
    )
    return reader_vectors(judgment, held=held, materiality_floor=floor)


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
    version = guide_version(rows)
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
                for index in validate:
                    oof[index] = logistic(coefficients, x[index])
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
            "guide_version": version,
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
        # Owner labels that certify this candidate must be labeled under the same guide.
        "guide_version": version,
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
    }
    artifact["candidate_identity"] = candidate_identity(artifact)
    return artifact


DERIVATION_FIELDS = (
    "protocol",
    "backend",
    "questions_identity",
    "eligibility_table_sha256",
    "kind_floor",
    "dataset_sha256",
    "guide_version",
    "split",
    "calibration",
    "cut_sequence",
    "answer_provenance",
    "fit_cases",
    "materiality_candidates",
    "oof_predictions",
    "fit_config",
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


def clopper_pearson_lower(successes: int, trials: int, delta: float) -> float:
    """One-sided exact lower bound: invert the binomial upper tail; no pseudo-counts or fractional weights."""
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


# --------------------------------------------------------------------------------------------------------------
# Holdout story frame over a complete claim census, scored by the frozen candidate.

_CENSUS_FIELDS = (
    "case_id",
    "claim_ref",
    "event_id",
    "first_available_at_ms",
    "decided_at_ms",
    "reader_input",
    "message_intents",
)


def census_claims(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate a complete claim-decision census and keep each claim's first recorded decision."""
    if not rows or len({row["case_id"] for row in rows}) != len(rows):
        raise ValueError("news_reader_eval_empty_or_duplicate_cases")
    frames = [row.get("sampling_frame") for row in rows]
    if (
        not isinstance(frames[0], Mapping)
        or any(frame != frames[0] for frame in frames)
        or frames[0].get("unit") != "claim_decision"
        or frames[0].get("units") != len(rows)
        or any(row.get("inclusion_probability") != 1 for row in rows)
    ):
        raise ValueError("news_reader_eval_complete_claim_census_required")
    first: dict[str, dict[str, Any]] = {}
    for raw in sorted(rows, key=lambda row: (row.get("decided_at_ms", 0), row["case_id"])):
        if any(raw.get(field) is None for field in _CENSUS_FIELDS):
            raise ValueError("news_reader_eval_census_case_fields_required")
        ReaderInput.model_validate(raw["reader_input"])
        if len(raw["message_intents"]) != len(raw["reader_input"]["messages"]):
            raise ValueError("news_reader_eval_message_intents_mismatch")
        if raw["claim_ref"] in first:
            continue
        row = dict(raw)
        novelty = reader_novelty(
            row["claim_ref"],
            [ClaimLink.model_validate(link) for link in row.get("links", [])],
            [LinkedReceipt.model_validate(receipt) for receipt in row.get("receipts", [])],
        )
        if "novelty" in row and novelty != ReaderNovelty.model_validate(row["novelty"]):
            raise ValueError("news_reader_eval_novelty_drift")
        row["reader_novelty"] = novelty
        first[row["claim_ref"]] = row
    return sorted(first.values(), key=lambda row: row["case_id"])


def story_frame(claims: Sequence[Mapping[str, Any]], boundary_ms: int) -> dict[str, Any]:
    """Independent holdout stories and their representatives.

    A story is the union of claim-Event membership and recorded claim links over the whole census, so a story
    reaching back before the candidate's boundary is excluded rather than split.
    """
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(node: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        parent[find(left)] = find(right)

    for row in claims:
        union(("claim", row["claim_ref"]), ("event", row["event_id"]))
        for link in row.get("links", []):
            union(("claim", link["current_ref"]), ("claim", link["previous_ref"]))
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in claims:
        groups[find(("claim", row["claim_ref"]))].append(row)
    representatives, crossing, holdout_claims = {}, 0, 0
    for members in groups.values():
        times = [int(row["first_available_at_ms"]) for row in members]
        if min(times) < boundary_ms:
            crossing += max(times) >= boundary_ms
            continue
        story = identity("news_reader_story", sorted(row["claim_ref"] for row in members))
        representatives[story] = min(members, key=lambda row: (int(row["first_available_at_ms"]), row["case_id"]))
        holdout_claims += len(members)
    return {
        "representatives": dict(sorted(representatives.items())),
        "census_claims": len(claims),
        "holdout_claims": holdout_claims,
        "stories": len(representatives),
        "boundary_crossing_stories": crossing,
    }


def journal_answers(
    cases: Mapping[str, Mapping[str, Any]],
    journals: Sequence[Mapping[str, Any]],
    backend: ReaderBackend,
) -> tuple[dict[str, ReaderJudgment], dict[str, Mapping[str, Any]], dict[str, Any] | None]:
    """Exact-input answers for frozen cases; failed and skipped calls stay without an answer.

    Journals may also cover other cases (for example the fitting sample); only these cases are read.
    """
    latest: dict[str, Mapping[str, Any]] = {}
    for record in journals:
        case = record["case_id"]
        if case not in cases:
            continue
        if record.get("input_sha256") != digest(cases[case]["reader_input"]):
            raise ValueError("news_reader_eval_journal_input_changed")
        if case in latest and not (latest[case].get("error_code") or latest[case].get("error_class")):
            raise ValueError("news_reader_eval_duplicate_successful_journal_case")
        latest[case] = record
    answers, records = {}, {}
    provenance = set()
    for case, record in latest.items():
        records[case] = record
        if record.get("skipped") or record.get("error_code") or record.get("error_class"):
            continue
        answers[case] = judgment = _judgment(record, cases[case]["reader_input"], backend)
        provenance.add((judgment.identity, judgment.served_model, record["program_identity"]))
    if len(provenance) > 1:
        raise ValueError("news_reader_eval_single_model_adapter_identity_required")
    adapter = None
    if provenance:
        identity_, served_model, program = next(iter(provenance))
        adapter = {"adapter_identity": identity_, "served_model": served_model, "program_identity": program}
    return answers, records, adapter


def score_representatives(
    representatives: Sequence[Mapping[str, Any]],
    answers: Mapping[str, ReaderJudgment],
    candidate: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Frozen-candidate scores for every representative; fixed rules and missing answers cannot be selected."""
    calibration = ReaderCalibration(**candidate["calibration"])
    scores: dict[str, dict[str, Any]] = {}
    for row in representatives:
        case = row["case_id"]
        if not applicable(row) or novelty_outcome(row["reader_novelty"], first_available_at_ms=_time(row)) is not None:
            scores[case] = {"scored": False, "reason": "fixed_rule", "eligible": False, "p_push": None, "p_key": None}
            continue
        if case not in answers:
            scores[case] = {"scored": False, "reason": "no_answer", "eligible": False, "p_push": None, "p_key": None}
            continue
        push_x, key_x = features(row, answers[case], calibration.materiality_floor)
        e = reader_scores(answers[case], calibration=calibration).e
        scores[case] = {
            "scored": True,
            "e": e,
            "eligible": e >= KIND_FLOOR,
            "p_push": logistic(calibration.push_coefficients, push_x),
            "p_key": logistic(calibration.key_coefficients, key_x),
        }
    return scores


def stratum_of(score: Mapping[str, Any], *, push_cut: float, key_cut: float) -> str:
    if score["scored"] and score["eligible"] and score["p_push"] >= push_cut:
        return "push_region"
    if score["scored"] and score["eligible"] and score["p_key"] >= key_cut:
        return "key_region"
    return "rest"


def _sample_size(value: Any) -> int | str:
    if value == "all":
        return "all"
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError("news_reader_eval_owner_sample_size_invalid")


def draw_selection(strata: Mapping[str, str], sizes: Mapping[str, Any], seed: int) -> dict[str, str]:
    """Simple random samples without replacement, stratum by stratum in fixed order; `all` is a census."""
    rng = random.Random(seed)  # noqa: S311 -- reproducible sampling, not a cryptographic operation.
    selected = {}
    for name in STRATA:
        population = sorted(case for case, stratum in strata.items() if stratum == name)
        size = _sample_size(sizes[name])
        count = len(population) if size == "all" else min(int(size), len(population))
        for case in rng.sample(population, count):
            selected[case] = name
    return dict(sorted(selected.items()))


def owner_frame(
    census: Sequence[Mapping[str, Any]],
    journals: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Recompute the holdout frame, the representatives' answers and the frozen candidate's scores."""
    if candidate.get("phase") != "fit" or candidate.get("protocol") != PROTOCOL:
        raise ValueError("news_reader_eval_frozen_fit_candidate_required")
    if (
        candidate["kind_floor"] != KIND_FLOOR
        or candidate["questions_identity"] != READER_QUESTIONS_IDENTITY
        or candidate["eligibility_table_sha256"] != digest(PUSHABLE_KINDS)
    ):
        raise ValueError("news_reader_eval_questions_or_eligibility_changed")
    claims = census_claims(census)
    boundary = int(candidate["split"]["boundary_ms"])
    stories = story_frame(claims, boundary)
    representatives = list(stories["representatives"].values())
    answers, records, provenance = journal_answers(
        {row["case_id"]: row for row in representatives}, journals, candidate["backend"]
    )
    if provenance is not None and provenance != candidate["answer_provenance"]:
        raise ValueError("news_reader_eval_model_adapter_changed")
    boundary_day = datetime.fromtimestamp(boundary / 1000, UTC).date().isoformat()
    census_days = census[0]["sampling_frame"].get("days")
    if not isinstance(census_days, list) or not census_days:
        raise ValueError("news_reader_eval_frozen_census_calendar_required")
    return {
        "census_dataset_sha256": dataset_sha256(census),
        "summary": {
            "unit": "independent_story_representative",
            "scope": "holdout",
            "boundary_ms": boundary,
            "census_claim_decisions": len(census),
            "census_claims": stories["census_claims"],
            "holdout_claims": stories["holdout_claims"],
            "stories": stories["stories"],
            "boundary_crossing_stories": stories["boundary_crossing_stories"],
            "story_rule": STORY_RULE,
            "representative_rule": REPRESENTATIVE_RULE,
            "days": sorted(day for day in census_days if day >= boundary_day),
            "partial_first_day": boundary % 86_400_000 != 0,
        },
        "stories": {story: row["case_id"] for story, row in stories["representatives"].items()},
        "representatives": {row["case_id"]: row for row in representatives},
        "answers": answers,
        "records": records,
        "scores": score_representatives(representatives, answers, candidate),
    }


def check_selection(
    selection: Mapping[str, Any], frame: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, str]:
    """A frozen selection is valid only for these recomputed scores and its own score-only strata."""
    push_cut, key_cut = selection.get("push_cut"), selection.get("key_cut")
    if (
        not isinstance(push_cut, float)
        or not isinstance(key_cut, float)
        or push_cut not in candidate["cut_sequence"]["push"]
        or key_cut not in candidate["cut_sequence"]["key"]
    ):
        raise ValueError("news_reader_eval_selection_cut_not_in_candidate_sequence")
    if selection.get("frozen_before_labels") is not True:
        raise ValueError("news_reader_eval_selection_must_precede_owner_labels")
    sizes = selection.get("sample_sizes")
    if not isinstance(sizes, Mapping) or set(sizes) != set(STRATA):
        raise ValueError("news_reader_eval_owner_sample_size_invalid")
    declared = selection.get("representatives")
    scores = frame["scores"]
    if not isinstance(declared, Mapping) or set(declared) != set(scores):
        raise ValueError("news_reader_eval_selection_frame_changed")
    for case, score in scores.items():
        claimed = declared[case]
        if bool(claimed.get("eligible")) != score["eligible"] or any(
            (claimed.get(field) is None) != (score[field] is None)
            or (score[field] is not None and not math.isclose(claimed[field], score[field], abs_tol=SCORE_TOLERANCE))
            for field in ("p_push", "p_key")
        ):
            raise ValueError("news_reader_eval_selection_scores_changed")
    strata = {case: stratum_of(score, push_cut=push_cut, key_cut=key_cut) for case, score in scores.items()}
    selected = selection.get("selected")
    if not isinstance(selected, Mapping) or any(strata.get(case) != stratum for case, stratum in selected.items()):
        raise ValueError("news_reader_eval_selection_stratum_changed")
    for name in STRATA:
        population = sum(stratum == name for stratum in strata.values())
        size = _sample_size(sizes[name])
        expected = population if size == "all" else min(int(size), population)
        if sum(stratum == name for stratum in selected.values()) != expected:
            raise ValueError("news_reader_eval_selection_sample_size_changed")
    return strata


def selection_manifest(
    candidate: Mapping[str, Any],
    frame: Mapping[str, Any],
    selection: Mapping[str, Any],
    *,
    external: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    strata = check_selection(selection, frame, candidate)
    populations = Counter(strata.values())
    sampled = Counter(selection["selected"].values())
    manifest = {
        "protocol": PROTOCOL,
        "phase": "owner_selection",
        "backend": candidate["backend"],
        "candidate_identity": candidate["candidate_identity"],
        "guide_version": candidate["guide_version"],
        "census_dataset_sha256": frame["census_dataset_sha256"],
        "frame": frame["summary"],
        "stories": frame["stories"],
        "selection": {
            "push_cut": selection["push_cut"],
            "key_cut": selection["key_cut"],
            "seed": selection.get("seed"),
            "sample_sizes": dict(selection["sample_sizes"]),
            "representatives": {
                case: {field: score[field] for field in ("scored", "eligible", "p_push", "p_key")}
                for case, score in sorted(frame["scores"].items())
            },
            "selected": dict(sorted(selection["selected"].items())),
            "frozen_before_labels": True,
        },
        "strata": {
            name: {
                "population": populations[name],
                "sampled": sampled[name],
                "inclusion_probability": sampled[name] / populations[name] if populations[name] else None,
            }
            for name in STRATA
        },
        "external_selection": None if external is None else dict(external),
    }
    manifest["selection_id"] = digest(manifest)
    return manifest


def sample_owner(
    census: Sequence[Mapping[str, Any]],
    journals: Sequence[Mapping[str, Any]],
    fit_rows: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    *,
    push_cut: float | None = None,
    key_cut: float | None = None,
    sizes: Mapping[str, Any] | None = None,
    seed: int | None = None,
    frozen: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Freeze the owner selection from scores only, before any owner label.

    Either draw it (cuts from the candidate's sequences, per-stratum sizes or `all`, a seed) or validate a
    selection frozen elsewhere before labels by recomputing the frame and every representative's score.
    """
    verify_candidate(fit_rows, candidate)
    frame = owner_frame(census, journals, candidate)
    external = None
    if frozen is None:
        if push_cut is None or key_cut is None or sizes is None or seed is None:
            raise ValueError("news_reader_eval_owner_selection_arguments_required")
        strata = {
            case: stratum_of(score, push_cut=push_cut, key_cut=key_cut) for case, score in frame["scores"].items()
        }
        selection = {
            "push_cut": push_cut,
            "key_cut": key_cut,
            "seed": seed,
            "sample_sizes": dict(sizes),
            "representatives": frame["scores"],
            "selected": draw_selection(strata, sizes, seed),
            "frozen_before_labels": True,
        }
    else:
        selection = dict(frozen)
        strata = check_selection(frozen, frame, candidate)
        reproduced = None
        if isinstance(frozen.get("seed"), int):
            reproduced = draw_selection(strata, frozen["sample_sizes"], frozen["seed"]) == dict(frozen["selected"])
        external = {"selection_sha256": digest(frozen), "seed_reproduces_selection": reproduced}
    manifest = selection_manifest(candidate, frame, selection, external=external)
    case_story = {case: story for story, case in frame["stories"].items()}
    blind_frame = {
        "unit": "independent_story_representative",
        "selection_id": manifest["selection_id"],
        "selected_case_ids": sorted(manifest["selection"]["selected"]),
        "selection_frozen_before_labels": True,
    }
    rows = []
    for case, stratum in manifest["selection"]["selected"].items():
        row = frame["representatives"][case]
        if not isinstance(row.get("source_texts"), list) or not row["source_texts"]:
            raise ValueError("news_reader_eval_selected_source_text_required")
        rows.append(
            {
                "case_id": case,
                "claim_ref": row["claim_ref"],
                "reader_input": row["reader_input"],
                "source_texts": row["source_texts"],
                "story_id": case_story[case],
                "stratum": stratum,
                "inclusion_probability": manifest["strata"][stratum]["inclusion_probability"],
                "sampling_design": "stratified",
                "sampling_unit": blind_frame["unit"],
                "sampling_frame": blind_frame,
                "guide_version": candidate["guide_version"],
            }
        )
    return rows, manifest


# --------------------------------------------------------------------------------------------------------------
# Certification of a known selected population.


def certify_sequence(
    *,
    scores: Mapping[str, float],
    strata: Mapping[str, str],
    labels: Mapping[str, bool],
    cuts: Sequence[float],
    target: float,
    minimum: int,
    delta: float,
) -> dict[str, Any]:
    """Fixed sequence, strict to loose, stopping at the first failure.

    For each cut the selected population S_c = {score >= c} is known exactly. In stratum h, N_h = |S_c ∩ h|,
    n_h owner-labelled members of S_c ∩ h, k_h positives. The bound is Σ N_h · CP(k_h, n_h, δ/H) / Σ N_h over
    the H strata with N_h > 0; a stratum with no labelled member contributes 0. The sequence starts at the
    first cut whose population reaches the minimum (scores only); a passing cut also needs that many labels.
    """
    if not cuts or list(cuts) != sorted(set(cuts), reverse=True) or any(not 0 <= cut <= 1 for cut in cuts):
        raise ValueError("news_reader_eval_strict_to_loose_fixed_sequence_required")
    if not set(scores) <= set(strata) or not set(labels) <= set(strata):
        raise ValueError("news_reader_eval_certification_outside_frame")
    populations = {cut: sum(score >= cut for score in scores.values()) for cut in cuts}
    start = next((index for index, cut in enumerate(cuts) if populations[cut] >= minimum), None)
    sequence = [] if start is None else list(cuts[start:])
    tested: list[dict[str, Any]] = []
    chosen = None
    for cut in sequence:
        selected = {case for case, score in scores.items() if score >= cut}
        counts = {}
        for name in STRATA:
            members = [case for case in selected if strata[case] == name]
            labelled = [case for case in members if case in labels]
            counts[name] = (len(members), len(labelled), sum(labels[case] for case in labelled))
        active = [name for name, (size, _, _) in counts.items() if size]
        alpha = delta / len(active)
        bounds = {
            name: clopper_pearson_lower(positives, labelled, alpha) if labelled else 0.0
            for name, (_, labelled, positives) in counts.items()
            if name in active
        }
        reports = [
            {
                "stratum": name,
                "population": size,
                "labelled": labelled,
                "positives": positives,
                "delta": alpha if name in active else None,
                "lower_bound": bounds.get(name),
            }
            for name, (size, labelled, positives) in counts.items()
        ]
        population = sum(counts[name][0] for name in active)
        lower = sum(counts[name][0] * bounds[name] for name in active) / population
        labelled_total = sum(counts[name][1] for name in active)
        positives = sum(counts[name][2] for name in active)
        result = {
            "cut": cut,
            "population": population,
            "independent_stories": labelled_total,
            "positives": positives,
            "labelled_precision": positives / labelled_total if labelled_total else None,
            "lower_bound": lower,
            "passed": lower >= target and labelled_total >= minimum,
            "strata": reports,
        }
        tested.append(result)
        if not result["passed"]:
            break
        chosen = result
    return {
        "status": "certified" if chosen else "uncalibrated",
        "selected": chosen,
        "tested": tested,
        "sequence": sequence,
        "populations": {str(cut): count for cut, count in populations.items()},
        "failure": None
        if chosen
        else "population_below_minimum"
        if start is None
        else "precision_or_labelled_stories_below_minimum",
        "target": target,
        "delta": delta,
        "minimum_independent_stories": minimum,
        "precision_estimand": "frozen-candidate-selected independent story representatives in the holdout frame",
        "bound_method": "known selected population; stratified one-sided Clopper-Pearson, Bonferroni over strata",
        "stop_rule": "first failure; no later cuts inspected",
        "labels": "owner only",
    }


def _owner_labels(
    labels: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any], frame: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    owner = {row["case_id"]: dict(row) for row in labels}
    if len(owner) != len(labels) or set(owner) != set(manifest["selection"]["selected"]):
        raise ValueError("news_reader_eval_selected_owner_labels_incomplete")
    case_story = {case: story for story, case in manifest["stories"].items()}
    for case, row in owner.items():
        label = row.get("label") or {}
        if row.get("labeler") != "owner":
            raise ValueError("news_reader_eval_proxy_labels_cannot_certify")
        if row.get("guide_version") != manifest["guide_version"]:
            raise ValueError("news_reader_eval_owner_guide_changed")
        if row.get("reader_input_sha256") != digest(frame["representatives"][case]["reader_input"]):
            raise ValueError("news_reader_eval_label_input_changed")
        if row.get("story_id") != case_story[case]:
            raise ValueError("news_reader_eval_independent_story_representatives_required")
        if label.get("push") not in {"push", "borderline", "feed"} or not isinstance(label.get("key"), bool):
            raise ValueError("news_reader_eval_current_labels_required")
        if label["key"] and label["push"] != "push":
            raise ValueError("news_reader_eval_key_requires_push")
        if label.get("kind") not in PUSHABLE_KINDS:
            raise ValueError("news_reader_eval_label_invalid")
    return owner


def certify(
    census: Sequence[Mapping[str, Any]],
    journals: Sequence[Mapping[str, Any]],
    fit_rows: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    manifest: Mapping[str, Any],
    labels: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Certify the frozen candidate on owner labels of its frozen holdout selection; no fitting or cut search."""
    verify_candidate(fit_rows, candidate)
    frame = owner_frame(census, journals, candidate)
    if (
        manifest.get("protocol") != PROTOCOL
        or manifest.get("phase") != "owner_selection"
        or manifest.get("candidate_identity") != candidate["candidate_identity"]
        or manifest.get("guide_version") != candidate["guide_version"]
        or manifest.get("census_dataset_sha256") != frame["census_dataset_sha256"]
        or manifest.get("frame") != frame["summary"]
        or manifest.get("stories") != frame["stories"]
        or manifest.get("selection_id")
        != digest({key: value for key, value in manifest.items() if key != "selection_id"})
    ):
        raise ValueError("news_reader_eval_owner_selection_changed")
    strata = check_selection(manifest["selection"], frame, candidate)
    owner = _owner_labels(labels, manifest, frame)
    backend: ReaderBackend = candidate["backend"]
    calibration = ReaderCalibration(**candidate["calibration"])
    scores = frame["scores"]
    eligible = {case: score for case, score in scores.items() if score["scored"] and score["eligible"]}
    push_scores = {case: float(score["p_push"]) for case, score in eligible.items()}
    push_cuts = [cut for cut in candidate["cut_sequence"]["push"] if cut >= manifest["selection"]["push_cut"]]
    push_certificate = certify_sequence(
        scores=push_scores,
        strata=strata,
        labels={case: row["label"]["push"] == "push" for case, row in owner.items()},
        cuts=push_cuts,
        target=PUSH_TARGET,
        minimum=PUSH_MINIMUM,
        delta=FIELD_DELTA,
    )
    push_cut = push_certificate["selected"]["cut"] if push_certificate["selected"] else None
    pushed = set() if push_cut is None else {case for case, score in push_scores.items() if score >= push_cut}
    key_certificate: dict[str, Any] = {"status": "not_tested", "failure": "push_uncertified", "selected": None}
    if push_cut is not None:
        # Key implies push. The push cut was chosen with these labels, so the key error is split over every push
        # cut the sequence could have certified.
        key_certificate = certify_sequence(
            scores={case: float(eligible[case]["p_key"]) for case in pushed},
            strata=strata,
            labels={case: bool(row["label"]["key"]) for case, row in owner.items()},
            cuts=[cut for cut in candidate["cut_sequence"]["key"] if cut >= manifest["selection"]["key_cut"]],
            target=KEY_TARGET,
            minimum=KEY_MINIMUM,
            delta=FIELD_DELTA / len(push_certificate["sequence"]),
        )
    key_cut = key_certificate["selected"]["cut"] if key_certificate.get("selected") else None
    keys = set() if key_cut is None else {case for case in pushed if eligible[case]["p_key"] >= key_cut}
    reviewed = replace(
        calibration,
        push_cut=push_cut,
        key_cut=key_cut,
        certification_status="certified" if push_cut is not None else "uncalibrated",
    )
    rows = []
    for case, label in sorted(owner.items()):
        stratum = manifest["selection"]["selected"][case]
        record = frame["records"].get(case)
        answered = case in frame["answers"]
        rows.append(
            {
                **frame["representatives"][case],
                **{key: label[key] for key in ("label", "labeler", "guide_version", "story_id")},
                "stratum": stratum,
                "sampling_design": "stratified",
                "inclusion_probability": manifest["strata"][stratum]["inclusion_probability"],
                "answers": {backend: record} if answered else {},
                "reask_failures": {backend: record} if record is not None and not answered else {},
            }
        )
    model_rows = [row for row in rows if scores[row["case_id"]]["scored"]]
    push_report = probability_report(
        [int(row["label"]["push"] == "push") for row in model_rows],
        [float(scores[row["case_id"]]["p_push"]) for row in model_rows],
        [_weight(row) for row in model_rows],
    )
    key_report = probability_report(
        [int(row["label"]["key"]) for row in model_rows],
        [float(scores[row["case_id"]]["p_key"]) for row in model_rows],
        [_weight(row) for row in model_rows],
    )
    prior_auc = baseline_auc(model_rows, backend)
    discrimination = (
        None
        if prior_auc is None or push_report["auc"] is None
        else push_report["auc"] >= prior_auc and (key_cut is None or (key_report["auc"] or 0) >= 0.7)
    )
    certification_dataset = {
        "census_dataset_sha256": frame["census_dataset_sha256"],
        "selection_id": manifest["selection_id"],
        "owner_labels_sha256": dataset_sha256(sorted(labels, key=lambda row: row["case_id"])),
    }
    report = {
        "protocol": PROTOCOL,
        "phase": "certify",
        "backend": backend,
        "questions_identity": READER_QUESTIONS_IDENTITY,
        "eligibility_table_sha256": digest(PUSHABLE_KINDS),
        "kind_floor": KIND_FLOOR,
        "guide_version": candidate["guide_version"],
        "candidate_identity": candidate["candidate_identity"],
        "fit_dataset_sha256": candidate["dataset_sha256"],
        **certification_dataset,
        "dataset_sha256": digest(certification_dataset),
        "holdout_identity": digest(
            {
                "backend": backend,
                "census_dataset_sha256": frame["census_dataset_sha256"],
                "boundary_ms": frame["summary"]["boundary_ms"],
            }
        ),
        "answer_provenance": candidate["answer_provenance"],
        "calibration": asdict(reviewed),
        "certification_status": reviewed.certification_status,
        "certification_scope": "push and key"
        if key_cut is not None
        else "push only; no claim is key"
        if push_cut is not None
        else "none",
        "push_certificate": push_certificate,
        "key_certificate": key_certificate,
        "frame": frame["summary"],
        "strata": manifest["strata"],
        "selection": {key: manifest["selection"][key] for key in ("push_cut", "key_cut", "seed", "sample_sizes")},
        "external_selection": manifest["external_selection"],
        "owner_labels": len(owner),
        "owner_label_sources": dict(Counter(row.get("label_sources", {}).get("push", "owner") for row in labels)),
        "holdout_metrics": {"push": push_report, "key": key_report, "same_input_v3_auc": prior_auc},
        "discrimination_passed": discrimination,
        "population_probability_sample_verified": True,
        "diagnostics": diagnostic_report(
            rows, frame["answers"], backend=backend, calibration=reviewed, pushed_case_ids=pushed, key_case_ids=keys
        ),
        "volume": volume_report(frame["summary"]["days"], list(frame["representatives"].values()), pushed, keys),
        "release_ready": False,
        "holdout_protocol": {
            "candidate_identity": candidate["candidate_identity"],
            "candidate_derivation_verified": True,
            "rule": "one frozen candidate per holdout census and backend; no tuning or candidate search after access",
            "limitation": (
                "reconstruction and local ledger do not prove absence of external holdout access; owner review required"
            ),
        },
    }
    if push_cut is None:
        report["certification_failure"] = push_certificate["failure"]
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


def volume_report(
    days: Sequence[str], representatives: Sequence[Mapping[str, Any]], pushed: set[str], keys: set[str]
) -> dict[str, Any]:
    """Exact selected story representatives per frozen as_of day; every representative is scored."""
    counts: dict[str, dict[str, int]] = {day: {"push": 0, "key": 0} for day in days}
    for row in representatives:
        day = counts.setdefault(row["reader_input"]["as_of"], {"push": 0, "key": 0})
        day["push"] += row["case_id"] in pushed
        day["key"] += row["case_id"] in keys
    return {
        "estimand": (
            "holdout story representatives selected at the certified cuts, by ReaderInput.as_of; "
            "not claims, Events or cards"
        ),
        "days": dict(sorted(counts.items())),
        "totals": {"push": len(pushed), "key": len(keys)},
        "guardrail_only": True,
        "volume_acceptance": "owner review required; volume never moves cuts",
    }


def render_report(artifact: Mapping[str, Any], evidence_name: str | None = None) -> str:
    """Render recorded evidence only; do not fit, select cuts or change artifacts."""
    status = artifact.get("certification_status", "uncalibrated")
    lines = [
        f"# News reader {artifact['backend']} calibration",
        "",
        f"Status: **{status}** ({artifact.get('certification_scope', 'none')}).",
        "",
        f"Dataset SHA-256: `{artifact['dataset_sha256']}`.",
        f"Candidate: `{artifact.get('candidate_identity')}`.",
        f"Questions: `{artifact['questions_identity']}`.",
        f"Guide: `{artifact.get('guide_version')}`.",
        f"Release ready: **{str(artifact.get('release_ready', False)).lower()}**.",
        "",
        "Claude is a fitting proxy. Precision certification uses owner labels and independent stories.",
        "No provider reask, production cache write, notification, deployment or empirical evidence is synthesized.",
        "",
    ]
    for field in ("push", "key"):
        proof = artifact.get(f"{field}_certificate") or {}
        lines.extend(
            f"- {field} cut {result['cut']}: {result['population']} selected stories, "
            f"{result['independent_stories']} owner-labelled, {result['positives']} positive, "
            f"lower bound {result['lower_bound']:.3f} ({'pass' if result['passed'] else 'fail'})."
            for result in proof.get("tested", [])
        )
        if proof and not proof.get("tested"):
            lines.append(f"- {field}: not tested ({proof.get('failure')}).")
    lines.extend(
        [
            "",
            "```json",
            json.dumps(
                {
                    key: artifact[key]
                    for key in (
                        "answer_provenance",
                        "calibration",
                        "certification_failure",
                        "frame",
                        "strata",
                        "selection",
                        "external_selection",
                        "holdout_metrics",
                        "diagnostics",
                        "push_certificate",
                        "key_certificate",
                        "volume",
                        "release_requirements",
                        "release_gates",
                        "holdout_protocol",
                    )
                    if key in artifact
                },
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
        ]
    )
    if evidence_name:
        lines.extend([f"Machine-readable evidence: `{evidence_name}`.", ""])
    return "\n".join(lines)


def _sizes(value: str) -> int | str:
    return "all" if value == "all" else int(value)


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
    for name, text in (
        ("owner-sample", "Score the holdout story frame and freeze the owner selection before labels."),
        ("certify", "Certify the frozen candidate with owner labels of its frozen selection."),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--census", type=Path, required=True, help="Complete claim census (exporter --census).")
        command.add_argument("--journal", type=Path, action="append", required=True, help="Candidate-backend reasks.")
        command.add_argument("--fit-input", type=Path, required=True, help="The candidate's fitting dataset.")
        command.add_argument("--candidate", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    owner_parser = commands.choices["owner-sample"]
    owner_parser.add_argument("--manifest", type=Path, required=True)
    owner_parser.add_argument("--push-cut", type=float, help="Loosest push cut tested; from the candidate sequence.")
    owner_parser.add_argument("--key-cut", type=float, help="Loosest key cut tested; from the candidate sequence.")
    owner_parser.add_argument("--push-sample", type=_sizes, help="push_region sample size or all.")
    owner_parser.add_argument("--key-sample", type=_sizes, help="key_region sample size or all.")
    owner_parser.add_argument("--rest-sample", type=_sizes, help="rest sample size or all.")
    owner_parser.add_argument("--seed", type=int)
    owner_parser.add_argument(
        "--frozen-selection", type=Path, help="Validate a selection frozen elsewhere before labels instead of drawing."
    )
    cert_parser = commands.choices["certify"]
    cert_parser.add_argument("--selection", type=Path, required=True, help="owner-sample manifest.")
    cert_parser.add_argument("--labels", type=Path, required=True, help="import-owner label journal.")
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
    if args.phase == "fit":
        result = json.dumps(fit(load(args.input), args.backend), ensure_ascii=False, indent=2) + "\n"
    elif args.phase in {"owner-sample", "certify"}:
        census = read_jsonl(args.census)
        journal = [row for path in args.journal for row in read_jsonl(path)]
        fit_rows = load(args.fit_input)
        candidate = json.loads(args.candidate.read_text("utf-8"))
        if args.phase == "owner-sample":
            if args.output.resolve() == args.manifest.resolve():
                parser.error("selection rows and manifest must be separate paths")
            drawn = (args.push_cut, args.key_cut, args.push_sample, args.key_sample, args.rest_sample, args.seed)
            if (args.frozen_selection is None) == any(value is None for value in drawn) or (
                args.frozen_selection is not None and any(value is not None for value in drawn)
            ):
                parser.error("give either every draw argument or --frozen-selection")
            selection, manifest = sample_owner(
                census,
                journal,
                fit_rows,
                candidate,
                push_cut=args.push_cut,
                key_cut=args.key_cut,
                sizes=None
                if args.push_sample is None
                else {"push_region": args.push_sample, "key_region": args.key_sample, "rest": args.rest_sample},
                seed=args.seed,
                frozen=None if args.frozen_selection is None else json.loads(args.frozen_selection.read_text("utf-8")),
            )
            write_jsonl(args.output, selection)
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            with args.manifest.open("w", encoding="utf-8") as stream:
                args.manifest.chmod(0o600)
                stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            return
        manifest = json.loads(args.selection.read_text("utf-8"))
        labels = read_jsonl(args.labels)
        certificate = certify(census, journal, fit_rows, candidate, manifest, labels)
        register_holdout_use(
            args.holdout_ledger,
            certificate,
            certification_dataset_sha256=certificate["owner_labels_sha256"],
        )
        certificate["holdout_protocol"].update(
            local_registration=True, ledger_records_sha256=digest(read_jsonl(args.holdout_ledger))
        )
        result = json.dumps(certificate, ensure_ascii=False, indent=2) + "\n"
    else:
        result = render_report(json.loads(args.artifact.read_text("utf-8")), args.artifact.name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result, encoding="utf-8")


if __name__ == "__main__":
    main()
