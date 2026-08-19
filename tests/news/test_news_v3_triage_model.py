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
    missing = {"raw": raw, "parsed": None, "parsing_error": ValueError("novelty missing")}
    # The retry comes first (the omission is usually transient); only a second omission is accepted leniently.
    model = _StructuredModel([dict(missing), _ok()])
    triage = TriageModel(model=model, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    result = asyncio.run(triage.triage("<event/>"))
    assert model.calls == 2 and result.attempts == 2 and result.novelty_defaulted is False

    model = _StructuredModel([dict(missing), dict(missing)])
    triage = TriageModel(model=model, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    result = asyncio.run(triage.triage("<event/>"))
    assert model.calls == 2 and result.attempts == 2
    assert result.novelty_defaulted is True and result.verdict.novelty == "new_fact" and result.verdict.restates == -1
    assert result.verdict.headline_zh == "英伟达投资"

    # No retry budget left: the lenient parse applies on the first answer.
    tight = _StructuredModel([dict(missing)])
    triage = TriageModel(model=tight, model_name="m", deadline_seconds=1.0)  # type: ignore[arg-type]
    result = asyncio.run(triage.triage("<event/>"))
    assert tight.calls == 1 and result.novelty_defaulted is True

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


# --- fallback chain (issue #65) -------------------------------------------------------------------------------------


def _chain(primary_outcomes: list[Any], fallback_outcomes: list[Any], **kwargs: Any) -> tuple[Any, Any, TriageModel]:
    primary = _StructuredModel(primary_outcomes)
    fallback = _StructuredModel(fallback_outcomes)
    fb = TriageModel(model=fallback, model_name="deepseek-chat", deadline_seconds=6.0)  # type: ignore[arg-type]
    chain = TriageModel(
        model=primary,  # type: ignore[arg-type]
        model_name="qwen3.8-27b",
        deadline_seconds=6.0,
        fallback=fb,
        primary_breaker_failures=kwargs.get("breaker_failures", 3),
        primary_breaker_open_seconds=kwargs.get("breaker_open_seconds", 60.0),
    )
    return primary, fallback, chain


def test_primary_success_never_touches_the_fallback() -> None:
    primary, fallback, chain = _chain([_ok()], [_ok()])
    result = asyncio.run(chain.triage("<event/>"))
    assert result.model == "qwen3.8-27b" and result.fallback_from is None
    assert primary.calls == 1 and fallback.calls == 0


def test_primary_failure_is_answered_by_the_fallback_and_recorded() -> None:
    primary, fallback, chain = _chain([_BadRequestError("schema")], [_ok()])
    result = asyncio.run(chain.triage("<event/>"))
    assert result.model == "deepseek-chat"
    assert result.fallback_from == "news_triage_model_failed:_BadRequestError"
    assert result.attempts == 2 and primary.calls == 1 and fallback.calls == 1


def test_primary_output_failure_also_falls_back() -> None:
    truncated = {"raw": AIMessage(content="", response_metadata={"finish_reason": "length"}), "parsed": None}
    _, fallback, chain = _chain([truncated], [_ok()])
    result = asyncio.run(chain.triage("<event/>"))
    assert result.model == "deepseek-chat" and result.fallback_from == "news_triage_output_truncated"
    assert fallback.calls == 1


def test_both_links_failing_raises_the_fallback_error_with_the_primary_code() -> None:
    _, _, chain = _chain([_RateLimitError("429"), _RateLimitError("429")], [_BadRequestError("schema")])
    with pytest.raises(TriageModelError) as exc:
        asyncio.run(chain.triage("<event/>"))
    assert exc.value.code == "news_triage_model_failed:_BadRequestError"
    assert exc.value.primary_code == "news_triage_model_failed:_RateLimitError"
    assert exc.value.attempts == 3 and exc.value.retryable is False


def test_primary_breaker_skips_a_failing_primary_until_it_reopens() -> None:
    outcomes = [_BadRequestError("x"), _BadRequestError("x"), _ok()]
    primary, fallback, chain = _chain(outcomes, [_ok(), _ok(), _ok(), _ok()], breaker_failures=2)
    asyncio.run(chain.triage("<event/>"))  # failure 1 -> fallback
    asyncio.run(chain.triage("<event/>"))  # failure 2 -> breaker opens
    third = asyncio.run(chain.triage("<event/>"))  # primary skipped
    assert third.fallback_from == "primary_circuit_open" and third.model == "deepseek-chat"
    assert primary.calls == 2 and fallback.calls == 3
    chain._breaker.open_until = 0.0  # the window elapsed
    fourth = asyncio.run(chain.triage("<event/>"))
    assert fourth.model == "qwen3.8-27b" and fourth.fallback_from is None and primary.calls == 3


def test_without_fallback_the_error_surfaces_unchanged() -> None:
    model = _StructuredModel([_BadRequestError("schema")])
    triage = TriageModel(model=model, model_name="m", deadline_seconds=6.0)  # type: ignore[arg-type]
    with pytest.raises(TriageModelError) as exc:
        asyncio.run(triage.triage("<event/>"))
    assert exc.value.primary_code is None
