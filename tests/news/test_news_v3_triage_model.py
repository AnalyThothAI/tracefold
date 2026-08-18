"""TriageModel: one structured call, classified failures, one bounded retry for fast retryable failures."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from tracefold.news.agents.triage_model import TriageModel, TriageModelError, is_retryable_model_failure
from tracefold.news.models import TriageVerdict


class _RateLimitError(Exception):
    pass


class _BadRequestError(Exception):
    pass


class _StructuredModel:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _StructuredModel:
        assert schema is TriageVerdict and kwargs["method"] == "function_calling"
        return self

    async def ainvoke(self, messages: list[object]) -> dict[str, Any]:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _verdict() -> TriageVerdict:
    return TriageVerdict(
        event_type="partnership",
        assets=[],
        direction="bullish",
        scope="single_name",
        magnitude=2,
        actionable=True,
        confidence=0.7,
        decision="push",
        headline_zh="英伟达投资",
        title_zh="英伟达将投资 1000 亿美元",
        rationale="",
    )


def _ok() -> dict[str, Any]:
    raw = AIMessage(content="", usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    return {"raw": raw, "parsed": _verdict(), "parsing_error": None}


def test_retryable_failure_earns_one_more_attempt_within_the_deadline() -> None:
    model = _StructuredModel([_RateLimitError("429"), _ok()])
    triage = TriageModel(model=model, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]

    result = asyncio.run(triage.triage("<event/>"))

    assert model.calls == 2 and result.attempts == 2
    assert result.verdict.title_zh == "英伟达将投资 1000 亿美元" and result.input_tokens == 100


def test_non_retryable_failure_and_exhausted_retry_are_classified() -> None:
    bad = _StructuredModel([_BadRequestError("schema")])
    triage = TriageModel(model=bad, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    with pytest.raises(TriageModelError) as first:
        asyncio.run(triage.triage("<event/>"))
    assert first.value.retryable is False and first.value.attempts == 1 and bad.calls == 1
    assert first.value.code == "news_triage_model_failed:_BadRequestError"

    flaky = _StructuredModel([_RateLimitError("429"), _RateLimitError("429")])
    triage = TriageModel(model=flaky, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    with pytest.raises(TriageModelError) as second:
        asyncio.run(triage.triage("<event/>"))
    assert second.value.retryable is True and second.value.attempts == 2 and flaky.calls == 2


def test_missing_structured_output_is_not_retried() -> None:
    model = _StructuredModel([{"raw": AIMessage(content=""), "parsed": None, "parsing_error": ValueError("x")}])
    triage = TriageModel(model=model, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    with pytest.raises(TriageModelError, match="news_triage_output_invalid"):
        asyncio.run(triage.triage("<event/>"))
    assert model.calls == 1


def test_retryable_classification_covers_transport_and_limit_failures() -> None:
    assert is_retryable_model_failure(TimeoutError())
    assert is_retryable_model_failure(_RateLimitError())
    assert is_retryable_model_failure(type("APIConnectionError", (Exception,), {})())
    assert not is_retryable_model_failure(_BadRequestError())
    assert not is_retryable_model_failure(ValueError())
