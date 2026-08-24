"""#160: active calibration uses typed v4 judgments; the v1 corpus stays immutable audit evidence.

The policy-v8 fixture cannot be projected into the hard-cut contracts, by
design. Its old `production_verdict`, `recorded_action` and Gate `priority`
shape is retained byte-for-byte so the epoch boundary is reviewable. The
separate v2 fixture is the reproducible policy-v10 / metric-v4 calibration.
"""

from __future__ import annotations

import gzip
import hashlib
from typing import Any

import pytest

from tests.support.baseline_calibration import (
    CALIBRATION_FIXTURE,
    HISTORICAL_CALIBRATION_FIXTURE,
    _redact,
    load_calibration_corpus,
    load_historical_calibration_corpus,
    prose_offenders,
)
from tracefold.news.learning.baseline import build_baseline_cases, run_baseline
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import load_stable_program_artifact

_EXPECTED_N = 4
_EXPECTED_CASE_MACRO = 0.6625
_EXPECTED_CLUSTER_MACRO = 0.716667
_EXPECTED_CLUSTER_N = 3
_HISTORICAL_N = 242
_HISTORICAL_RAW_SHA256 = "dac040e4f48de7aea94469ed295fe736c32ce047c10eabe6f53ef3dd31d82460"
_ACTIVE_RAW_SHA256 = "9ea9330f6c17ea92f96946901d6b41c16db6d8d85027b1367ef6f132f14a7cd1"
# #190 rebinds the report to the explicit canonical-number-hardening Program identity. The audit corpus
# remains byte-for-byte evidence from program_v6; every score and execution budget remains unchanged.
_EXPECTED_REPORT_SHA256 = "b234ba2fd4a3f0297e1e59b7a76b3a6ad58fb8f78de8929e73212106b68eccd3"


@pytest.fixture(scope="module")
def report() -> Any:
    corpus = load_calibration_corpus()
    cases = build_baseline_cases(corpus["episodes"], action_source="recorded")
    return run_baseline(cases, mode="recorded", artifact=load_stable_program_artifact())


def test_recorded_calibration_is_reproducible_from_the_typed_v2_corpus(report: Any) -> None:
    assert report.population == {
        "requested_n": _EXPECTED_N,
        "answered_n": _EXPECTED_N,
        "failure_n": 0,
        "failure_rate": 0.0,
    }
    assert report.scores["case_macro_answered"] == pytest.approx(_EXPECTED_CASE_MACRO)
    assert report.scores["case_macro_failure_as_zero"] == report.scores["case_macro_answered"]
    assert report.scores["cluster_macro_answered"] == pytest.approx(_EXPECTED_CLUSTER_MACRO)
    assert report.scores["cluster_n"] == _EXPECTED_CLUSTER_N
    assert report.failures["by_code"] == {}


def test_the_calibration_report_is_byte_stable_across_runs() -> None:
    corpus = load_calibration_corpus()
    artifact = load_stable_program_artifact()

    def run() -> str:
        cases = build_baseline_cases(corpus["episodes"], action_source="recorded")
        return run_baseline(cases, mode="recorded", artifact=artifact).report_sha256

    assert hashlib.sha256(CALIBRATION_FIXTURE.read_bytes()).hexdigest() == _ACTIVE_RAW_SHA256
    assert run() == run() == _EXPECTED_REPORT_SHA256


def test_v2_is_the_hard_cut_contract_not_a_compatibility_projection() -> None:
    corpus = load_calibration_corpus()
    cases = build_baseline_cases(corpus["episodes"], action_source="recorded")
    assert len(cases) == _EXPECTED_N
    for raw, case in zip(corpus["episodes"], cases, strict=True):
        assert "production_verdict" not in raw and "recorded_action" not in raw
        assert case.episode.production_judgment is not None
        assert case.episode.production_judgment.editorial.relevance is not None
        assert case.recorded_decision_result is not None
        assert set(case.recorded_decision_result) == {
            "final",
            "override_rule",
            "throttled_by",
            "rule_baseline",
            "watchlist_hits",
            "seen_similarity",
            "seen_against",
            "seen_scope",
        }
        assert case.episode.context.evidence.queue_priority in {"normal", "high"}
        assert case.episode.policy_metric["policy_version"] == TRIAGE_POLICY_VERSION
        assert set(case.episode.policy_metric["policy_values"]) == {
            "restatement_drop",
            "similarity_max",
            "stale_source_max_age_s",
            "listing_exempt_from_duplicate",
        }


