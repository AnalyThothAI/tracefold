import pytest

from tracefold.news.review.desk import EventRubricSubmission


def test_evidence_refs_are_bounded_per_entry() -> None:
    with pytest.raises(ValueError, match="at most 500 characters"):
        EventRubricSubmission(
            should_push="must_hold",
            dimensions={
                "factual_fidelity": "fail",
                "taxonomy_subject_codes": "pass",
                "taxonomy_event_family": "pass",
                "taxonomy_change_state": "pass",
                "taxonomy_source_authority": "pass",
                "taxonomy_assertion_status": "pass",
            },
            novelty={"judgment": "new_fact"},
            taxonomy={
                "subject_codes": [],
                "event_family": "other",
                "change_state": "unknown",
                "assertion_status": "unknown",
                "source_authority": "unknown",
            },
            evidence_refs=["x" * 501],
        )
