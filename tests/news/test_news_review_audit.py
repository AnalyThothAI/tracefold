"""The daily audit report: what a person is asked to read, and the two ratios over one batch (#675 §4)."""

from __future__ import annotations

import json
from argparse import Namespace
from contextlib import contextmanager
from typing import Any

import pytest

from scripts.news_freeze_audit_2026_09_22 import CONFIRMED_MISSES, should_push_for, submission_for
from tracefold.app.cli.commands import news_review
from tracefold.app.cli.parser import build_parser
from tracefold.news.review.audit import (
    agreement_sampled,
    audit_report,
    decision_task_ids,
    decisions_from_queue,
    outcome_of,
    render_table,
)
from tracefold.news.review.desk import EventRubricSubmission
from tracefold.news.review.drafter import DRAFT_SCHEMA


def _entry(task_id: str, should_push: str, *, error: str | None = None) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_version": "f" * 64,
        "event_id": task_id.split(".")[1] if "." in task_id else task_id,
        "headline_zh": f"{task_id} 的卡",
        "error": error,
        "draft": {"should_push": should_push, "confidence": 0.7},
    }


def _batch(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"schema_id": DRAFT_SCHEMA, "batch_sha256": "a" * 64, "drafts": list(entries)}


def _decision(final_decision: str, stratum: str = "") -> dict[str, Any]:
    return {"final_decision": final_decision, "selection": {"stratum": stratum}, "headline": ""}


def test_a_disagreement_is_a_push_on_a_withheld_card_or_a_hold_on_a_delivered_one() -> None:
    """The two directions the loop exists to find: an unwanted interrupt and a miss."""

    batch = _batch(
        _entry("evt.a.1", "should_hold"),  # delivered, draft would hold -> unwanted
        _entry("evt.b.1", "must_push"),  # dropped, draft would push -> missed
        _entry("evt.c.1", "must_push"),  # delivered and drafted push -> agreement
        _entry("evt.d.1", "must_hold"),  # dropped and drafted hold -> agreement
        _entry("evt.e.1", "uncertain"),  # neither: not a disagreement in either direction
        _entry("evt.f.1", "should_push"),  # throttled counts as withheld
    )
    decisions = {
        "evt.a.1": _decision("push"),
        "evt.b.1": _decision("drop"),
        "evt.c.1": _decision("escalate"),
        "evt.d.1": _decision("drop"),
        "evt.e.1": _decision("push"),
        "evt.f.1": _decision("throttled"),
    }

    report = audit_report(batch, decisions, probability=0.0)

    assert [(row["task_id"], row["disagreement"]) for row in report["disagreements"]] == [
        ("evt.a.1", "unwanted"),
        ("evt.b.1", "missed"),
        ("evt.f.1", "missed"),
    ]
    assert report["agreement_sample"] == []
    assert report["only"] == "evt.a.1,evt.b.1,evt.f.1"


def test_uncertain_is_in_both_denominators_and_in_neither_numerator() -> None:
    """A draft that will not answer still counted a card; scoring it as a keep would flatter the ratio."""

    batch = _batch(
        _entry("evt.a.1", "must_push"),
        _entry("evt.b.1", "uncertain"),
        _entry("evt.c.1", "should_hold"),
        _entry("evt.d.1", "should_push"),
        _entry("evt.e.1", "uncertain"),
    )
    decisions = {
        "evt.a.1": _decision("push"),
        "evt.b.1": _decision("push"),
        "evt.c.1": _decision("push"),
        "evt.d.1": _decision("drop"),
        "evt.e.1": _decision("throttled"),
    }

    report = audit_report(batch, decisions, probability=0.0)

    assert report["keep_ratio_sent"] == {"ratio": round(1 / 3, 4), "numerator": 1, "denominator": 3}
    assert report["missed_ratio_dropped"] == {"ratio": 0.5, "numerator": 1, "denominator": 2}


