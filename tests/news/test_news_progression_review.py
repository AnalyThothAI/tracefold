from __future__ import annotations

import asyncio
import json

from tracefold.news.program.dspy_adapter import ScriptedPredictorAdapter
from tracefold.news.program.progression_review import DspyProgressionVerifier


def test_progression_verifier_confirms_only_a_named_candidate_and_uses_its_stored_headline() -> None:
    adapter = ScriptedPredictorAdapter(
        [
            {
                "review": {
                    "related": True,
                    "candidate_i": 3,
                    "reason_zh": "同一工会行动从协商进入罢工投票，新增了明确比例。",
                }
            }
        ]
    )
    verifier = DspyProgressionVerifier(adapter=adapter, model_binding="progression_review.primary")

    review = asyncio.run(
        verifier.review(
            event={"leader_title": "Micron Taiwan union wins an 80% preliminary strike vote"},
            verdict={
                "headline_zh": "美光台湾工会初步罢工投票支持率达 80%",
                "why_zh": "工会行动进入有明确门槛的罢工程序。",
            },
            candidates=[
                {
                    "i": 3,
                    "headline_zh": "美光工会此前启动劳资协商",
                    "tier": "storyline",
                    "similarity": 0.31,
                    "ago_min": 90,
                    "event_type": "product",
                    "symbols": ["MU"],
                }
            ],
        )
    )

    assert review.state == "confirmed"
    assert review.candidate_i == 3
    assert review.candidate_headline_zh == "美光工会此前启动劳资协商"
    assert review.reason_zh == "同一工会行动从协商进入罢工投票，新增了明确比例。"
    request = adapter.requests[0]
    assert request.predictor == "progression_review"
    assert request.model_binding == "progression_review.primary"
    visible = json.loads(request.inputs["evidence_json"])
    assert visible["current"]["headline_zh"] == "美光台湾工会初步罢工投票支持率达 80%"
    assert visible["candidates"][0]["headline_zh"] == "美光工会此前启动劳资协商"
