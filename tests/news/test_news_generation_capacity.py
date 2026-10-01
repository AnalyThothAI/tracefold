"""News runtime generation admission, with recorded-shape outputs and no provider or database."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from tests.support.news_update_semantic import MemoryCache, update_one
from tracefold.app.learning_runtime import generative_lm
from tracefold.app.llm import ConfiguredLMEndpoint
from tracefold.app.news_updates import compose_news_updates
from tracefold.news.adapters import generation
from tracefold.news.adapters.reader_judge import DspyReaderJudge
from tracefold.news.adapters.semantic_judgments import NativeJudgments
from tracefold.news.generation_capacity import (
    NewsGenerationCapacity,
    generation_call,
    generation_capacity_wait_before_call,
    generation_capacity_wait_timed_out,
    generation_stage,
)
from tracefold.news.notifications.reader import ReaderInput, cached_judgments
from tracefold.news.updates.judgment import Budget, Question, error_code


def _reader_prediction() -> Any:
    return SimpleNamespace(
        importance=SimpleNamespace(value=2.0, probabilities={index: 0.2 for index in range(5)}, confidence=0.5)
    )


def test_composed_news_roles_share_one_actual_generation_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    active = peak = 0
    calls: list[str] = []

    class Predict:
        def __init__(self, signature: Any) -> None:
            del signature

        async def acall(self, *, lm: str, **inputs: Any) -> Any:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            calls.append(lm)
            try:
                await asyncio.sleep(0.005)
                if lm == "extraction":
                    return SimpleNamespace(result={"claims": []})
                if lm == "judgment":
                    return SimpleNamespace(
                        result={
                            "answers": [{"item_id": row["item_id"], "value": "unrelated"} for row in inputs["items"]]
                        }
                    )
                if lm == "reader":
                    return _reader_prediction()
                return SimpleNamespace(
                    result={"headline_zh": "机构公布关税", "lines": [{"claim_ref": "c1", "text_zh": "机构公布关税。"}]}
                )
            finally:
                active -= 1

    monkeypatch.setattr(dspy, "Predict", Predict)

    async def run() -> None:
        source, _, update = update_one()
        runtime = compose_news_updates(
            semantic_store=SimpleNamespace(),
            notification_store=SimpleNamespace(),
            relation_cache=MemoryCache(),
            extraction_lm_factory=lambda: "extraction",
            judgment_lm_factory=lambda: "judgment",
            card_lm_factory=lambda: "card",
            extraction_model_identity="extraction-fixture",
            judgment_model_identity="judgment-fixture",
            card_model_identity="card-fixture",
            max_model_concurrent_calls=2,
        )
        extractor = runtime.agent.analyzer.extractor
        semantic = runtime.judgments.generated
        reader = runtime.notifications.planner.judge
        composer = runtime.notifications.composer
        # The reader shares the same judgment route; distinguish its signature in this fixture.
        reader.generated_lm_factory = lambda: "reader"
        reader_input = ReaderInput.of(update.claims[0], update, [])
        assert all(
            adapter.generation_capacity is runtime.generation_capacity
            for adapter in (extractor, semantic, reader, composer)
        )

        async def roles() -> None:
            await asyncio.gather(
                extractor.extract(source),
                semantic.judge("relation", (Question(item_id="prior", payload_json="{}"),), context_json=None),
                reader.judge(reader_input, Budget.start(5)),
                composer.compose(update.claims, sources={row.ref: row.source for row in update.evidence}),
            )

        await asyncio.gather(roles(), roles())
        await runtime.aclose()

    asyncio.run(run())
    assert peak == 2 and active == 0
    assert sorted(calls) == sorted(["extraction", "judgment", "reader", "card"] * 2)


@pytest.mark.parametrize("during_call", [False, True], ids=["waiting", "provider"])
def test_direct_task_cancellation_propagates_and_releases_admission(
    monkeypatch: pytest.MonkeyPatch, during_call: bool
) -> None:
    started = asyncio.Event()
    calls: list[str] = []

    class Predict:
        def __init__(self, signature: Any) -> None:
            del signature

        async def acall(self, *, lm: str, **inputs: Any) -> str:
            calls.append(lm)
            if lm == "blocked":
                started.set()
                await asyncio.Future()
            return "answer"

    monkeypatch.setattr(dspy, "Predict", Predict)

    async def run() -> None:
        capacity = NewsGenerationCapacity(1)
        if during_call:
            task = asyncio.create_task(generation.generate(object(), "blocked", capacity=capacity))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            async with capacity.acquire():
                task = asyncio.create_task(generation.generate(object(), "blocked", capacity=capacity))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert task.cancelled()
        assert await asyncio.wait_for(generation.generate(object(), "next", capacity=capacity), 0.5) == "answer"

    asyncio.run(run())
    assert calls == (["blocked", "next"] if during_call else ["next"])


@pytest.mark.parametrize("fanout", [False, True], ids=["direct", "gather"])
def test_stage_deadline_during_admission_keeps_local_wait_cause(fanout: bool) -> None:
    async def run() -> None:
        capacity = NewsGenerationCapacity(1)

        async def admitted() -> None:
            async with capacity.acquire():
                pytest.fail("a blocked stage must not enter the provider")

        async with capacity.acquire():
            with pytest.raises(TimeoutError) as caught:
                async with asyncio.timeout(0.02):
                    if fanout:
                        await asyncio.gather(admitted(), admitted())
                    else:
                        await admitted()
            assert generation_capacity_wait_timed_out(caught.value)
            assert error_code(caught.value, default="news_provider_unavailable") == "news_generation_capacity_wait"
        async with asyncio.timeout(0.5), capacity.acquire():
            pass

    asyncio.run(run())


def test_provider_timeout_is_not_classified_as_local_admission() -> None:
    async def run() -> None:
        capacity = NewsGenerationCapacity(1)
        with pytest.raises(TimeoutError) as caught:
            async with asyncio.timeout(0.02), capacity.acquire():
                await asyncio.Future()
        assert not generation_capacity_wait_timed_out(caught.value)
        assert error_code(caught.value, default="news_provider_unavailable") == "news_provider_unavailable:TimeoutError"
        async with asyncio.timeout(0.5), capacity.acquire():
            pass

    asyncio.run(run())


def test_fallback_gets_a_new_slot_and_releases_a_failed_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    primary_started = asyncio.Event()
    release_primary = asyncio.Event()

    class Predict:
        def __init__(self, signature: Any) -> None:
            del signature

        async def acall(self, *, lm: str, **inputs: Any) -> str:
            calls.append(lm)
            if lm == "primary":
                primary_started.set()
                await release_primary.wait()
                raise dspy.LMRateLimitError("fixture rate limit")
            return "answer"

    monkeypatch.setattr(dspy, "Predict", Predict)

    async def run() -> None:
        capacity = NewsGenerationCapacity(1)
        route = asyncio.create_task(generation.generate(object(), ("primary", "fallback"), capacity=capacity))
        await primary_started.wait()
        sibling = asyncio.create_task(generation.generate(object(), "sibling", capacity=capacity))
        await asyncio.sleep(0)
        release_primary.set()
        assert await asyncio.wait_for(route, 0.5) == "answer"
        assert await sibling == "answer"
        assert await generation.generate(object(), "after", capacity=capacity) == "answer"

    asyncio.run(run())
    # The queued sibling can proceed between the failed primary and the declared fallback.
    assert calls == ["primary", "sibling", "fallback", "after"]


def test_reader_fanout_wait_timeout_has_local_code_and_no_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Predict:
        def __init__(self, signature: Any) -> None:
            del signature

        async def acall(self, *, lm: str, **inputs: Any) -> Any:
            calls.append(lm)
            return _reader_prediction()

    monkeypatch.setattr(dspy, "Predict", Predict)

    async def run() -> None:
        capacity = NewsGenerationCapacity(1)
        _, _, update = update_one()
        reader = ReaderInput.of(update.claims[0], update, [])
        judge = DspyReaderJudge(lambda: "reader", generated_model_identity="fixture", generation_capacity=capacity)
        async with capacity.acquire():
            values = await cached_judgments(judge, MemoryCache(), {"a": reader, "b": reader}, Budget.start(0.02))
        assert all(
            value.status == "unavailable" and value.error_code == "news_generation_capacity_wait"
            for value in values.values()
        )
        assert (await judge.judge(reader, Budget.start(1))).status == "available"

    asyncio.run(run())
    assert calls == ["reader"]


def test_native_semantic_and_reader_calls_do_not_use_generation_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Predict:
        def __init__(self, signature: Any) -> None:
            del signature

        async def acall(self, *, lm: str, **inputs: Any) -> Any:
            calls.append(lm)
            if lm == "native-reader":
                return _reader_prediction()
            return SimpleNamespace(answer_0=SimpleNamespace(value="unrelated", probabilities={"unrelated": 1.0}))

    monkeypatch.setattr(dspy, "Predict", Predict)

    async def run() -> None:
        capacity = NewsGenerationCapacity(1)
        semantic = NativeJudgments(lambda: "native-semantic", model_identity="fixture")
        judge = DspyReaderJudge(
            lambda: "generated",
            generated_model_identity="fixture",
            native_lm_factory=lambda: "native-reader",
            native_model_identity="native-fixture",
            generation_capacity=capacity,
        )
        _, _, update = update_one()
        async with capacity.acquire():
            semantic_result, reader_result = await asyncio.wait_for(
                asyncio.gather(
                    semantic.judge("relation", (Question(item_id="prior", payload_json="{}"),), context_json=None),
                    judge.judge(ReaderInput.of(update.claims[0], update, []), Budget.start(1)),
                ),
                0.5,
            )
        assert semantic_result.answers[0].value == "unrelated"
        assert reader_result.status == "available" and reader_result.backend == "native"

    asyncio.run(run())
    assert sorted(calls) == ["native-reader", "native-semantic"]


def test_same_endpoint_trading_lm_does_not_take_news_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def provider(self: Any, *args: Any, **kwargs: Any) -> list[str]:
        calls.append(self.model)
        return ["fixture"]

    monkeypatch.setattr(dspy.LM, "acall", provider)

    async def run() -> None:
        capacity = NewsGenerationCapacity(1)
        lm = generative_lm(
            ConfiguredLMEndpoint(
                model_name="openai/trading-fixture",
                api_key="fixture",
                api_base="https://same.fixture/v1",
                model_kwargs={},
            ),
            max_tokens=1000,
            timeout=1,
        )
        async with capacity.acquire():
            assert await asyncio.wait_for(lm.acall("fixture"), 0.5) == ["fixture"]

    asyncio.run(run())
    assert calls == ["openai/trading-fixture"]


def test_mixed_fanout_keeps_provider_timeout_when_a_waiter_cancels_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from tracefold.news.updates.judgment import _all

    started = asyncio.Event()

    class Predict:
        def __init__(self, signature: Any) -> None:
            del signature

        async def acall(self, **inputs: Any) -> None:
            started.set()
            try:
                await asyncio.Future()
            finally:
                # A waiter can finish cancellation before a running provider finishes its cleanup.
                await asyncio.sleep(0.01)

    monkeypatch.setattr(dspy, "Predict", Predict)

    async def run() -> None:
        capacity = NewsGenerationCapacity(1)

        async def waiter() -> None:
            await started.wait()
            await generation.generate(object(), "waiting", capacity=capacity)

        with generation_stage() as stage, pytest.raises(TimeoutError) as caught:
            async with asyncio.timeout(0.02):
                await _all((generation.generate(object(), "provider", capacity=capacity), waiter()))
        assert stage.started_calls == 1 and stage.cancelled_calls == 1
        assert not generation_capacity_wait_timed_out(caught.value)
        assert not generation_capacity_wait_before_call(caught.value)
        assert error_code(caught.value, default="news_provider_unavailable") == "news_provider_unavailable:TimeoutError"

    asyncio.run(run())


def test_successful_earlier_call_then_admission_timeout_is_not_a_zero_call_stage() -> None:
    async def run() -> None:
        capacity = NewsGenerationCapacity(1)
        with generation_stage() as stage:
            with generation_call():
                pass  # A completed native/generated provider call in this stage.
            async with capacity.acquire():
                with pytest.raises(TimeoutError) as caught:
                    async with asyncio.timeout(0.02):
                        async with capacity.acquire():
                            pytest.fail("stage deadline must bound admission")
        assert stage.started_calls == 1 and stage.cancelled_calls == 0
        assert generation_capacity_wait_timed_out(caught.value)
        assert not generation_capacity_wait_before_call(caught.value)

    asyncio.run(run())


def test_generation_stage_observations_do_not_leak_between_turns() -> None:
    async def run() -> None:
        capacity = NewsGenerationCapacity(1)
        with generation_stage() as first, generation_call():
            pass
        with generation_stage() as second:
            async with capacity.acquire():
                with pytest.raises(TimeoutError) as caught:
                    async with asyncio.timeout(0.02), capacity.acquire():
                        pytest.fail("a blocked stage cannot start a call")
        assert first.started_calls == 1 and second.started_calls == 0
        assert generation_capacity_wait_before_call(caught.value)

    asyncio.run(run())
