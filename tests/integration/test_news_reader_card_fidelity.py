"""Review acceptance, PostgreSQL freeze, and the shared metric use the same bounded evidence.

The scripted provider tests accounting and scoring, not linguistic quality. Real-model receipts belong
to the issue's frozen comparison and retain their separate reviewer provenance.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.integration import test_news_candidate_evaluator as evaluator_fixtures
from tests.integration.test_news_candidate_evaluator import (
    NOW,
    ReviewDesk,
    _arm,
    _datasets,
    _open_event,
)
from tests.integration.test_news_review_desk import PRINCIPAL, _rubric
from tests.news.test_news_program_judge import _ScriptedJudgeLM
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.learning.contracts import ClosedWindow
from tracefold.news.learning.dataset import DatasetSpec
from tracefold.news.learning.judge import CardEquivalenceJudge
from tracefold.news.learning.metric import CandidatePrediction, accepted_review_metric, build_compile_example
from tracefold.news.learning.objective import DevelopmentEpisode
from tracefold.news.review.desk import DeskQuery, EventRubricSubmission, TaskRef

pytestmark = pytest.mark.integration
conn = evaluator_fixtures.conn


@pytest.mark.parametrize(
    ("label", "supported", "unavailable", "expected"),
    [
        ("fail", True, False, "support_hit"),
        ("pass", True, False, "support_hit"),
        ("fail", False, False, "support_miss"),
        ("fail", False, True, "support_unavailable"),
    ],
)
def test_review_freeze_support_repair_and_preservation(conn, label, supported, unavailable, expected):
    event_id = _open_event(conn)
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    original = _rubric(why=label, first_bad_owner="triage_prompt" if label == "fail" else None)
    payload = original.model_dump(mode="json")
    payload["dimensions"].update(factual_fidelity=label, why_value="fail")
    payload["evidence_refs"] = ["source:sentence:1", "output:why"]
    payload["expected_correction"] = "Preserve the reported price change; do not invent its consequences."
    submission = EventRubricSubmission.model_validate(payload)
    with repositories_for_connection(conn).transaction():
        receipt = desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            submission,
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )

    datasets = _datasets(conn, _arm())
    frozen = asyncio.run(
        datasets.freeze_dataset(
            DatasetSpec(role="development", window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW))
        )
    )
    exported = datasets.development_compile_export(frozen.artifact_sha)
    episode = DevelopmentEpisode.model_validate(exported.episodes[0])
    assert episode.accepted_review["review_id"] == receipt["receipt"]["review_id"]
    assert episode.accepted_review["dimensions"]["why_support"] == label
    example = build_compile_example(episode)
    assert "Micron says DRAM contract prices rose again in August" in example.card_evidence_json
    assert example.accepted_review["expected"].get("why_zh") is None
    production = example.production_judgment
    assert production is not None
    candidate = CandidatePrediction(
        verdict={**production["verdict"], "why_zh": "美光称8月DRAM合约价再度上涨，持续性尚未披露。"},
        editorial=production["editorial"],
    )
    lm = _ScriptedJudgeLM(facts_supported=supported, fail=unavailable)
    judge = CardEquivalenceJudge(lm)
    outcome = accepted_review_metric(example, candidate, judge=judge)
    assert ("why_support", expected) in outcome.dimension_outcomes
    assert ("why_value", "not_scored_no_gold") in outcome.dimension_outcomes
    assert outcome.component_denominators["reader_card"] == 3
    assert outcome.hard_gate == (
        "metric_judge_unavailable" if unavailable else "factual_contradiction" if not supported else ""
    )
    # A support failure is shared across the two dimensions rather than automatically retried.
    assert lm.calls == (2 if label == "pass" else 1)
    requests = " ".join(str(request.messages) for request in lm.delegate.requests)
    assert "Micron says DRAM contract prices rose again in August" in requests
    assert datasets.development_compile_export(frozen.artifact_sha).episodes == exported.episodes
