"""The reader judgment of one claim: its frozen input, one two-question request, the fallback and reuse.

The native tests drive DspyReaderJudge through a real SystemOneConnection, the official SDK and DSPy's
decision adapter over httpx2.MockTransport; the generative route is a scripted DSPy LM. No provider is called.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import dspy
import httpx2
import pytest
from pydantic import ValidationError

from tests.support.news_update_semantic import MemoryCache
from tests.support.scripted_lm import ScriptedLM
from tracefold.app.system_one import SystemOneConnection
from tracefold.news.updates.contracts import (
    Citation,
    ClaimFields,
    DraftClaim,
    Evidence,
    Extraction,
    FrozenInput,
    Source,
)
from tracefold.news.updates.dspy_backend import DspyReaderJudge
from tracefold.news.updates.identity import digest
from tracefold.news.updates.judgment import Budget, ConfigurationFault
from tracefold.news.updates.reader_judgments import (
    COVERAGE_QUESTION,
    IMPORTANCE_LEVELS,
    IMPORTANCE_QUESTION,
    READER_CUTS,
    READER_INSTRUCTIONS,
    READER_QUOTE_CHARS_MAX,
    CoverageEvidence,
    ImportanceEvidence,
    ReaderInput,
    ReaderJudgment,
    cache_key,
    cached_judgment,
)
from tracefold.news.updates.semantics import assemble_update
from tracefold.news.updates.topics import CODEBOOK

STAMP = 1_790_405_000_000
TOPIC, TOPIC_NAME = CODEBOOK[0]
SENT = ("英伟达宣布1500亿美元回购\n\n英伟达宣布了1500亿美元的股票回购计划。", "现货黄金跌2%\n\n现货黄金下跌2%。")


def _update(text: str = "NVIDIA ANNOUNCES $150 BILLION BUYBACK", *, adopted_at_ms: int = STAMP):
    source = Evidence.issue(
        text,
        Source(
            publisher_id="news-opennews",
            artifact_id="a1",
            artifact_revision="1",
            origin_id="opennews",
            attribution="Reuters",
            first_available_at_ms=STAMP,
            source_authority="reputable_secondary",
        ),
    )
    draft = DraftClaim(
        slot="c1",
        topics=(TOPIC,),
        statement="Nvidia announced a $150 billion share buyback.",
        fields=ClaimFields(subject="Nvidia", action="announced", object="$150 billion share buyback", mode="decision"),
        citations=(Citation(evidence_ref=source.ref, quote=text),),
    )
    update = assemble_update(
        FrozenInput(event_id="event", revision=1, lineage_id="lineage", evidence=(source,)),
        Extraction(claims=(draft,)),
        None,
        adopted_at_ms=adopted_at_ms,
    )
    assert update is not None
    return update


def _reader(messages: tuple[str, ...] = SENT) -> ReaderInput:
    update = _update()
    return ReaderInput.of(update.claims[0], update, messages)


def test_reader_input_is_the_claim_its_sources_and_the_recalled_bodies_and_nothing_time_relative() -> None:
    update = _update()
    reader = ReaderInput.of(update.claims[0], update, SENT)

    assert reader.claim.topics == (TOPIC_NAME,)
    assert reader.change == "new_fact"
    assert [(s.publisher, s.origin, s.attribution, s.authority) for s in reader.sources] == [
        ("news-opennews", "opennews", "Reuters", "reputable_secondary")
    ]
    assert reader.messages == SENT
    # A later adoption of the same content asks the same question.
    later = _update(adopted_at_ms=STAMP + 3_600_000)
    assert ReaderInput.of(later.claims[0], later, SENT).digest == reader.digest
    assert ReaderInput.of(update.claims[0], update, SENT[:1]).digest != reader.digest

    inputs = reader.model_inputs()
    assert inputs["messages"] == [{"id": "m1", "body": SENT[0]}, {"id": "m2", "body": SENT[1]}]
    claim = inputs["claim"]
    assert claim["statement"] == "Nvidia announced a $150 billion share buyback."
    assert claim["topics"] == [TOPIC_NAME] and claim["change"] == "new_fact"
    # Empty values are not model input; a claim-level "unknown" reading is.
    assert "conditions" not in claim and "quantities" not in claim and claim["polarity"] == "unknown"
    assert claim["sources"] == [
        {
            "publisher": "news-opennews",
            "origin": "opennews",
            "attribution": "Reuters",
            "authority": "reputable_secondary",
            "quote": "NVIDIA ANNOUNCES $150 BILLION BUYBACK",
        }
    ]
    assert "messages" not in ReaderInput.of(update.claims[0], update, ()).model_inputs()


def test_reader_input_bounds_quotes_and_messages() -> None:
    update = _update("X" * (READER_QUOTE_CHARS_MAX + 50))
    reader = ReaderInput.of(update.claims[0], update, ())
    assert len(reader.sources[0].quote) == READER_QUOTE_CHARS_MAX
    with pytest.raises(ValidationError):
        ReaderInput.of(update.claims[0], update, tuple(f"消息{index}" for index in range(17)))


def _score(probabilities: list[float]) -> dict[str, Any]:
    return {
        "type": "score",
        "score": sum(index * p for index, p in enumerate(probabilities)),
        "confidence": 0.8,
        "legend": {str(index): level for index, level in enumerate(IMPORTANCE_LEVELS)},
        "probabilities": {str(index): p for index, p in enumerate(probabilities)},
    }


def _choice(probabilities: dict[str, float]) -> dict[str, Any]:
    return {
        "type": "choice",
        "choice": max(probabilities, key=probabilities.__getitem__),
        "confidence": 0.9,
        "probabilities": probabilities,
    }


def _response(answers: dict[str, Any]) -> httpx2.Response:
    return httpx2.Response(
        200,
        headers={"x-typesafe-request-id": "reader-request"},
        json={"model": "jev-1.13-served", "usage": {"input_tokens": 10, "output_tokens": 0}, "answers": answers},
    )


def _connection(respond: Any) -> SystemOneConnection:
    return SystemOneConnection(
        base_url="https://api.typesafe.ai",
        api_key="example-test-key",
        model="jev-1.13.0",
        timeout_seconds=2.0,
        async_transport=httpx2.MockTransport(respond),
    )


def _judge(connection: SystemOneConnection | None, generated: Any = None, **options: Any) -> DspyReaderJudge:
    def unused() -> Any:
        raise AssertionError("the generative route must not be asked")

    native: dict[str, Any] = {}
    if connection is not None:
        native = {
            "native_lm_factory": lambda: connection.bind(timeout_seconds=2.0),
            "native_model_identity": "jev-test",
        }
    return DspyReaderJudge(
        (lambda: generated) if generated is not None else unused,
        generated_model_identity="generated-test",
        **native,
        **options,
    )


def test_one_native_request_asks_both_questions_over_one_shared_state() -> None:
    async def run() -> tuple[ReaderJudgment, list[dict[str, Any]], str]:
        sent: list[dict[str, Any]] = []

        async def respond(request: httpx2.Request) -> httpx2.Response:
            assert request.url.path == "/v1/systemone"
            sent.append(json.loads(request.content))
            return _response(
                {
                    "importance": _score([0.0, 0.1, 0.1, 0.7, 0.1]),
                    "covering_message": _choice({"m1": 0.85, "m2": 0.05, "none": 0.1}),
                }
            )

        connection = _connection(respond)
        try:
            judge = _judge(connection)
            judgment = await judge.judge(_reader(), Budget.start(5))
        finally:
            await connection.aclose()
        return judgment, sent, judge.native_identity or ""

    judgment, sent, native_identity = asyncio.run(run())
    request = sent[0]
    assert "temperature" not in request and "max_tokens" not in request
    assert request["state"]["instructions"] == READER_INSTRUCTIONS
    assert [row["id"] for row in request["state"]["inputs"]["messages"]] == ["m1", "m2"]
    assert request["state"]["inputs"]["claim"]["statement"] == "Nvidia announced a $150 billion share buyback."
    importance = request["questions"]["importance"]
    assert importance["type"] == "score" and importance["instructions"] == IMPORTANCE_QUESTION
    assert importance["criteria"] == list(IMPORTANCE_LEVELS)
    coverage = request["questions"]["covering_message"]
    assert coverage["type"] == "choice" and coverage["instructions"] == COVERAGE_QUESTION
    assert list(coverage["criteria"]) == ["m1", "m2", "none"]

    assert judgment.status == "available" and judgment.backend == "native"
    assert judgment.identity == native_identity and judgment.served_model == "jev-1.13-served"
    assert judgment.importance is not None and judgment.importance.value == pytest.approx(2.8)
    assert judgment.importance.probabilities == pytest.approx((0.0, 0.1, 0.1, 0.7, 0.1))
    assert judgment.coverage is not None and judgment.coverage.covering(judgment.cuts) == 0
    assert judgment.matches(_reader()) and not judgment.matches(_reader(SENT[:1]))


def test_no_recalled_message_asks_only_the_importance_question() -> None:
    async def run() -> tuple[ReaderJudgment, list[dict[str, Any]]]:
        sent: list[dict[str, Any]] = []

        async def respond(request: httpx2.Request) -> httpx2.Response:
            sent.append(json.loads(request.content))
            return _response({"importance": _score([0.5, 0.5, 0.0, 0.0, 0.0])})

        connection = _connection(respond)
        try:
            judgment = await _judge(connection).judge(_reader(()), Budget.start(5))
        finally:
            await connection.aclose()
        return judgment, sent

    judgment, sent = asyncio.run(run())
    assert set(sent[0]["questions"]) == {"importance"}
    assert "messages" not in sent[0]["state"]["inputs"]
    assert judgment.coverage is None and judgment.importance is not None
    assert judgment.importance.value == pytest.approx(0.5)


def _generated_answer(request: Any) -> dict[str, Any]:
    return {
        "importance": {"probabilities": {"0": 0.1, "1": 0.2, "2": 0.4, "3": 0.2, "4": 0.1}, "confidence": 0.6},
        "covering_message": {"probabilities": {"m1": 0.1, "m2": 0.1, "none": 0.8}, "confidence": 0.7},
    }


def test_an_unavailable_native_answer_falls_back_once_to_the_same_questions_on_the_generative_route() -> None:
    async def run() -> tuple[ReaderJudgment, int, ScriptedLM, DspyReaderJudge]:
        requests = 0

        async def respond(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests
            requests += 1
            return httpx2.Response(529, json={"error": {"message": "overloaded"}})

        connection = _connection(respond)
        generated = ScriptedLM([_generated_answer])
        judge = _judge(connection, generated)
        try:
            judgment = await judge.judge(_reader(), Budget.start(5))
        finally:
            await connection.aclose()
        return judgment, requests, generated, judge

    judgment, requests, generated, judge = asyncio.run(run())
    assert requests == 1 and len(generated.requests) == 1
    assert judgment.status == "available" and judgment.backend == "generated"
    assert judgment.identity == judge.generated_identity and judgment.cuts == READER_CUTS["generated"]
    assert judgment.importance is not None and judgment.importance.value == pytest.approx(2.0)
    assert judgment.coverage is not None and judgment.coverage.covering(judgment.cuts) is None
    request = generated.requests[0]
    prompt = "\n".join([str(request.system or ""), *(message.text for message in request.messages)])
    assert READER_INSTRUCTIONS in prompt
    for text in (IMPORTANCE_LEVELS[3], COVERAGE_QUESTION):
        assert json.dumps(text, ensure_ascii=False)[1:-1] in prompt


def test_a_slow_native_answer_falls_back_within_the_stage_deadline() -> None:
    async def run() -> ReaderJudgment:
        async def respond(request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(1.0)
            return _response({"importance": _score([1.0, 0.0, 0.0, 0.0, 0.0])})

        connection = _connection(respond)
        try:
            judge = _judge(connection, ScriptedLM([_generated_answer]), native_operation_seconds=0.05)
            return await judge.judge(_reader(), Budget.start(5))
        finally:
            await connection.aclose()

    assert asyncio.run(run()).backend == "generated"


def test_when_neither_backend_answers_the_judgment_is_unavailable_and_never_stored() -> None:
    async def run() -> tuple[ReaderJudgment, ReaderJudgment, MemoryCache]:
        async def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(503, json={"error": {"message": "down"}})

        connection = _connection(respond)
        generated = ScriptedLM([dspy.LMServerError("down"), dspy.LMServerError("down")])
        judge = _judge(connection, generated)
        cache = MemoryCache()
        try:
            first = await cached_judgment(judge, cache, _reader(), Budget.start(5))
            second = await cached_judgment(judge, cache, _reader(), Budget.start(5))
        finally:
            await connection.aclose()
        return first, second, cache

    first, second, cache = asyncio.run(run())
    assert first.status == second.status == "unavailable"
    assert first.error_code == "news_generation_LMServerError"
    assert first.importance is None and first.coverage is None
    assert cache.values == {}


def test_a_native_authentication_fault_is_never_hidden_by_the_fallback() -> None:
    async def run() -> None:
        async def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(401, json={"error": {"message": "bad key"}})

        connection = _connection(respond)
        try:
            with pytest.raises(ConfigurationFault, match="news_reader_http_401"):
                await _judge(connection).judge(_reader(), Budget.start(5))
        finally:
            await connection.aclose()

    asyncio.run(run())


class _CountingJudge:
    identity = "counting-judge"

    def __init__(self) -> None:
        self.calls = 0

    async def judge(self, reader: ReaderInput, budget: Budget) -> ReaderJudgment:
        self.calls += 1
        return ReaderJudgment(
            status="available",
            backend="native",
            identity="native-test",
            importance=ImportanceEvidence(value=2.6, probabilities=(0.0, 0.1, 0.3, 0.5, 0.1), confidence=0.7),
            coverage=CoverageEvidence(probabilities={"m1": 0.2, "m2": 0.1, "none": 0.7}, confidence=0.7),
        )


def test_an_available_answer_is_reused_for_exactly_the_same_input() -> None:
    async def run() -> tuple[_CountingJudge, MemoryCache, ReaderJudgment]:
        judge, cache = _CountingJudge(), MemoryCache()
        first = await cached_judgment(judge, cache, _reader(), Budget.start(5))
        again = await cached_judgment(judge, cache, _reader(), Budget.start(5))
        assert again == first
        other = _update("NVIDIA ANNOUNCES $160 BILLION BUYBACK")
        await cached_judgment(judge, cache, ReaderInput.of(other.claims[0], other, SENT), Budget.start(5))
        return judge, cache, first

    judge, cache, first = asyncio.run(run())
    assert judge.calls == 2
    stored = cache.values[cache_key(judge, _reader())]
    assert stored.status == "available" and ReaderJudgment.model_validate_json(str(stored.value)) == first


def test_evidence_shapes_and_judgment_status_are_exact() -> None:
    with pytest.raises(ValidationError, match="news_reader_coverage_options_invalid"):
        CoverageEvidence(probabilities={"m1": 0.5, "m3": 0.1, "none": 0.4}, confidence=0.5)
    with pytest.raises(ValidationError, match="news_reader_coverage_options_invalid"):
        CoverageEvidence(probabilities={"none": 1.0}, confidence=0.5)
    with pytest.raises(ValidationError):
        ImportanceEvidence(value=2.0, probabilities=(0.5, 0.5), confidence=0.5)
    with pytest.raises(ValidationError, match="news_reader_unavailable_judgment_has_answer"):
        ReaderJudgment(status="unavailable", error_code="x", backend="native")
    with pytest.raises(ValidationError, match="news_reader_available_judgment_incomplete"):
        ReaderJudgment(status="available", backend="native", identity="n")
    coverage = CoverageEvidence(probabilities={"m1": 0.1, "m2": 0.45, "none": 0.45}, confidence=0.5)
    assert coverage.covering(READER_CUTS["native"]) == 1
    for cuts in READER_CUTS.values():
        assert 0 < cuts.push < cuts.key < len(IMPORTANCE_LEVELS) - 1 and 0 < cuts.covered_none_below < 1


def test_the_cache_key_names_the_judge_and_the_frozen_input_only() -> None:
    judge = _CountingJudge()
    assert cache_key(judge, _reader()) == cache_key(judge, _reader())
    assert cache_key(judge, _reader()) != cache_key(judge, _reader(SENT[:1]))
    assert digest(_reader()) == _reader().digest
