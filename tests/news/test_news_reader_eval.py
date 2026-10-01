"""The reader replay scores recorded answers over archived inputs through the current decision and cuts.

The inputs are the archived 2026-09-28 `news_reader_input_v1` baseline (#742 PR-2); they are never sent to the
current judge. The recorded answers are those of the #742 PR-5 rubric, asked on these inputs while v1 was the
current contract, and the labels follow PR-5's product definition (rows it changed carry `label.relabel`). #750
did not change `reader_decision`. #759 preserves the cuts and selectively scores an actual
state change at the push cut when an information link has no core-fact anchor.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.eval_news_reader import answer_record, evaluate, load_all, recorded


@pytest.fixture(scope="module")
def fixtures() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[list[dict[str, Any]]]]:
    return load_all()


def test_fixtures_are_archived_reader_inputs_with_independent_labels(fixtures: Any) -> None:
    replay, anchors, coverage, (nvidia, spacex) = fixtures
    assert len(replay) == 397 and len(anchors) == 300 and len(coverage) == 300
    assert {row["label"]["verdict"] for row in replay} == {"keep", "borderline", "demote"}
    # PR-5 relabelled, blind to scores, the rows the owner's product definition turns: 17 to keep, 22 to
    # borderline (small-project launches and partnerships, listings, large-company launches, ETFs, milestones).
    relabelled = [row["label"] for row in replay if "relabel" in row["label"]]
    assert sorted((label["relabel"]["from"], label["verdict"]) for label in relabelled) == sorted(
        [("borderline", "keep")] * 10 + [("demote", "keep")] * 7 + [("demote", "borderline")] * 22
    )
    assert sum(row["label"]["anchor"] != "none" for row in anchors) == 93
    assert {row["reader_novelty"].novelty for row in anchors} == {"unlinked"}
    # 150 claims sampled where the native answer leans to an anchor, 150 from the rest of the day's 1,176.
    assert round(sum(row["weight"] for row in anchors)) == 1176
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
    replay, anchors, coverage, clusters = fixtures
    everything = [*replay, *anchors, *coverage, *(row for rows in clusters for row in rows)]
    report = evaluate(replay, anchors, coverage, clusters, recorded(everything, "native"), "native")

    assert report["answered"] == {"reader_replay": 397, "anchor": 300, "coverage": 300}
    assert report["importance"]["auc_keep_borderline_vs_demote"] >= 0.80
    at_push = next(row for row in report["decision_table"]["push"] if row["cut"] == report["cuts"]["push"])
    assert at_push["precision_keep_borderline"] >= 0.40
    # #742 PR-5: the owner's 300-500 messages and 50-60 key a day (PR-4 at 2.5 / 2.8: 120 claims a day).
    pinned = (
        at_push["claims_per_day"],
        at_push["key_per_day"],
        at_push["precision_keep_borderline"],
        at_push["keep_recall"],
    )
    assert pinned == (331, 39, 0.536, 0.725)
    cut = report["cuts"]["anchor_none_below"]
    fully_said = next(row for row in report["anchor"]["fully_said"] if row["none_below"] == cut)
    assert (fully_said["anchor_recall"], fully_said["false_anchor_rate"]) == (0.98, 0.0)
    # Against the core-fact labels the Issue's 97 % / 1 % is not reached at any cut; a missed anchor renders
    # the claim in full at the push cut, a false one holds it to the key cut as an increment of another
    # message, so the cut keeps false anchors low (weighted to the day).
    core_fact = next(row for row in report["anchor"]["core_fact"] if row["none_below"] == cut)
    assert (core_fact["anchor_recall"], core_fact["false_anchor_rate"]) == (0.545, 0.014)
    nvidia, spacex = report["clusters"]
    # The first $150B announcement and the separate "through FY2028" claim; the five repeats stay in the feed.
    assert [case.split(":")[1] for case in nvidia["pushed"]] == ["0a929efc", "65153734"]
    assert nvidia["novelty"] == {"unlinked": 2, "known": 4, "increment": 3}
    # The debut liftoff and "first revenue-generating flight"; every other development stays in the feed.
    assert len(spacex["pushed"]) <= 3


def test_recorded_generative_fallback_answers_meet_the_bar_with_their_own_cuts(fixtures: Any) -> None:
    replay, anchors, coverage, clusters = fixtures
    everything = [*replay, *anchors, *coverage, *(row for rows in clusters for row in rows)]
    report = evaluate(replay, anchors, coverage, clusters, recorded(everything, "generated"), "generated")

    assert report["answered"] == {"reader_replay": 397, "anchor": 300, "coverage": 300}
    assert report["importance"]["auc_keep_borderline_vs_demote"] >= 0.80
    at_push = next(row for row in report["decision_table"]["push"] if row["cut"] == report["cuts"]["push"])
    assert at_push["precision_keep_borderline"] >= 0.40
    # The fallback scores developments higher, so its push cut also keeps the Starship sequence to three
    # distinct developments (liftoff, in orbit, satellites deployed); today's production claims put it at
    # about 310 messages a day against the native route's 370 (#742 PR-5).
    pinned = (
        at_push["claims_per_day"],
        at_push["key_per_day"],
        at_push["precision_keep_borderline"],
        at_push["keep_recall"],
    )
    # #759 restores L098 (withdrawals resumed), without promoting parameter details.
    assert pinned == (250, 62, 0.561, 0.579)
    assert [len(cluster["pushed"]) for cluster in report["clusters"]] == [2, 3]


def test_a_recorded_answer_round_trips(fixtures: Any) -> None:
    replay, anchors, coverage, _ = fixtures
    for rows in (replay, anchors, coverage):
        answers = recorded(rows, "native")
        row = rows[0]
        assert answer_record(answers[row["case_id"]]) == row["answers"]["native"]


def test_recorded_recovery_is_an_action_while_partnership_terms_remain_details(fixtures: Any) -> None:
    from scripts.eval_news_reader import decision

    replay, *_ = fixtures
    rows = {row["case_id"]: row for row in replay}
    recovery, terms = rows["L098"], rows["L240"]
    assert recovery["label"]["verdict"] == "keep"
    assert terms["label"]["verdict"] == "demote"
    assert recovery["reader_novelty"].novelty == terms["reader_novelty"].novelty == "increment"
    answers = recorded((recovery, terms), "generated")
    # These are archived production answers; no threshold or probability has been altered.
    assert answers["L098"].importance is not None and answers["L098"].importance.value == 2.8
    assert answers["L240"].importance is not None and answers["L240"].importance.value == 2.8
    assert decision(recovery, answers["L098"]).outcome == "push"
    assert decision(terms, answers["L240"]).outcome == "feed"
