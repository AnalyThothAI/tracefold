"""Descriptive quality gates over owner labels and exact-input v3 baseline records.

No fitting, model requests, caches or statistical precision certificates live
here. The retired rubric identity is a research control, never a runtime alias.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from tracefold.news.notifications.policy import KIND_FLOOR, PUSHABLE_KINDS, ReaderCalibration, reader_scores
from tracefold.news.notifications.reader import REPORT_KIND_OPTIONS, ReaderBackend, ReaderJudgment
from tracefold.news.updates.identity import digest

# Exact reader-v3 question identity at the implementation base 08df9b2b0.
BASELINE_QUESTIONS_IDENTITY = "news_reader_questions:aba3b9079321ba142cf556214573d19921ac52359c164839c807f0ac0622c52d"


def _weight(row: Mapping[str, Any]) -> float:
    probability = row.get("inclusion_probability")
    return 1.0 if probability is None else 1 / probability


def baseline_records(rows: Sequence[Mapping[str, Any]], backend: ReaderBackend) -> dict[str, Mapping[str, Any]] | None:
    records = {}
    for row in rows:
        record = row.get("baseline_v3")
        if record is None:
            return None
        if (
            not isinstance(record, Mapping)
            or record.get("input_sha256") != digest(row["reader_input"])
            or record.get("backend") != backend
            or record.get("questions_identity") != BASELINE_QUESTIONS_IDENTITY
            or not isinstance(record.get("adapter_identity"), str)
            or not record["adapter_identity"]
            or not isinstance(record.get("rank_score"), (int, float))
            or not math.isfinite(record["rank_score"])
            or not 0 <= record["rank_score"] <= 4
            or not isinstance(record.get("pushed"), bool)
        ):
            raise ValueError("news_reader_eval_paired_baseline_provenance_invalid")
        records[row["case_id"]] = record
    if len({record["adapter_identity"] for record in records.values()}) > 1:
        raise ValueError("news_reader_eval_paired_baseline_adapter_changed")
    return records


def baseline_auc(rows: Sequence[Mapping[str, Any]], backend: ReaderBackend) -> float | None:
    records = baseline_records(rows, backend)
    if records is None:
        return None
    positive = [row for row in rows if row["label"]["push"] == "push"]
    negative = [row for row in rows if row["label"]["push"] != "push"]
    total = sum(_weight(row) for row in positive) * sum(_weight(row) for row in negative)
    if total == 0:
        return None
    return (
        sum(
            _weight(p)
            * _weight(n)
            * (
                1
                if records[p["case_id"]]["rank_score"] > records[n["case_id"]]["rank_score"]
                else 0.5
                if records[p["case_id"]]["rank_score"] == records[n["case_id"]]["rank_score"]
                else 0
            )
            for p in positive
            for n in negative
        )
        / total
    )


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    at = (len(ordered) - 1) * probability
    left, right = math.floor(at), math.ceil(at)
    return ordered[left] + (ordered[right] - ordered[left]) * (at - left)


def diagnostic_report(
    rows: Sequence[Mapping[str, Any]],
    answers: Mapping[str, ReaderJudgment],
    *,
    backend: ReaderBackend,
    calibration: ReaderCalibration,
    pushed_case_ids: set[str] | None = None,
    key_case_ids: set[str] | None = None,
) -> dict[str, Any]:
    evaluated = [row for row in rows if row["case_id"] in answers]
    kinds = [kind for kind, _ in REPORT_KIND_OPTIONS]
    positions = {kind: index for index, kind in enumerate(kinds)}
    confusion = [[0 for _ in kinds] for _ in kinds]
    weighted = [[0.0 for _ in kinds] for _ in kinds]
    bad_type = bad_gate = ambiguous = total = positive_total = 0.0
    current_latency, paired_latency, old_latency = [], [], []
    baseline = baseline_records(evaluated, backend)
    for row in evaluated:
        answer = answers[row["case_id"]]
        if answer.report_kind is None:
            raise ValueError("news_reader_eval_diagnostics_answer_unavailable")
        weight = _weight(row)
        observed, predicted = positions[row["label"]["kind"]], positions[answer.report_kind.value]
        confusion[observed][predicted] += 1
        weighted[observed][predicted] += weight
        scores = reader_scores(answer, calibration=calibration)
        total += weight
        ambiguous += weight * (0.35 <= scores.m <= 0.65)
        if row["label"]["push"] == "push":
            positive_total += weight
            bad_type += weight * (not PUSHABLE_KINDS[answer.report_kind.value])
            bad_gate += weight * (scores.e < KIND_FLOOR)
        duration = row.get("answers", {}).get(backend, {}).get("duration_ms")
        if isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0:
            current_latency.append(duration)
            prior = None if baseline is None else baseline[row["case_id"]].get("duration_ms")
            if isinstance(prior, (int, float)) and math.isfinite(prior) and prior >= 0:
                paired_latency.append(duration)
                old_latency.append(prior)
    push_recall = baseline_recall = key_recall = None
    if pushed_case_ids is not None and positive_total:
        push_recall = (
            sum(
                _weight(row)
                for row in evaluated
                if row["label"]["push"] == "push" and row["case_id"] in pushed_case_ids
            )
            / positive_total
        )
        if baseline is not None:
            baseline_recall = (
                sum(
                    _weight(row)
                    for row in evaluated
                    if row["label"]["push"] == "push" and baseline[row["case_id"]]["pushed"]
                )
                / positive_total
            )
    key_total = sum(_weight(row) for row in evaluated if row["label"]["key"])
    if key_case_ids is not None and key_total:
        key_recall = (
            sum(_weight(row) for row in evaluated if row["label"]["key"] and row["case_id"] in key_case_ids) / key_total
        )
    new_p90, old_p90 = _quantile(paired_latency, 0.9), _quantile(old_latency, 0.9)
    wrong_type_fraction = bad_type / positive_total if positive_total else None
    ambiguous_fraction = ambiguous / total if total else None
    return {
        "cases": len(evaluated),
        "descriptive_only": True,
        "report_kind": {
            "categories": kinds,
            "owner_by_predicted_confusion": confusion,
            "weighted_confusion": weighted,
            "pushable_label_predicted_ineligible_fraction": wrong_type_fraction,
            "pushable_label_below_kind_floor_fraction": bad_gate / positive_total if positive_total else None,
            "five_percent_gate_passed": None if wrong_type_fraction is None else wrong_type_fraction <= 0.05,
        },
        "materiality": {
            "ambiguous_fraction": ambiguous_fraction,
            "historical_v3_reference_fraction": 0.2,
            "below_historical_reference": None if ambiguous_fraction is None else ambiguous_fraction < 0.2,
        },
        "recall": {
            "push": push_recall,
            "paired_v3": baseline_recall,
            "key": key_recall,
            "at_least_paired_v3": None
            if push_recall is None or baseline_recall is None
            else push_recall >= baseline_recall,
        },
        "latency": {
            "available_current_cases": len(current_latency),
            "current_p90_ms": _quantile(current_latency, 0.9),
            "paired_cases": len(paired_latency),
            "paired_current_p90_ms": new_p90,
            "paired_v3_p90_ms": old_p90,
            "within_500ms": None if new_p90 is None or old_p90 is None else new_p90 <= old_p90 + 500,
        },
    }
