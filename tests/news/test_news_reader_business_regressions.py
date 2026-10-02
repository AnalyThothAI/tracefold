"""Retained real current native distributions preserve important new details at the release cuts.

Model requests were offline, cache-free controls over exact recorded receipts. The
fixture pins policy regression and provenance; the report describes model error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracefold.news.notifications.novelty import ReaderNovelty
from tracefold.news.notifications.policy import reader_decision
from tracefold.news.notifications.reader import ReaderJudgment
from tracefold.news.updates.contracts import ClaimFields

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/news/reader_791_business_regressions.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["case_id"])
def test_real_native_business_regression(case: dict) -> None:
    judgment = ReaderJudgment.model_validate(case["judgment"])
    result = reader_decision(
        ReaderNovelty.model_validate(case["novelty"]),
        judgment,
        first_available_at_ms=case["first_available_at_ms"],
        message_intents=case["message_intents"],
        claim_fields=ClaimFields.model_validate(case["fields"]),
    )
    assert judgment.status == "available" and judgment.backend == "native"
    assert result.outcome == case["expected_outcome"]
    assert result.anchor_intent_id == case["expected_anchor"]
