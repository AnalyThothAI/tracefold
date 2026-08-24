import pytest

from tracefold.news.learning.review import EventRubricSubmission


def test_evidence_refs_are_bounded_per_entry() -> None:
    with pytest.raises(ValueError, match="at most 500 characters"):
        EventRubricSubmission(
            should_push="must_hold",
            dimensions={"factual_fidelity": "fail"},
            novelty={"judgment": "new_fact"},
            evidence_refs=["x" * 501],
        )
