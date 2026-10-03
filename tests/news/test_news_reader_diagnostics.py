"""Quality readouts require paired evidence; missing metrics never become passes."""

from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.news_reader_diagnostics import BASELINE_QUESTIONS_IDENTITY, baseline_auc, diagnostic_report
from tracefold.news.notifications.policy import PUSHABLE_KINDS, ReaderCalibration
from tracefold.news.notifications.reader import ReaderJudgment
from tracefold.news.updates.identity import digest


def _row(number: int, *, push: str = "push", weight: int = 1, kind: str = "new_action") -> dict:
    reader_input = {
        "as_of": "2026-10-02",
        "claim": {"statement": f"Launch {number}", "fields": {"subject": "Project", "action": "launch"}},
        "sources": [{"publisher": "fixture", "quote": f"Launch {number}"}],
        "messages": [],
    }
    return {
        "case_id": str(number),
        "reader_input": reader_input,
        "inclusion_probability": 1 / weight,
        "label": {"kind": kind, "push": push, "key": False},
        "answers": {"native": {"duration_ms": 1000 + number * 1500}},
        "baseline_v3": {
            "input_sha256": digest(reader_input),
            "backend": "native",
            "questions_identity": BASELINE_QUESTIONS_IDENTITY,
            "adapter_identity": "old-v3-fixture",
            "rank_score": 3 if push == "push" else 1,
            "pushed": push == "push",
            "duration_ms": 1000 + number * 200,
        },
    }


def _answer(kind: str = "new_action", *, m: float = 0.8) -> ReaderJudgment:
    return ReaderJudgment.model_validate(
        {
            "status": "available",
            "backend": "native",
            "identity": "synthetic-diagnostic",
            "report_kind": {
                "value": kind,
                "probabilities": {key: float(key == kind) for key in PUSHABLE_KINDS},
                "confidence": 0.8,
            },
            "materiality": {"value": 1 + m, "probabilities": [0, 1 - m, m, 0], "confidence": 0.8},
            "interrupt": {"probabilities": [0.8, 0.2], "confidence": 0.8},
        }
    )


def test_paired_auc_and_recall_need_bound_baseline_evidence() -> None:
    rows = [_row(0), _row(1, push="feed", kind="background")]
    assert baseline_auc(rows, "native") == 1
    report = diagnostic_report(
        rows,
        {"0": _answer(), "1": _answer("background")},
        backend="native",
        calibration=ReaderCalibration(),
        pushed_case_ids={"0"},
    )
    assert report["recall"] == {
        "push": 1,
        "paired_v3": 1,
        "recorded_production": None,
        "key": None,
        "at_least_paired_v3": True,
    }
    # The decision production recorded for the same claims is descriptive, never the paired v3 control.
    recorded = deepcopy(rows)
    recorded[0]["original_decision"] = "not_notified"
    recorded[1]["original_decision"] = "notify"
    report = diagnostic_report(
        recorded,
        {"0": _answer(), "1": _answer("background")},
        backend="native",
        calibration=ReaderCalibration(),
        pushed_case_ids={"0"},
    )
    assert report["recall"]["recorded_production"] == 0 and report["recall"]["at_least_paired_v3"] is True
    missing = deepcopy(rows)
    missing[0].pop("baseline_v3")
    missing[0]["baseline_v3_probability"] = 0.99
    assert baseline_auc(missing, "native") is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("input_sha256", "wrong-input"),
        ("backend", "generated"),
        ("questions_identity", "older-rubric"),
        ("adapter_identity", ""),
        ("rank_score", float("nan")),
        ("pushed", "yes"),
    ],
)
def test_baseline_rejects_mismatched_provenance_and_invalid_score(field: str, value) -> None:
    row = _row(0)
    row["baseline_v3"][field] = value
    with pytest.raises(ValueError, match="paired_baseline_provenance_invalid"):
        baseline_auc([row], "native")


def test_mixed_baseline_model_adapters_are_not_one_control() -> None:
    rows = [_row(0), _row(1, push="feed")]
    rows[1]["baseline_v3"]["adapter_identity"] = "different-model"
    with pytest.raises(ValueError, match="paired_baseline_adapter_changed"):
        baseline_auc(rows, "native")


def test_weighted_type_confusion_counts_a_missed_push_and_ambiguous_materiality() -> None:
    rows = [_row(0, weight=10), _row(1)]
    report = diagnostic_report(
        rows,
        {"0": _answer("commentary", m=0.5), "1": _answer()},
        backend="native",
        calibration=ReaderCalibration(),
        pushed_case_ids={"1"},
    )
    assert report["report_kind"]["pushable_label_predicted_ineligible_fraction"] == pytest.approx(10 / 11)
    assert report["report_kind"]["five_percent_gate_passed"] is False
    assert report["materiality"]["ambiguous_fraction"] == pytest.approx(10 / 11)
    assert report["recall"]["push"] == pytest.approx(1 / 11)
    assert report["recall"]["at_least_paired_v3"] is False
    # IPW empirical CDF: the fast case represents ten units, the slow one one.
    assert report["latency"]["paired_current_p90_ms"] == 1000
    assert report["latency"]["paired_v3_p90_ms"] == 1000
    assert report["latency"]["within_500ms"] is True


def test_no_observed_latencies_or_selected_cut_does_not_claim_quality() -> None:
    row = _row(0)
    row["answers"]["native"].pop("duration_ms")
    report = diagnostic_report([row], {"0": _answer()}, backend="native", calibration=ReaderCalibration())
    assert report["latency"]["within_500ms"] is None
    assert report["recall"]["at_least_paired_v3"] is None


def test_failed_new_reader_call_keeps_gold_baseline_and_timeout_in_denominators() -> None:
    rows = [_row(0), _row(1)]
    rows[1]["answers"] = {}
    rows[1]["reask_failures"] = {"native": {"error_code": "timeout", "duration_ms": 12000}}
    report = diagnostic_report(
        rows, {"0": _answer()}, backend="native", calibration=ReaderCalibration(), pushed_case_ids={"0"}
    )
    assert report["cases"] == 2 and report["available_cases"] == 1
    assert report["coverage"]["weighted_available_fraction"] == 0.5
    assert report["recall"]["push"] == 0.5
    assert report["recall"]["paired_v3"] == 1
    assert report["recall"]["at_least_paired_v3"] is False
    assert report["latency"]["paired_current_p90_ms"] == 12000
    assert report["latency"]["within_500ms"] is False
    assert report["report_kind"]["five_percent_gate_passed"] is None


def test_unattempted_or_missing_duration_cannot_pass_latency_and_fixed_notify_counts() -> None:
    rows = [_row(0), _row(1), _row(2)]
    rows[1]["answers"] = {}
    rows[2].update(reader_applicable=False, deterministic_decision="notify")
    report = diagnostic_report(
        rows, {"0": _answer()}, backend="native", calibration=ReaderCalibration(), pushed_case_ids={"0"}
    )
    assert report["recall"]["push"] == pytest.approx(2 / 3)
    assert report["recall"]["paired_v3"] == 1
    assert report["coverage"]["applicable_cases"] == 2
    assert report["latency"]["complete"] is False
    assert report["latency"]["within_500ms"] is None
