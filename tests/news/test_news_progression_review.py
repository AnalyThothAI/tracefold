from __future__ import annotations

import asyncio

import dspy  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from tracefold.news.program.lm import AuditedConfiguredLM, RuntimeModelIdentity, ScriptedLM
from tracefold.news.program.progression_review import (
    PROGRESSION_REVIEW_MAX_CALLS,
    PROGRESSION_REVIEW_MAX_TOKENS,
    PROGRESSION_REVIEW_SHA256,
    ProgressionReviewProgram,
)


def _program(lm: ScriptedLM) -> ProgressionReviewProgram:
    return ProgressionReviewProgram(
        AuditedConfiguredLM(
            lm,
            structured_output=("json_schema" if lm.supports_response_schema else "json_object"),
            runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=lm.model),
            predictor="progression_review",
            route="primary",
            model_binding="progression_review.primary",
        )
    )


def test_progression_verifier_confirms_only_a_named_candidate_and_uses_its_stored_headline() -> None:
    lm = ScriptedLM(
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
    verifier = _program(lm)

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
    assert review.verifier_id.startswith("tracefold.news.progression_review_v3:")
    assert len(lm.requests) == 1
    request = lm.requests[0]
    rendered = "\n".join(part.text for message in request.messages for part in message.parts if hasattr(part, "text"))
    assert "美光台湾工会初步罢工投票支持率达 80%" in rendered
    assert "美光工会此前启动劳资协商" in rendered
    assert '"ago_min":null' in rendered
    assert request.config.max_tokens == PROGRESSION_REVIEW_MAX_TOKENS


def test_progression_verifier_compacts_a_long_multiline_reason_before_it_reaches_delivery() -> None:
    lm = ScriptedLM(
        [
            {
                "review": {
                    "related": False,
                    "candidate_i": -1,
                    "reason_zh": (
                        "两条新闻只是共享宽泛的行业标签，\n并不涉及同一个主体、同一个事件链或前后状态变化，"
                        "因此不能把当前新闻定义成候选新闻的后续进展，也不应重复展示候选标题。"
                    ),
                }
            }
        ]
    )
    verifier = _program(lm)

    review = asyncio.run(
        verifier.review(
            event={"leader_title": "Current"},
            verdict={"headline_zh": "当前新闻", "why_zh": "当前影响"},
            candidates=[
                {
                    "i": 0,
                    "headline_zh": "候选新闻",
                    "tier": "recency",
                    "similarity": 0.5,
                    "ago_min": 5,
                    "event_type": "other",
                    "symbols": [],
                }
            ],
        )
    )

    assert review.state == "rejected"
    assert "\n" not in review.reason_zh
    assert len(review.reason_zh) <= 60
    assert review.reason_zh.endswith("…")


def test_identity_names_native_dspy_render_capability_and_two_call_ceiling() -> None:
    schema = _program(ScriptedLM([], structured_output="json_schema"))
    object_mode = _program(ScriptedLM([], structured_output="json_object"))

    assert schema.identity["program_sha256"] == PROGRESSION_REVIEW_SHA256
    assert schema.identity["program"]["dspy_version"] == "3.3.1"
    assert schema.identity["program"]["signature"]["instructions"].startswith("You verify whether")
    adapter_identity = schema.identity["program"]["json_adapter"]
    assert adapter_identity["type"] == "dspy.JSONAdapter"
    assert adapter_identity["use_native_function_calling"] is False
    assert len(adapter_identity["canonical_render_sha256"]) == 64
    assert schema.identity["program"]["max_calls"] == 2
    assert schema.identity["program"]["per_call_timeout_seconds"] == 12.0
    assert schema.identity["effective_lm_capability"] == {
        "supported_params": ["response_format"],
        "supports_response_schema": True,
    }
    runtime_identity = schema.identity["runtime_identity"]
    assert runtime_identity["provider"] == "scripted"
    assert runtime_identity["model"] == "scripted/test"
    assert len(runtime_identity["model_sha256"]) == len(runtime_identity["binding_sha256"]) == 64
    assert schema.identity["model_binding"] == "progression_review.primary"
    assert schema.verifier_id != object_mode.verifier_id
    assert PROGRESSION_REVIEW_MAX_CALLS == 2


def test_identity_moves_with_the_runtime_model_and_endpoint() -> None:
    first_delegate = ScriptedLM([], model="scripted/first")
    second_delegate = ScriptedLM([], model="scripted/second")
    first = ProgressionReviewProgram(
        AuditedConfiguredLM(
            first_delegate,
            structured_output="json_schema",
            runtime_identity=RuntimeModelIdentity.issue(
                provider="scripted",
                model=first_delegate.model,
                model_sha256="a" * 64,
            ),
            predictor="progression_review",
            route="primary",
            model_binding="progression_review.primary",
        )
    )
    changed_endpoint = ProgressionReviewProgram(
        AuditedConfiguredLM(
            ScriptedLM([], model="scripted/first"),
            structured_output="json_schema",
            runtime_identity=RuntimeModelIdentity.issue(
                provider="scripted",
                model="scripted/first",
                model_sha256="b" * 64,
            ),
            predictor="progression_review",
            route="primary",
            model_binding="progression_review.primary",
        )
    )
    changed_model = ProgressionReviewProgram(
        AuditedConfiguredLM(
            second_delegate,
            structured_output="json_schema",
            runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=second_delegate.model),
            predictor="progression_review",
            route="primary",
            model_binding="progression_review.primary",
        )
    )

    assert len({first.verifier_id, changed_endpoint.verifier_id, changed_model.verifier_id}) == 3


