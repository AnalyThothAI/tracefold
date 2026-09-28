"""Current decision feedback accepts only its bounded fields."""

import pytest
from pydantic import ValidationError

from tracefold.news.review.desk import DecisionFeedbackSubmission


def test_decision_feedback_note_is_bounded_without_legacy_rubric() -> None:
    assert DecisionFeedbackSubmission(should_push="should_hold", note="").note == ""
    with pytest.raises(ValidationError) as error:
        DecisionFeedbackSubmission(should_push="should_hold", note="x" * 2001)
    assert error.value.errors()[0]["loc"] == ("note",)
    with pytest.raises(ValidationError) as error:
        DecisionFeedbackSubmission.model_validate({"should_push": "should_hold", "dimensions": {}})
    assert error.value.errors()[0]["loc"] == ("dimensions",)
