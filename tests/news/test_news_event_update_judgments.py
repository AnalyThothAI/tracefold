"""Judgment batching, budgets and the native DSPy decision path.

The native tests drive NativeJudgments through a real SystemOneConnection, the official SDK and DSPy's
decision adapter over httpx2.MockTransport. No provider is called.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest

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
from tracefold.news.updates.dspy_backend import NativeJudgments
from tracefold.news.updates.judgment import (
    OPTIONS,
    Answer,
    BatchResult,
    Budget,
    ConfigurationFault,
    ContractFault,
    NewsJudgments,
    ProviderUnavailable,
    Question,
    Task,
)
from tracefold.news.updates.semantics import SemanticAnalyzer
from tracefold.news.updates.topics import CODEBOOK

STAMP = 1_790_405_000_000


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, Answer] = {}

    async def get(self, key: str) -> Answer | None:
        return self.values.get(key)

    async def put(self, key: str, answer: Answer) -> None:
        self.values.setdefault(key, answer)


class Backend:
    """A test double answering every item of a batch with one value."""

    def __init__(
        self,
        value: str | bool = "none",
        *,
        identity: str = "test-backend",
        fail_batch: int | None = None,
        cancel: bool = False,
        delays: dict[int, float] | None = None,
    ) -> None:
        self.identity = identity
        self.value = value
        self.fail_batch = fail_batch
        self.cancel = cancel
        self.delays = delays or {}
        self.calls: list[tuple[Task, tuple[str, ...], str | None]] = []

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        self.calls.append((task, tuple(item.item_id for item in items), context_json))
        number = len(self.calls)
        if self.cancel:
            raise asyncio.CancelledError
        if number == self.fail_batch:
            raise ProviderUnavailable("controlled provider failure")
        if number in self.delays:
            await asyncio.sleep(self.delays[number])
        return BatchResult(
            answers=tuple(Answer(item_id=item.item_id, value=self.value, backend=self.identity) for item in items)
        )


def questions(count: int) -> tuple[Question, ...]:
    return tuple(Question(item_id=str(index), payload_json="{}") for index in range(count))


def test_failed_native_batch_does_not_rejudge_successful_batch_or_truncate_tail() -> None:
    async def run() -> None:
        native = Backend(fail_batch=2, identity="native")
        generated = Backend(identity="generated")
        judgments = NewsJudgments(generated=generated, native=native, cache=MemoryCache(), batch_size=3)
        answers = await judgments.judge("coverage", questions(8), Budget.start(10))
        assert len(answers) == 8
        assert len(native.calls) == 3
        assert [call[1] for call in generated.calls] == [("3", "4", "5")]

    asyncio.run(run())


def test_cancelled_native_batch_never_falls_back() -> None:
    async def run() -> None:
        generated = Backend(identity="generated")
        judgments = NewsJudgments(generated=generated, native=Backend(cancel=True), cache=MemoryCache())
        with pytest.raises(asyncio.CancelledError):
            await judgments.judge("coverage", questions(1), Budget.start(5))
        assert generated.calls == []

    asyncio.run(run())


def test_each_native_batch_has_its_own_operation_timeout() -> None:
    async def run() -> None:
        # Five batches take longer together than one operation budget. None of them falls back:
        # there is no shared native window that expires after the first few batches.
        native = Backend(identity="native", delays=dict.fromkeys(range(1, 6), 0.03))
        generated = Backend(identity="generated")
        judgments = NewsJudgments(
            generated=generated,
            native=native,
            cache=MemoryCache(),
            batch_size=1,
            native_operation_seconds=0.1,
        )
        answers = await judgments.judge("coverage", questions(5), Budget.start(5))
        assert {answer.backend for answer in answers} == {"native"}
        assert len(native.calls) == 5
        assert generated.calls == []

    asyncio.run(run())


def test_a_slow_native_batch_falls_back_alone() -> None:
    async def run() -> None:
        native = Backend(identity="native", delays={2: 0.5})
        generated = Backend(identity="generated")
        judgments = NewsJudgments(
            generated=generated,
            native=native,
            cache=MemoryCache(),
            batch_size=2,
            native_operation_seconds=0.05,
        )
        answers = await judgments.judge("coverage", questions(6), Budget.start(5))
        assert [answer.backend for answer in answers] == [
            "native",
            "native",
            "generated",
            "generated",
            "native",
            "native",
        ]
        assert [call[1] for call in generated.calls] == [("2", "3")]

    asyncio.run(run())


def test_native_operation_timeout_never_extends_the_shared_stage_deadline() -> None:
    async def run() -> None:
        native = Backend(identity="native", delays={1: 0.05, 2: 0.5})
        generated = Backend(identity="generated")
        judgments = NewsJudgments(
            generated=generated,
            native=native,
            cache=MemoryCache(),
            batch_size=1,
            native_operation_seconds=5.0,
        )
        with pytest.raises(TimeoutError):
            await judgments.judge("coverage", questions(3), Budget.start(0.15))
        # The second batch was cut at the stage deadline, not at its own 5 s limit, and an expired
        # stage has no generated fallback.
        assert len(native.calls) == 2
        assert generated.calls == []

    asyncio.run(run())


def test_topic_codebook_is_one_request_with_shared_context() -> None:
    async def run() -> None:
        native = Backend(True, identity="native")
        judgments = NewsJudgments(generated=Backend(identity="generated"), native=native, cache=MemoryCache())
        items = tuple(Question(item_id=code, payload_json=json.dumps({"topic": label})) for code, label in CODEBOOK)
        context = json.dumps({"claims": ["one shared copy"]})
        answers = await judgments.judge("topic", items, Budget.start(5), context_json=context)
        assert len(answers) == len(CODEBOOK) > judgments.batch_size
        assert native.calls == [("topic", tuple(code for code, _label in CODEBOOK), context)]

    asyncio.run(run())


def test_other_tasks_keep_their_packing_batch_size() -> None:
    judgments = NewsJudgments(generated=Backend(), cache=MemoryCache())
    assert [len(batch) for batch in judgments.batches("relation", questions(20))] == [8, 8, 4]
    with pytest.raises(ValueError, match="news_judgment_batch_size_invalid"):
        NewsJudgments(generated=Backend(), cache=MemoryCache(), batch_size=33)
    with pytest.raises(ContractFault, match="news_single_request_too_large"):
        judgments.batches("topic", questions(65))


def test_reask_uses_the_generated_backend_once_and_caches_apart_from_first_answers() -> None:
    async def run() -> None:
        native = Backend("unknown", identity="native")
        generated = Backend("decision", identity="generated")
        judgments = NewsJudgments(generated=generated, native=native, cache=MemoryCache())
        first = await judgments.judge("mode", questions(1), Budget.start(5))
        again = await judgments.reask("mode", questions(1), Budget.start(5))
        repeat = await judgments.reask("mode", questions(1), Budget.start(5))
        assert first[0].value == "unknown"
        assert again[0].value == repeat[0].value == "decision"
        assert len(native.calls) == 1
        assert len(generated.calls) == 1

    asyncio.run(run())


def test_unavailable_generated_answer_is_explicit_and_not_cached() -> None:
    async def run() -> None:
        generated = Backend(identity="generated", fail_batch=1)
        cache = MemoryCache()
        judgments = NewsJudgments(generated=generated, cache=cache)
        answers = await judgments.judge("coverage", questions(2), Budget.start(5))
        assert {answer.status for answer in answers} == {"unavailable"}
        assert all(answer.value is None for answer in answers)
        assert cache.values == {}

    asyncio.run(run())


# ------------------------------------------------------------------ native decoding over the real SDK


def _choice(task: Task, value: str) -> dict[str, Any]:
    options = [option for option, _description in OPTIONS[task]]
    rest = (1 - 0.7) / (len(options) - 1)
    probabilities = {option: 0.7 if option == value else rest for option in options}
    return {"type": "choice", "choice": value, "confidence": 0.7, "probabilities": probabilities}


def _connection(respond: Any) -> SystemOneConnection:
    return SystemOneConnection(
        base_url="https://api.typesafe.ai",
        api_key="example-test-key",
        model="jev-1.13.0",
        timeout_seconds=2.0,
        async_transport=httpx2.MockTransport(respond),
    )


def _response(answers: dict[str, Any]) -> httpx2.Response:
    return httpx2.Response(
        200,
        headers={"x-typesafe-request-id": "native-request"},
        json={"model": "jev-1.13.0", "usage": {"input_tokens": 10, "output_tokens": 0}, "answers": answers},
    )


def test_native_choice_batch_decodes_each_slot_back_to_its_item() -> None:
    async def run() -> None:
        sent: list[dict[str, Any]] = []

        async def respond(request: httpx2.Request) -> httpx2.Response:
            assert request.url.path == "/v1/systemone"
            sent.append(json.loads(request.content))
            return _response(
                {"answer_0": _choice("relation", "equivalent"), "answer_1": _choice("relation", "unrelated")}
            )

        connection = _connection(respond)
        try:
            native = NativeJudgments(lambda: connection.bind(timeout_seconds=2.0), model_identity="jev-test")
            items = (
                Question(item_id="pair:a", payload_json=json.dumps({"current": "A", "previous": "A again"})),
                Question(item_id="pair:b", payload_json=json.dumps({"current": "B", "previous": "C"})),
            )
            result = await native.judge("relation", items, context_json=None)
        finally:
            await connection.aclose()
        assert [(row.item_id, row.value) for row in result.answers] == [
            ("pair:a", "equivalent"),
            ("pair:b", "unrelated"),
        ]
        assert result.answers[0].probabilities is not None
        assert result.answers[0].probabilities["equivalent"] == pytest.approx(0.7)
        request = sent[0]
        assert request["model"] == "jev-1.13.0"
        assert "temperature" not in request and "max_tokens" not in request
        assert [row["item_id"] for row in request["state"]["inputs"]["items"]] == ["pair:a", "pair:b"]
        assert "context" not in request["state"]["inputs"]
        assert set(request["questions"]) == {"answer_0", "answer_1"}
        question = request["questions"]["answer_1"]
        assert question["type"] == "choice"
        assert "inputs.items[1]" in question["instructions"]
        assert set(question["criteria"]) == {option for option, _description in OPTIONS["relation"]}

    asyncio.run(run())


def test_native_topic_questions_share_one_context_and_decode_nouls() -> None:
    async def run() -> None:
        sent: list[dict[str, Any]] = []

        async def respond(request: httpx2.Request) -> httpx2.Response:
            sent.append(json.loads(request.content))
            return _response({"answer_0": {"type": "noul", "noul": 0.9}, "answer_1": {"type": "noul", "noul": 0.2}})

        connection = _connection(respond)
        try:
            native = NativeJudgments(lambda: connection.bind(timeout_seconds=2.0), model_identity="jev-test")
            items = tuple(
                Question(item_id=code, payload_json=json.dumps({"topic": label})) for code, label in CODEBOOK[-2:]
            )
            context = json.dumps({"claims": [{"statement": "Iran strikes a Gulf base."}]})
            result = await native.judge("topic", items, context_json=context)
        finally:
            await connection.aclose()
        assert [(row.item_id, row.value) for row in result.answers] == [
            ("medtop:20001279", True),
            ("medtop:16000000", False),
        ]
        assert result.answers[0].probabilities == {"true": 0.9, "false": pytest.approx(0.1)}
        inputs = sent[0]["state"]["inputs"]
        assert inputs["context"] == {"claims": [{"statement": "Iran strikes a Gulf base."}]}
        assert all("claims" not in row["payload"] for row in inputs["items"])
        assert {question["type"] for question in sent[0]["questions"].values()} == {"noul"}

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "fault"),
    [
        (401, ConfigurationFault),
        (403, ConfigurationFault),
        (429, ProviderUnavailable),
        (500, ProviderUnavailable),
        (529, ProviderUnavailable),
    ],
)
def test_native_http_errors_are_classified_by_sdk_status(status: int, fault: type[Exception]) -> None:
    async def run() -> None:
        calls: list[httpx2.Request] = []

        async def respond(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            return httpx2.Response(status, json={"error": {"message": "rejected"}})

        connection = _connection(respond)
        try:
            native = NativeJudgments(lambda: connection.bind(timeout_seconds=2.0), model_identity="jev-test")
            with pytest.raises(fault, match=f"news_judgment_http_{status}"):
                await native.judge("relation", (Question(item_id="a", payload_json="{}"),), context_json=None)
        finally:
            await connection.aclose()
        assert len(calls) == 1

    asyncio.run(run())


def test_native_server_error_falls_back_to_generated_for_that_batch_only() -> None:
    async def run() -> None:
        requests = 0

        async def respond(request: httpx2.Request) -> httpx2.Response:
            nonlocal requests
            requests += 1
            if requests == 1:
                return httpx2.Response(529, json={"error": {"message": "overloaded"}})
            return _response({"answer_0": _choice("coverage", "none")})

        connection = _connection(respond)
        generated = Backend("partial", identity="generated")
        try:
            native = NativeJudgments(lambda: connection.bind(timeout_seconds=2.0), model_identity="jev-test")
            judgments = NewsJudgments(generated=generated, native=native, cache=MemoryCache(), batch_size=1)
            answers = await judgments.judge("coverage", questions(2), Budget.start(5))
        finally:
            await connection.aclose()
        assert [(row.value, row.backend) for row in answers] == [("partial", "generated"), ("none", native.identity)]
        assert requests == 2

    asyncio.run(run())


def test_native_authentication_error_is_never_hidden_by_fallback() -> None:
    async def run() -> None:
        async def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(401, json={"error": {"message": "bad key"}})

        connection = _connection(respond)
        generated = Backend(identity="generated")
        try:
            native = NativeJudgments(lambda: connection.bind(timeout_seconds=2.0), model_identity="jev-test")
            judgments = NewsJudgments(generated=generated, native=native, cache=MemoryCache())
            with pytest.raises(ConfigurationFault):
                await judgments.judge("coverage", questions(1), Budget.start(5))
        finally:
            await connection.aclose()
        assert generated.calls == []

    asyncio.run(run())


# ------------------------------------------------------------------ the analyzer's native task layout


class TaskBackend:
    """Answers per task, recording each request."""

    identity = "native-by-task"

    def __init__(self, values: dict[Task, str | bool | None]) -> None:
        self.values = values
        self.calls: list[tuple[Task, int, str | None]] = []

    async def judge(self, task: Task, items: tuple[Question, ...], *, context_json: str | None) -> BatchResult:
        self.calls.append((task, len(items), context_json))
        value = self.values.get(task)
        if value is None:
            raise ProviderUnavailable("controlled provider failure")
        return BatchResult(
            answers=tuple(Answer(item_id=item.item_id, value=value, backend=self.identity) for item in items)
        )


class UnusedExtractor:
    identity = "unused-extractor"

    async def extract(self, source: FrozenInput, *, extract_only: bool) -> Extraction:
        raise AssertionError("understanding must not re-extract supplied claims")


def _source_and_claims(count: int) -> tuple[FrozenInput, Extraction]:
    evidence = Evidence.issue(
        "Agency orders a 25% tariff effective October 1.",
        Source(publisher_id="wire", artifact_id="release", artifact_revision="1", first_available_at_ms=STAMP),
    )
    claims = tuple(
        DraftClaim(
            slot=f"c{index}",
            statement=evidence.text,
            fields=ClaimFields(subject="Agency", action=f"order tariff {index}", content_kind="other"),
            citations=(Citation(evidence_ref=evidence.ref, quote=evidence.text),),
        )
        for index in range(count)
    )
    source = FrozenInput(event_id="event", revision=1, lineage_id="lineage", evidence=(evidence,))
    return source, Extraction(claims=claims)


def test_analyzer_asks_native_readings_per_claim_and_the_whole_codebook_once() -> None:
    async def run() -> None:
        native = TaskBackend(
            {
                "mode": "decision",
                "phase": "ordered",
                "content_kind": "official_measure",
                "topic": True,
                "support": "supports",
            }
        )
        generated = Backend(identity="generated")
        analyzer = SemanticAnalyzer(
            UnusedExtractor(), NewsJudgments(generated=generated, native=native, cache=MemoryCache())
        )
        source, extracted = _source_and_claims(10)
        result = await analyzer.understand(source, extracted, Budget.start(5))
        topic_calls = [call for call in native.calls if call[0] == "topic"]
        assert topic_calls == [("topic", len(CODEBOOK), topic_calls[0][2])]
        assert json.loads(topic_calls[0][2] or "{}")["claims"][0]["slot"] == "c0"
        # Ten claims pack into batches of eight for every per-claim reading.
        assert [call[:2] for call in native.calls if call[0] == "mode"] == [("mode", 8), ("mode", 2)]
        assert {claim.fields.mode for claim in result.claims} == {"decision"}
        assert {claim.fields.phase for claim in result.claims} == {"ordered"}
        assert {claim.fields.content_kind for claim in result.claims} == {"official_measure"}
        assert len(result.topics) == 3
        assert generated.calls == []

    asyncio.run(run())


def test_unavailable_content_reading_keeps_the_extracted_kind() -> None:
    async def run() -> None:
        native = TaskBackend({"mode": "decision", "phase": "ordered", "topic": False, "support": "supports"})
        generated = Backend(identity="generated", fail_batch=1)
        analyzer = SemanticAnalyzer(
            UnusedExtractor(), NewsJudgments(generated=generated, native=native, cache=MemoryCache())
        )
        source, extracted = _source_and_claims(1)
        result = await analyzer.understand(source, extracted, Budget.start(5))
        assert result.claims[0].fields.content_kind == "other"
        assert result.claims[0].fields.mode == "decision"

    asyncio.run(run())