def test_the_pre_160_fixture_remains_immutable_historical_evidence() -> None:
    with gzip.open(HISTORICAL_CALIBRATION_FIXTURE, "rb") as handle:
        raw = handle.read()
    historical = load_historical_calibration_corpus()
    assert hashlib.sha256(raw).hexdigest() == _HISTORICAL_RAW_SHA256
    assert historical["schema"] == "tracefold.news.baseline_calibration_corpus.v1"
    assert len(historical["episodes"]) == _HISTORICAL_N
    assert all("production_verdict" in episode and "recorded_action" in episode for episode in historical["episodes"])
    assert all("production_judgment" not in episode for episode in historical["episodes"])
    assert any("priority" in episode["context"]["evidence"] for episode in historical["episodes"])


def test_both_public_corpora_carry_no_provider_or_reviewer_prose() -> None:
    active = load_calibration_corpus()
    historical = load_historical_calibration_corpus()
    assert prose_offenders(active["episodes"]) == []
    assert prose_offenders(historical["episodes"]) == []
    assert active["redaction"]["rule"].startswith("allowlist")
    assert "recorded" in active["redaction"]["property"]


def test_the_redactor_defaults_to_redacting_a_key_nobody_listed() -> None:
    invented = _redact({"a_field_invented_tomorrow": "Nvidia announces a data centre in Ohio"})
    assert invented["a_field_invented_tomorrow"].startswith("redacted:")
    assert _redact({"direction": "bullish"}) == {"direction": "bullish"}


# The corpus was recorded under the `program_v6` root. #162 PR8-B re-issued the Program root when the
# package moved, so — like every other piece of v6 evidence — this corpus is now audit-only history. It
# is deliberately NOT re-stamped with the new sha: it was not produced by the new Program, and a fixture
# that claims otherwise is a forged record. Its job here is unaffected, because that job is to prove the
# *metric wiring* is unchanged, and `recorded` mode scores persisted verdicts without executing the
# Program at all. Re-record it against `program_v7` once that epoch has accepted reviews.
_V6_AUDIT_CORPUS_PROGRAM_SHA256 = "9334eae481e2d0cdcc3b982d25aa8def22538cadb1a57549074b56fb2a96d1ba"


def test_the_active_corpus_is_the_shipped_program_and_names_no_policy(report: Any) -> None:
    corpus = load_calibration_corpus()
    shipped = load_stable_program_artifact().program_sha256
    # The report names the Program it ran under; the corpus names the one that produced it.
    assert report.identity["program_sha256"] == shipped
    assert corpus["program_sha256"] == _V6_AUDIT_CORPUS_PROGRAM_SHA256
    assert shipped != corpus["program_sha256"], "re-point this test once a program_v7 corpus is recorded"
    assert report.identity["metric_id"] == "tracefold.news.production_action_trade_relevance_v4"
    # Recorded mode uses the persisted complete DecisionResult and never replays today's policy.
    assert report.identity["policy_sha256"] is None
    assert report.identity["policy_values"] is None


def test_timeliness_is_delivery_metadata_but_never_a_scored_prediction(report: Any) -> None:
    labelled = sum(
        1
        for episode in load_calibration_corpus()["episodes"]
        if (episode["accepted_review"].get("dimensions") or {}).get("timeliness") in {"pass", "fail"}
    )
    assert labelled == 3
    assert report.review_label_distribution["delivery"]["timeliness"]["n"] == labelled
    assert "timeliness" not in report.prediction_dimensions
    assert all("timeliness" not in dict(case.dimension_outcomes) for case in report.cases)


def test_hard_gated_cases_stay_inside_every_dimension_denominator(report: Any) -> None:
    gated = [case for case in report.cases if case.hard_gate]
    assert len(gated) == 1 and gated[0].hard_gate == "must_hold_send"
    assert gated[0].score == 0.0 and gated[0].dimension_outcomes
    assert report.hard_gates == {"by_gate": {"must_hold_send": 1}, "n": 1}

    labelled = report.review_label_distribution["event_semantics"]["direction"]["n"]
    assert report.prediction_dimensions["direction"]["n"] == labelled


def test_a_must_hold_violation_is_never_published_as_agreement(report: Any) -> None:
    sends = [case for case in report.cases if case.hard_gate == "must_hold_send"]
    assert len(sends) == 1 and sends[0].action == "push"
    confusion = report.action_confusion["must_hold"]
    assert confusion["reached_reader"] == 1
    assert confusion["agreement"] == pytest.approx(0.5)


def test_exact_trade_relevance_gold_enters_the_v4_denominator(report: Any) -> None:
    surprise = report.prediction_dimensions["trade_surprise"]
    assert surprise == {
        "retention_hit": 3,
        "gold_miss": 1,
        "n": 4,
        "not_labelled": 0,
        "hit_rate": 0.75,
    }
