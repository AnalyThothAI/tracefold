"""TriageModel: one structured call, classified failures, one bounded retry for fast retryable failures (transport, or
an unusable answer that is not a max_tokens truncation)."""

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
        novelty="new_fact",
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
        why_zh="",
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


def _invalid() -> dict[str, Any]:
    return {"raw": AIMessage(content=""), "parsed": None, "parsing_error": ValueError("x")}


def test_missing_structured_output_is_retried_once_within_the_deadline() -> None:
    """An empty/invalid tool call is usually transient at temperature 0 (#61 probe: 31/44 recovered on retry), so
    it earns one more attempt inside the deadline; a second failure is the classified output failure."""

    recovers = _StructuredModel([_invalid(), _ok()])
    triage = TriageModel(model=recovers, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    result = asyncio.run(triage.triage("<event/>"))
    assert recovers.calls == 2 and result.attempts == 2 and result.verdict.novelty == "new_fact"

    twice = _StructuredModel([_invalid(), _invalid()])
    triage = TriageModel(model=twice, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    with pytest.raises(TriageModelError, match="news_triage_output_invalid") as info:
        asyncio.run(triage.triage("<event/>"))
    assert twice.calls == 2 and info.value.attempts == 2
    assert info.value.output_failure is True and info.value.retryable is False
    assert info.value.finish_reason is None and info.value.detail == "ValueError: x"

    # No budget left for a retry: classified on the first invalid answer.
    tight = _StructuredModel([_invalid()])
    triage = TriageModel(model=tight, model_name="m", deadline_seconds=1.0)  # type: ignore[arg-type]
    with pytest.raises(TriageModelError, match="news_triage_output_invalid"):
        asyncio.run(triage.triage("<event/>"))
    assert tight.calls == 1


def test_a_full_verdict_missing_only_novelty_is_accepted_as_new_fact() -> None:
    """The one lenient parse: prompt-v5-shaped output (every field but ``novelty``) is a usable judgment."""

    args = _verdict().model_dump()
    args.pop("novelty")
    args.pop("restates")
    raw = AIMessage(
        content="",
        tool_calls=[{"name": "TriageVerdict", "args": args, "id": "call-1", "type": "tool_call"}],
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )
    model = _StructuredModel([{"raw": raw, "parsed": None, "parsing_error": ValueError("novelty missing")}])
    triage = TriageModel(model=model, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    result = asyncio.run(triage.triage("<event/>"))
    assert model.calls == 1 and result.attempts == 1
    assert result.novelty_defaulted is True and result.verdict.novelty == "new_fact" and result.verdict.restates == -1
    assert result.verdict.headline_zh == "英伟达投资"

    # A tool call missing anything else is still an output failure (retried once, then classified).
    broken = dict(args)
    broken.pop("headline_zh")
    raw2 = AIMessage(content="", tool_calls=[{"name": "TriageVerdict", "args": broken, "id": "c", "type": "tool_call"}])
    model = _StructuredModel([{"raw": raw2, "parsed": None, "parsing_error": ValueError("x")} for _ in range(2)])
    triage = TriageModel(model=model, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    with pytest.raises(TriageModelError, match="news_triage_output_invalid"):
        asyncio.run(triage.triage("<event/>"))
    assert model.calls == 2


def test_truncated_tool_call_is_classified_as_output_truncated_with_diagnostics() -> None:
    """A tool call cut by max_tokens (finish_reason=length) is an output failure: named, traced, never a circuit hit."""

    raw = AIMessage(
        content="",
        response_metadata={"finish_reason": "length"},
        usage_metadata={"input_tokens": 2700, "output_tokens": 300, "total_tokens": 3000},
    )
    model = _StructuredModel([{"raw": raw, "parsed": None, "parsing_error": ValueError("8 validation errors")}])
    triage = TriageModel(model=model, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    with pytest.raises(TriageModelError) as info:
        asyncio.run(triage.triage("<event/>"))
    assert info.value.code == "news_triage_output_truncated"
    assert info.value.output_failure is True and info.value.retryable is False
    assert info.value.finish_reason == "length" and info.value.output_tokens == 300
    assert info.value.detail == "ValueError: 8 validation errors"
    assert model.calls == 1


def test_retryable_classification_covers_transport_and_limit_failures() -> None:
    assert is_retryable_model_failure(TimeoutError())
    assert is_retryable_model_failure(_RateLimitError())
    assert is_retryable_model_failure(type("APIConnectionError", (Exception,), {})())
    assert not is_retryable_model_failure(_BadRequestError())
    assert not is_retryable_model_failure(ValueError())
