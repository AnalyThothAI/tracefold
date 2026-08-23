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

from tests.support.baseline_calibration import _redact, load_calibration_corpus, prose_offenders
from tracefold.news.agents.program_baseline import build_baseline_cases, run_baseline
from tracefold.news.agents.semantic_program import load_stable_program_artifact

# Measured against the live database, 2026-08-23, `--all-cohorts --mode recorded`.
_EXPECTED_N = 242
_EXPECTED_CASE_MACRO = 0.888206
_EXPECTED_CLUSTER_MACRO = 0.89004
_EXPECTED_CLUSTER_N = 219


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
    """The fixture is published in a public repository. Its only job is to reproduce a number.

    Scanned for the *shape* of human language, not against a list of key names. The predecessor asserted
    `_redact(episode) == episode`, which is a tautology for a key-based redactor: prose under an unlisted key
    is a fixed point, so the guard stayed green while 60 reader-facing Chinese cards sat in the file under
    `title_zh`.
    """

    corpus = load_calibration_corpus()
    offenders = prose_offenders(corpus["episodes"])
    assert offenders == [], offenders[:3]
    assert corpus["redaction"]["rule"].startswith("allowlist")
    assert "recorded" in corpus["redaction"]["property"]


def test_the_redactor_defaults_to_redacting_a_key_nobody_listed() -> None:
    """The failure mode that shipped: a new prose field appears upstream and nothing here changes."""

    invented = _redact({"a_field_invented_tomorrow": "Nvidia announces a data centre in Ohio"})
    assert invented["a_field_invented_tomorrow"].startswith("redacted:")
    # ...while a structural value the metric compares survives untouched.
    assert _redact({"direction": "bullish"}) == {"direction": "bullish"}


def test_the_calibration_corpus_is_the_shipped_program_and_names_no_policy(report: Any) -> None:
    corpus = load_calibration_corpus()
    shipped = load_stable_program_artifact().program_sha256
    assert report.identity["program_sha256"] == corpus["program_sha256"] == shipped
    # `recorded` returns before policy replay, so the report names no policy rather than borrowing today's.
    assert report.identity["policy_sha256"] is None
    assert report.identity["policy_values"] is None


def test_timeliness_is_labelled_by_reviewers_but_never_scored(report: Any) -> None:
    """#150 Stage D: `timeliness` is delivery-owned. It stays visible as corpus metadata and leaves the
    EventSemantics score, so the number above is not the one #143 published."""

    # `not_applicable` is the usual answer and is not a label, so only pass/fail is counted — the same rule
    # every other dimension follows.
    labelled = sum(
        1
        for episode in load_calibration_corpus()["episodes"]
        if (episode["accepted_review"].get("dimensions") or {}).get("timeliness") in {"pass", "fail"}
    )
    assert labelled > 0, "the corpus must still contain the labels, or this test proves nothing"
    assert report.review_label_distribution["delivery"]["timeliness"]["n"] == labelled
    assert "timeliness" not in report.review_label_distribution["event_semantics"]
    assert "timeliness" not in report.review_label_distribution["reader_card"]
    assert "timeliness" not in report.prediction_dimensions
    assert all("timeliness" not in dict(case.dimension_outcomes) for case in report.cases)


def test_hard_gated_cases_stay_inside_every_denominator(report: Any) -> None:
    """A zero must enter the tables, not leave them.

    The predecessor initialised the outcome list below the gates, so a hard-gated case contributed nothing
    to `prediction_dimensions`. A candidate with *more* hard failures could therefore publish a *higher*
    per-dimension hit rate, because its zeros left the denominator — the exact inversion, in the one table
    the docs tell operators to compare between runs.
    """

    gated = [case for case in report.cases if case.hard_gate]
    assert gated, "the corpus contains hard-gated cases, or this test proves nothing"
    assert all(case.score == 0.0 for case in gated)
    assert all(case.dimension_outcomes for case in gated)
    assert report.hard_gates["n"] == len(gated)

    labelled = report.review_label_distribution["event_semantics"]["direction"]["n"]
    assert report.prediction_dimensions["direction"]["n"] == labelled, (
        "every labelled case is observable, including the ones a gate zeroed"
    )


def test_a_must_hold_violation_is_never_published_as_agreement(report: Any) -> None:
    """`action_confusion` exists to say which direction the errors run, and this is the direction that
    matters. The predecessor dropped `production_action` from the gate's early return, so an empty string
    read as `withheld` and the corpus's `must_hold` sends were filed as agreement 1.0."""

    sends = [case for case in report.cases if case.hard_gate == "must_hold_send"]
    if not sends:
        pytest.skip("no must_hold violation in the frozen corpus")
    assert all(case.action in {"push", "escalate"} for case in sends)
    confusion = report.action_confusion["must_hold"]
    assert confusion["reached_reader"] == len(sends)
    assert confusion["agreement"] < 1.0
