"""The reader replay scores recorded answers over frozen inputs; it stays offline unless --live is given.

The numbers are the 2026-09-28 replay (#742 PR-2). A change of level text, instruction, cut or model is
re-run with `scripts/eval_news_reader.py --live` and the recorded answers are replaced with its answers.
"""

from __future__ import annotations

import pytest

from scripts.eval_news_reader import (
    COVERAGE_LABELED,
    READER_REPLAY,
    answer_record,
    evaluate,
    load,
    recorded,
)
from tracefold.news.updates.reader_judgments import READER_MESSAGES_MAX


@pytest.fixture(scope="module")
def fixtures() -> tuple[list[dict], list[dict]]:
    return load(READER_REPLAY), load(COVERAGE_LABELED)


def test_fixtures_are_current_reader_inputs_with_independent_labels(fixtures: tuple[list[dict], list[dict]]) -> None:
    replay, coverage = fixtures
    assert len(replay) == 397 and len(coverage) == 300
    assert {row["label"]["verdict"] for row in replay} == {"keep", "borderline", "demote"}
    assert sum(row["label"]["covered_by"] != "none" for row in coverage) == 150
    assert all(len(row["reader"].messages) <= READER_MESSAGES_MAX for row in (*replay, *coverage))
    # The day's population is 1,565 editorially judged claims; the stratum weights restore it.
    assert round(sum(row["weight"] for row in replay)) == 1565


def test_recorded_native_answers_meet_the_importance_bar_and_report_coverage(
    fixtures: tuple[list[dict], list[dict]],
) -> None:
    replay, coverage = fixtures
    report = evaluate(replay, coverage, recorded(replay, "native"), recorded(coverage, "native"), "native")

    assert report["answered"] == {"reader_replay": 397, "coverage": 300}
    importance = report["importance"]
    assert importance["auc_keep_borderline_vs_demote"] == 0.818
    assert importance["auc_keep_vs_demote"] == 0.893
    at_push = next(row for row in report["decision_table"] if row["cut"] == report["cuts"]["push"])
    assert at_push["precision_keep_borderline"] >= 0.40
    at_threshold = next(row for row in report["coverage"] if row["none_below"] == report["cuts"]["covered_none_below"])
    assert (at_threshold["covered_recall"], at_threshold["false_covered_rate"]) == (0.913, 0.013)


def test_a_recorded_answer_round_trips(fixtures: tuple[list[dict], list[dict]]) -> None:
    replay, coverage = fixtures
    for rows in (replay, coverage):
        answers = recorded(rows, "native")
        row = rows[0]
        assert answer_record(answers[row["case_id"]]) == row["answers"]["native"]
