"""The 2026-09-22 independent-audit freeze script maps each audit label to one ReviewDesk rubric (#675 §4)."""

from __future__ import annotations

import pytest

from scripts.news_freeze_audit_2026_09_22 import CONFIRMED_MISSES, should_push_for, submission_for
from tracefold.news.review.desk import EventRubricSubmission


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
    assert submission.novelty is None and submission.expected is None
    assert submission.evidence_refs == []