def test_an_empty_side_reports_a_null_ratio_rather_than_zero_or_one() -> None:
    report = audit_report(_batch(_entry("evt.a.1", "must_push")), {"evt.a.1": _decision("push")}, probability=0.0)
    assert report["keep_ratio_sent"] == {"ratio": 1.0, "numerator": 1, "denominator": 1}
    assert report["missed_ratio_dropped"] == {"ratio": None, "numerator": 0, "denominator": 0}


def test_a_task_the_desk_cannot_resolve_is_skipped_rather_than_assumed() -> None:
    """A guessed decision would put a fabricated denominator under a product metric."""

    batch = _batch(
        _entry("evt.a.1", "must_push"),
        _entry("evt.b.1", "must_push"),
        _entry("evt.c.1", "must_push", error="taxonomy_drafting_failed: provider unavailable"),
    )

    report = audit_report(batch, {"evt.a.1": _decision("push")}, probability=0.0)

    assert report["counts"]["skipped"] == {"drafting_failed": 1, "decision_unavailable": 1}
    assert report["counts"]["delivered"] == 1 and report["tasks"] == 3


def test_a_degraded_or_unjudged_task_enters_neither_ratio() -> None:
    report = audit_report(_batch(_entry("evt.a.1", "must_push")), {"evt.a.1": _decision("")}, probability=0.0)
    assert outcome_of(None) == "unclassified"
    assert report["counts"]["unclassified"] == 1
    assert report["keep_ratio_sent"]["denominator"] == 0
    assert report["missed_ratio_dropped"]["denominator"] == 0
    assert [row["task_id"] for row in report["unclassified"]] == ["evt.a.1"]


def test_the_agreement_sample_is_the_same_reading_list_on_every_rerun() -> None:
    """Seeded by task_id, so re-running the report after accepting half of it does not reshuffle it."""

    tasks = [f"evt.{index:064d}.1" for index in range(400)]
    selected = [task_id for task_id in tasks if agreement_sampled(task_id)]

    assert selected == [task_id for task_id in tasks if agreement_sampled(task_id)]
    assert 0.03 <= len(selected) / len(tasks) <= 0.20
    # The bounds of the probability argument are exact, not approximate.
    assert all(agreement_sampled(task_id, probability=1.0) for task_id in tasks[:20])
    assert not any(agreement_sampled(task_id, probability=0.0) for task_id in tasks[:20])


def test_the_sample_covers_agreements_only_and_lands_in_the_only_string() -> None:
    tasks = [f"evt.{index:064d}.1" for index in range(400)]
    batch = _batch(*(_entry(task_id, "must_push") for task_id in tasks))
    decisions = {task_id: _decision("push") for task_id in tasks}

    report = audit_report(batch, decisions)

    sampled = [row["task_id"] for row in report["agreement_sample"]]
    assert report["disagreements"] == []
    assert sampled == [task_id for task_id in tasks if agreement_sampled(task_id)]
    assert report["only"].split(",") == sampled
    assert report["counts"]["agreement_sampled"] == len(sampled)


def test_the_table_states_both_ratios_with_their_denominators() -> None:
    batch = _batch(_entry("evt.a.1", "should_hold"), _entry("evt.b.1", "must_push"))
    report = audit_report(batch, {"evt.a.1": _decision("push"), "evt.b.1": _decision("drop")}, probability=0.0)

    table = render_table(report)

    assert "keep_ratio_sent      0.0% (0/1)" in table
    assert "missed_ratio_dropped 100.0% (1/1)" in table
    assert "evt.a.1" in table and "evt.b.1" in table


def test_queue_rows_are_indexed_by_task_and_batch_order_is_preserved() -> None:
    batch = _batch(_entry("evt.b.1", "must_push"), _entry("evt.a.1", "must_push"), _entry("evt.b.1", "must_push"))
    assert decision_task_ids(batch) == ("evt.b.1", "evt.a.1")

    rows = [{"task_id": "evt.a.1", "final_decision": "drop", "selection": {"stratum": "model_drop"}}]
    assert decisions_from_queue(rows)["evt.a.1"]["selection"]["stratum"] == "model_drop"


