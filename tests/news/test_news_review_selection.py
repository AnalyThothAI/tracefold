from __future__ import annotations

from typing import Any

import pytest

from tracefold.news.review.desk import _selection


def _row(
    *,
    scope: str = "macro",
    final_decision: str = "drop",
    queue_priority: str = "normal",
    **observed: Any,
) -> dict[str, Any]:
    """One review candidate, carrying only what the sampler is allowed to read."""

    row: dict[str, Any] = {
        "verdict": {"scope": scope, "fact_kind": "state_change"},
        "queue_priority": queue_priority,
        "final_decision": final_decision,
    }
    row.update(observed)
    return row


@pytest.mark.parametrize("scope", ["macro", "sector", "single_name"])
def test_model_drop_is_sampled_at_one_rate_whatever_the_scope(scope: str) -> None:
    """Sampler v4 (#675 §1) has no macro branch. Six relevance-driven strata used to claim a `macro`
    drop before `model_drop` could see it, so the recall sample the daily audit reads was built from
    the non-macro rows alone; every scope now enters it at the same 10%."""

    selection = _selection(_row(scope=scope))

    assert selection == {
        "stratum": "model_drop",
        "stratum_zh": "模型判断不推",
        "reason": "semantic_or_policy_hold",
        "reason_zh": "语义或策略判断不送达",
        "sampling_probability": 0.10,
        "selection_version": "news_review_sampler_v4",
    }


@pytest.mark.parametrize("scope", ["macro", "sector"])
def test_delivered_row_is_a_quality_sample_whatever_the_scope(scope: str) -> None:
    """The other half of the same deletion: a delivered card is judged on having been delivered."""

    selection = _selection(_row(scope=scope, final_decision="push", delivery_state="sent"))

    assert selection["stratum"] == "delivered"
    assert selection["reason"] == "sent_quality_sample"
    assert selection["sampling_probability"] == 0.25


def test_queue_high_model_event_carries_no_sampling_authority() -> None:
    """`queue_priority` is how fast a card was sent, not what it was. The sampler never reads it."""

    assert _selection(_row(queue_priority="high")) == _selection(_row(queue_priority="normal"))


def test_escalate_is_always_reviewed() -> None:
    selection = _selection(_row(scope="macro", final_decision="escalate"))

    assert selection["stratum"] == "critical"
    assert selection["reason"] == "semantic_escalation"
    assert selection["sampling_probability"] == 1.0


@pytest.mark.parametrize(
    ("observed", "stratum", "reason"),
    [
        ({"delivery_error_code": "ambiguous_after_crash"}, "delivery_ambiguous", "delivery_truth_unknown"),
        ({"delivery_state": "terminal"}, "delivery_failed", "delivery_terminal_failure"),
    ],
)
def test_delivery_truth_opens_the_cascade(observed: dict[str, Any], stratum: str, reason: str) -> None:
    """A card whose delivery result is unknown or terminally failed is reviewed before any judgment
    about it is, including an `escalate` -- what the desk cannot say happened, it has to look at."""

    selection = _selection(_row(final_decision="escalate", **observed))

    assert selection["stratum"] == stratum
    assert selection["reason"] == reason
    assert selection["sampling_probability"] == 1.0
