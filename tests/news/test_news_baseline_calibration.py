"""#150: the recorded calibration is a property of the code plus a checked-in corpus, not of a live database.

`recorded` mode exists to prove one thing: that metric wiring still produces the same number over the same
history. That proof was impossible while the corpus was read from the operator's database — #143 published
`0.896373 / n=162` and the same command answered `0.888426 / n=243` a day later, because #148 added 81
reviews. Nothing was wrong; the check simply could not tell "the metric changed" from "the corpus grew".

The corpus is frozen in `tests/fixtures/news_baseline_calibration_v1.json.gz` (see
`tests.support.baseline_calibration` for the redaction and how to regenerate it). These numbers were produced
by the same code against the live database on 2026-08-23 and must move only when the metric moves.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.baseline_calibration import _TEXT_KEYS, _redact, load_calibration_corpus
from tracefold.news.agents.program_baseline import build_baseline_cases, run_baseline
from tracefold.news.agents.semantic_program import load_stable_program_artifact

# Measured against the live database, 2026-08-23, `--all-cohorts --mode recorded`.
_EXPECTED_N = 243
_EXPECTED_CASE_MACRO = 0.888426
_EXPECTED_CLUSTER_MACRO = 0.890275
_EXPECTED_CLUSTER_N = 220


@pytest.fixture(scope="module")
def report() -> Any:
    corpus = load_calibration_corpus()
    cases = build_baseline_cases(corpus["episodes"], action_source="recorded")
    return run_baseline(cases, mode="recorded", artifact=load_stable_program_artifact())


def test_recorded_calibration_is_reproducible_from_the_frozen_corpus(report: Any) -> None:
    assert report.population == {
        "requested_n": _EXPECTED_N,
        "answered_n": _EXPECTED_N,
        "failure_n": 0,
        "failure_rate": 0.0,
    }
    assert report.scores["case_macro_answered"] == pytest.approx(_EXPECTED_CASE_MACRO)
    # `recorded` spends no provider call, so there is nothing that can fail to answer and the conditional
    # mean and the lower bound are the same number. Any drift between them here is a wiring bug.
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

    assert run() == run()


def test_the_frozen_corpus_carries_no_provider_or_reviewer_prose() -> None:
    """The fixture is published in a public repository. Its only job is to reproduce a number."""

    corpus = load_calibration_corpus()
    for episode in corpus["episodes"]:
        assert _redact(episode) == episode, "regenerate the fixture: it contains unredacted text"
    assert sorted(corpus["redaction"]["keys"]) == sorted(_TEXT_KEYS)
    assert "recorded" in corpus["redaction"]["property"]


def test_the_calibration_corpus_is_one_policy_and_the_shipped_program(report: Any) -> None:
    corpus = load_calibration_corpus()
    shipped = load_stable_program_artifact().program_sha256
    assert report.identity["program_sha256"] == corpus["program_sha256"] == shipped
    assert report.identity["policy_sha256"] == corpus["policy_sha256"]


def test_timeliness_is_labelled_by_reviewers_but_never_scored(report: Any) -> None:
    """#150 Stage D: `timeliness` is delivery-owned. It stays visible as corpus metadata and leaves the
    EventSemantics score, so the number above is not the one #143 published."""

    labelled = sum(
        1
        for episode in load_calibration_corpus()["episodes"]
        if "timeliness" in (episode["accepted_review"].get("dimensions") or {})
    )
    assert labelled > 0, "the corpus must still contain the labels, or this test proves nothing"
    assert "timeliness" not in report.review_label_distribution
    assert "timeliness" not in report.prediction_dimensions
    assert all("timeliness" not in dict(case.dimension_outcomes) for case in report.cases)
