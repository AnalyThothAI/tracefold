"""The reader replay scores recorded answers over frozen inputs; it stays offline unless --live is given.

The numbers are the 2026-09-28 replay (#742 PR-2). A change of level text, instruction, cut, novelty rule or
model is re-run with `scripts/eval_news_reader.py --live` and the recorded answers are replaced with its answers.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.eval_news_reader import answer_record, evaluate, load_all, recorded


@pytest.fixture(scope="module")
def fixtures() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[list[dict[str, Any]]]]:
    return load_all()


def test_fixtures_are_current_reader_inputs_with_independent_labels(fixtures: Any) -> None:
    replay, coverage, (nvidia, spacex) = fixtures
    assert len(replay) == 397 and len(coverage) == 300
    assert {row["label"]["verdict"] for row in replay} == {"keep", "borderline", "demote"}
    assert sum(row["label"]["covered_by"] != "none" for row in coverage) == 150
    # The day's population is 1,565 editorially judged claims; the stratum weights restore it.
    assert round(sum(row["weight"] for row in replay)) == 1565
    assert {row["event_id"][:8] for row in nvidia} == {
        "65153734",
        "0a929efc",
        "efd12876",
        "c17b9d0c",
        "48f26352",
        "f4501316",
        "f9ba0a10",
    }
    assert len(spacex) == 17


def test_recorded_native_answers_meet_the_bar(fixtures: Any) -> None:
    replay, coverage, clusters = fixtures
    everything = [*replay, *coverage, *(row for rows in clusters for row in rows)]
    report = evaluate(replay, coverage, clusters, recorded(everything, "native"), "native")

    assert report["answered"] == {"reader_replay": 397, "coverage": 300}
    assert report["importance"]["auc_keep_borderline_vs_demote"] >= 0.80
    at_push = next(row for row in report["decision_table"] if row["cut"] == report["cuts"]["push"])
    assert at_push["precision_keep_borderline"] >= 0.40
    anchor = next(row for row in report["anchor"] if row["none_below"] == report["cuts"]["anchor_none_below"])
    assert (anchor["anchor_recall"], anchor["false_anchor_rate"]) == (0.993, 0.011)
    nvidia, spacex = report["clusters"]
    # The first $150B announcement and the separate "through FY2028" claim; the five repeats stay in the feed.
    assert [case.split(":")[1] for case in nvidia["pushed"]] == ["0a929efc", "65153734"]
    assert nvidia["novelty"] == {"unlinked": 2, "known": 4, "increment": 3}
    assert len(spacex["pushed"]) <= 2


def test_recorded_generative_fallback_answers_meet_the_bar_with_their_own_cuts(fixtures: Any) -> None:
    replay, coverage, clusters = fixtures
    report = evaluate(replay, coverage, clusters, recorded(replay, "generated"), "generated")

    assert report["answered"]["reader_replay"] == 397
    assert report["importance"]["auc_keep_borderline_vs_demote"] >= 0.80
    at_push = next(row for row in report["decision_table"] if row["cut"] == report["cuts"]["push"])
    assert at_push["precision_keep_borderline"] >= 0.40


def test_a_recorded_answer_round_trips(fixtures: Any) -> None:
    replay, coverage, _ = fixtures
    for rows in (replay, coverage):
        answers = recorded(rows, "native")
        row = rows[0]
        assert answer_record(answers[row["case_id"]]) == row["answers"]["native"]