def test_the_cli_reads_decisions_through_the_desk_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """`audit-report` opens the same queue view a reviewer opens, and never reaches a review writer."""

    batch = _batch(_entry("evt.a.1", "should_hold"), _entry("evt.b.1", "must_push"))
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch), encoding="utf-8")
    opened: list[str] = []

    class _Desk:
        def __init__(self, _conn: Any) -> None:
            pass

        def open(self, query: Any, *, principal: Any) -> dict[str, Any]:
            del principal
            opened.append(query.task)
            return {"tasks": [{"task_id": query.task, "final_decision": "push", "selection": {}}]}

        def submit(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("audit-report must never write a review")

    @contextmanager
    def _connection(_settings: Any) -> Any:
        yield object()

    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", _connection)
    monkeypatch.setattr("tracefold.news.review.desk.ReviewDesk", _Desk)

    code, payload = news_review._handle_review_audit_report(Namespace(file=str(path), json=False), object(), object())

    assert code == 0 and opened == ["evt.a.1", "evt.b.1"]
    assert payload["data"]["counts"]["disagreement"] == 1
    assert "keep_ratio_sent" in payload["data"]["table"]

    code, machine = news_review._handle_review_audit_report(Namespace(file=str(path), json=True), object(), object())
    assert code == 0 and "table" not in machine["data"]
    assert machine["data"]["only"] == payload["data"]["only"]


def test_the_parser_exposes_audit_report_beside_accept_drafts() -> None:
    args = build_parser().parse_args(["news", "review", "audit-report", "--file", "batch.json"])
    assert args.review_command == "audit-report" and args.file == "batch.json" and args.json is False
    assert (
        build_parser()
        .parse_args(
            ["news", "learning", "draft-reviews", "--rubric-model", "m", "--taxonomy-models", "a,b", "--out", "o"]
        )
        .concurrency
        == 4
    )


def test_the_2026_09_22_freeze_maps_each_audit_verdict_to_one_push_label() -> None:
    """The mapping the issue fixed, including the six confirmed misses and the marketing/schedule split."""

    assert should_push_for({"verdict": "keep", "category": "real_fact"}) == "should_push"
    assert should_push_for({"verdict": "demote", "category": "price_report"}) == "should_hold"
    assert should_push_for({"verdict": "ok_drop", "category": "opinion"}) == "should_hold"
    assert should_push_for({"verdict": "ok_drop", "category": "marketing"}) == "must_hold"
    assert should_push_for({"verdict": "ok_drop", "category": "schedule"}) == "must_hold"
    assert should_push_for({"verdict": "borderline", "category": "listing"}) == "uncertain"

    confirmed = sorted(CONFIRMED_MISSES)[0]
    assert len(CONFIRMED_MISSES) == 6
    assert should_push_for({"verdict": "missed_valuable", "event_id": confirmed}) == "must_push"
    assert should_push_for({"verdict": "missed_valuable", "event_id": "0" * 64}) == "uncertain"

    with pytest.raises(ValueError, match="news_freeze_audit_unknown_verdict"):
        should_push_for({"verdict": "not_a_verdict"})


def test_the_freeze_submission_is_the_minimal_honest_shape_the_rubric_accepts() -> None:
    """`should_push` plus `timeliness: not_applicable`: the DB refuses empty dimensions, and these
    reviewers never read the frozen evidence, so no component dimension may be claimed."""

    payload = submission_for({"verdict": "keep", "category": "real_fact", "reason": "读者要这条", "event_id": "x"})

    submission = EventRubricSubmission.model_validate(payload)
    assert submission.should_push == "should_push"
    assert submission.dimensions == {"timeliness": "not_applicable"}
    assert submission.note == "category=real_fact; 读者要这条"
    assert submission.taxonomy is None and submission.novelty is None and submission.expected is None
    assert submission.evidence_refs == []