def test_strict_answer_validation_can_spend_only_the_json_adapter_fallback() -> None:
    invalid = {
        "review": {
            "related": False,
            "candidate_i": -1,
            "reason_zh": "没有同一事件链。",
            "unexpected": "not allowed",
        }
    }
    lm = ScriptedLM([invalid, invalid])
    verifier = _program(lm)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        asyncio.run(
            verifier.review(
                event={"leader_title": "Current"},
                verdict={"headline_zh": "当前新闻", "why_zh": "当前影响"},
                candidates=[],
            )
        )

    assert len(lm.requests) == PROGRESSION_REVIEW_MAX_CALLS


def test_answer_must_name_a_visible_candidate() -> None:
    lm = ScriptedLM(
        [
            {
                "review": {
                    "related": True,
                    "candidate_i": 9,
                    "reason_zh": "声称相关但没有对应候选。",
                }
            }
        ]
    )
    verifier = _program(lm)

    with pytest.raises(ValueError, match="news_progression_review_answer_candidate_missing"):
        asyncio.run(
            verifier.review(
                event={"leader_title": "Current"},
                verdict={"headline_zh": "当前新闻", "why_zh": "当前影响"},
                candidates=[],
            )
        )

    assert len(lm.requests) == 1


def test_constructor_refuses_unwrapped_lm_that_cannot_audit_physical_calls() -> None:
    lm = dspy.BaseLM("scripted/unsafe", cache=True, num_retries=3)

    with pytest.raises(TypeError, match="news_progression_review_lm_invalid"):
        ProgressionReviewProgram(lm)


def test_review_opens_a_fresh_scope_for_the_production_audited_lm() -> None:
    delegate = ScriptedLM(
        [
            {
                "review": {
                    "related": False,
                    "candidate_i": -1,
                    "reason_zh": "没有同一事件链。",
                }
            }
        ],
        model="scripted/progression",
    )
    lm = AuditedConfiguredLM(
        delegate,
        structured_output="json_schema",
        runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=delegate.model),
        predictor="progression_review",
        route="primary",
        model_binding="progression_review.primary",
    )

    review = asyncio.run(
        ProgressionReviewProgram(lm).review(
            event={"leader_title": "Current"},
            verdict={"headline_zh": "当前新闻", "why_zh": "当前影响"},
            candidates=[],
        )
    )

    assert review.state == "rejected"
    assert len(delegate.requests) == 1
