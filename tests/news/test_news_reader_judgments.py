"""The reader judgment of one claim: its frozen input, independent questions, fallback and reuse.

The native tests drive DspyReaderJudge through a real SystemOneConnection, the official SDK and DSPy's
decision adapter over httpx2.MockTransport; the generative route is a scripted DSPy LM. No provider is called.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import dspy
import httpx2
import pytest
from pydantic import ValidationError

from tests.support.news_update_semantic import MemoryCache
from tests.support.scripted_lm import ScriptedLM
from tracefold.app.system_one import SystemOneConnection
from tracefold.news.adapters.reader_judge import DspyReaderJudge
from tracefold.news.notifications.novelty import ClaimLink, LinkedReceipt, current_links, reader_novelty
from tracefold.news.notifications.policy import READER_CALIBRATIONS, anchor_index, calibration_for
from tracefold.news.notifications.reader import (
    ANCHOR_QUESTION,
    INTERRUPT_QUESTION,
    MATERIALITY_LEVELS,
    MATERIALITY_QUESTION,
    READER_INSTRUCTIONS,
    READER_QUOTE_CHARS_MAX,
    REPORT_KIND_OPTIONS,
    REPORT_KIND_QUESTION,
    AnchorEvidence,
    InterruptEvidence,
    MaterialityEvidence,
    ReaderInput,
    ReaderJudgment,
    ReportKind,
    ReportKindEvidence,
    cache_key,
    cached_judgments,
)
from tracefold.news.updates.assembly import assemble_update
from tracefold.news.updates.contracts import (
    Citation,
    ClaimFields,
    DraftClaim,
    Evidence,
    Extraction,
    FrozenInput,
    Source,
)
from tracefold.news.updates.identity import digest
from tracefold.news.updates.judgment import Budget, ConfigurationFault
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
    assert reader.schema_version == "news_reader_input_v3"
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
    assert claim["topics"] == [TOPIC_NAME] and "change" not in claim
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
        "legend": {str(index): level for index, level in enumerate(MATERIALITY_LEVELS)},
        "probabilities": {str(index): p for index, p in enumerate(probabilities)},
    }


def _choice(probabilities: dict[str, float]) -> dict[str, Any]:
    return {
        "type": "choice",
        "choice": max(probabilities, key=probabilities.__getitem__),
        "confidence": 0.9,
        "probabilities": probabilities,
    }


def _kind_probabilities(**values: float) -> dict[ReportKind, float]:
    return {kind: values.get(kind, 0.0) for kind, _ in REPORT_KIND_OPTIONS}


def _noul(probability: float) -> dict[str, Any]:
    return {"type": "noul", "noul": probability, "confidence": abs(2 * probability - 1)}


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


def test_one_native_request_asks_four_independent_questions_over_one_shared_state() -> None:
    async def run() -> tuple[ReaderJudgment, list[dict[str, Any]], str]:
        sent: list[dict[str, Any]] = []

        async def respond(request: httpx2.Request) -> httpx2.Response:
            assert request.url.path == "/v1/systemone"
            sent.append(json.loads(request.content))
            return _response(
                {
                    "report_kind": _choice(_kind_probabilities(new_action=0.8, background=0.2)),
                    "anchor_message": _choice({"m1": 0.85, "m2": 0.05, "none": 0.1}),
                    "materiality": _score([0.0, 0.1, 0.7, 0.2]),
                    "interrupt_now": _noul(0.75),
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
    assert len(sent) == 1
    request = sent[0]
    assert "temperature" not in request and "max_tokens" not in request
    assert request["state"]["instructions"] == READER_INSTRUCTIONS
    assert [row["id"] for row in request["state"]["inputs"]["messages"]] == ["m1", "m2"]
    assert request["state"]["inputs"]["claim"]["statement"] == "Nvidia announced a $150 billion share buyback."
    assert set(request["questions"]) == {"report_kind", "anchor_message", "materiality", "interrupt_now"}
    kind = request["questions"]["report_kind"]
    assert kind["type"] == "choice" and kind["instructions"] == REPORT_KIND_QUESTION
    assert kind["criteria"] == dict(REPORT_KIND_OPTIONS)
    materiality = request["questions"]["materiality"]
    assert materiality["type"] == "score" and materiality["instructions"] == MATERIALITY_QUESTION
    assert materiality["criteria"] == list(MATERIALITY_LEVELS)
    interrupt = request["questions"]["interrupt_now"]
    assert interrupt["type"] == "noul" and interrupt["instructions"] == INTERRUPT_QUESTION
    anchor = request["questions"]["anchor_message"]
    assert anchor["type"] == "choice" and anchor["instructions"] == ANCHOR_QUESTION
    assert list(anchor["criteria"]) == ["m1", "m2", "none"]

    assert judgment.status == "available" and judgment.backend == "native"
    assert judgment.identity == native_identity and judgment.served_model == "jev-1.13-served"
    assert judgment.report_kind is not None and judgment.report_kind.value == "new_action"
    assert judgment.report_kind.probabilities == pytest.approx(_kind_probabilities(new_action=0.8, background=0.2))
    assert judgment.materiality is not None and judgment.materiality.value == pytest.approx(2.1)
    assert judgment.materiality.probabilities == pytest.approx((0.0, 0.1, 0.7, 0.2))
    assert judgment.interrupt is not None and judgment.interrupt.probabilities == pytest.approx((0.25, 0.75))
    assert judgment.interrupt.probability == 0.75 and judgment.interrupt.confidence == 0.5
    assert judgment.anchor is not None and anchor_index(judgment.anchor, calibration_for(judgment)) == 0
    assert judgment.matches(_reader()) and not judgment.matches(_reader(SENT[:1]))


def test_no_recalled_message_asks_all_questions_except_the_anchor() -> None:
    async def run() -> tuple[ReaderJudgment, list[dict[str, Any]]]:
        sent: list[dict[str, Any]] = []

        async def respond(request: httpx2.Request) -> httpx2.Response:
            sent.append(json.loads(request.content))
            return _response(
                {
                    "report_kind": _choice(_kind_probabilities(background=1.0)),
                    "materiality": _score([0.5, 0.5, 0.0, 0.0]),
                    "interrupt_now": _noul(0.05),
                }
            )

        connection = _connection(respond)
        try:
            judgment = await _judge(connection).judge(_reader(()), Budget.start(5))
        finally:
            await connection.aclose()
        return judgment, sent

    judgment, sent = asyncio.run(run())
    assert set(sent[0]["questions"]) == {"report_kind", "materiality", "interrupt_now"}
    assert "messages" not in sent[0]["state"]["inputs"]
    assert judgment.anchor is None and judgment.materiality is not None
    assert judgment.materiality.value == pytest.approx(0.5)
    assert judgment.report_kind is not None and judgment.report_kind.value == "background"
    assert judgment.interrupt is not None and judgment.interrupt.probability == 0.05


def _generated_answer(request: Any) -> dict[str, Any]:
    return {
        "report_kind": {"probabilities": _kind_probabilities(new_action=0.6, commentary=0.4), "confidence": 0.6},
        "materiality": {"probabilities": {"0": 0.1, "1": 0.2, "2": 0.4, "3": 0.3}, "confidence": 0.6},
        "interrupt_now": {"noul": 0.2},
        "anchor_message": {"probabilities": {"m1": 0.1, "m2": 0.1, "none": 0.8}, "confidence": 0.7},
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
    assert judgment.identity == judge.generated_identity
    assert calibration_for(judgment) == READER_CALIBRATIONS["generated"]
    assert judgment.materiality is not None and judgment.materiality.value == pytest.approx(1.9)
    assert judgment.report_kind is not None and judgment.report_kind.value == "new_action"
    assert judgment.interrupt is not None and judgment.interrupt.probabilities == pytest.approx((0.8, 0.2))
    assert judgment.anchor is not None and anchor_index(judgment.anchor, calibration_for(judgment)) is None
    request = generated.requests[0]
    prompt = "\n".join([str(request.system or ""), *(message.text for message in request.messages)])
    assert READER_INSTRUCTIONS in prompt
    for text in (REPORT_KIND_OPTIONS[0][1], MATERIALITY_LEVELS[3], ANCHOR_QUESTION, INTERRUPT_QUESTION):
        assert json.dumps(text, ensure_ascii=False)[1:-1] in prompt


def test_a_slow_native_answer_falls_back_within_the_stage_deadline() -> None:
    async def run() -> ReaderJudgment:
        async def respond(request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(1.0)
            return _response({})

        connection = _connection(respond)
        try:
            judge = _judge(connection, ScriptedLM([_generated_answer]), native_operation_seconds=0.05)
            return await judge.judge(_reader(), Budget.start(5))
        finally:
            await connection.aclose()

    assert asyncio.run(run()).backend == "generated"


@pytest.mark.parametrize(
    ("question", "distribution"),
    [
        ("report_kind", "probabilities"),
        ("materiality", "probabilities"),
        ("interrupt_now", "probability"),
        ("anchor_message", "probabilities"),
    ],
)
def test_missing_native_distribution_falls_back_for_each_independent_question(
    monkeypatch: pytest.MonkeyPatch, question: str, distribution: str
) -> None:
    prediction = SimpleNamespace(
        report_kind=SimpleNamespace(value="new_action", probabilities=_kind_probabilities(new_action=1), confidence=1),
        materiality=SimpleNamespace(value=2, probabilities={0: 0, 1: 0, 2: 1, 3: 0}, confidence=1),
        interrupt_now=SimpleNamespace(probability=0.7, confidence=0.4),
        anchor_message=SimpleNamespace(probabilities={"m1": 0, "m2": 0, "none": 1}, confidence=1),
    )
    setattr(getattr(prediction, question), distribution, None)

    async def native(*args: Any, **kwargs: Any) -> Any:
        return prediction

    monkeypatch.setattr("tracefold.news.adapters.reader_judge.native_predict", native)
    generated = ScriptedLM([_generated_answer])
    judge = DspyReaderJudge(
        lambda: generated,
        generated_model_identity="generated-test",
        native_lm_factory=lambda: None,
        native_model_identity="native-test",
    )
    judgment = asyncio.run(judge.judge(_reader(), Budget.start(5)))
    assert judgment.status == "available" and judgment.backend == "generated"
    assert len(generated.requests) == 1


def test_generated_without_messages_preserves_all_three_evidence_distributions() -> None:
    def answer(request: Any) -> dict[str, Any]:
        values = _generated_answer(request)
        del values["anchor_message"]
        return values

    judgment = asyncio.run(_judge(None, ScriptedLM([answer])).judge(_reader(()), Budget.start(5)))
    assert judgment.status == "available" and judgment.backend == "generated" and judgment.anchor is None
    assert judgment.report_kind is not None and len(judgment.report_kind.probabilities) == len(REPORT_KIND_OPTIONS)
    assert judgment.materiality is not None and len(judgment.materiality.probabilities) == len(MATERIALITY_LEVELS)
    assert judgment.interrupt is not None and judgment.interrupt.probabilities == pytest.approx((0.8, 0.2))


def test_when_neither_backend_answers_the_judgment_is_unavailable_and_never_stored() -> None:
    async def run() -> tuple[ReaderJudgment, ReaderJudgment, MemoryCache]:
        async def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(503, json={"error": {"message": "down"}})

        connection = _connection(respond)
        generated = ScriptedLM([dspy.LMServerError("down"), dspy.LMServerError("down")])
        judge = _judge(connection, generated)
        cache = MemoryCache()
        try:
            first = (await cached_judgments(judge, cache, {"c": _reader()}, Budget.start(5)))["c"]
            second = (await cached_judgments(judge, cache, {"c": _reader()}, Budget.start(5)))["c"]
        finally:
            await connection.aclose()
        return first, second, cache

    first, second, cache = asyncio.run(run())
    assert first.status == second.status == "unavailable"
    assert first.error_code == "news_generation_lm_server_error"
    assert first.report_kind is first.materiality is first.interrupt is first.anchor is None
    assert cache.values == {}


def test_generated_score_decoder_value_error_is_an_unavailable_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    class MalformedScore:
        async def acall(self, **kwargs: Any) -> Any:
            raise ValueError("Invalid Score distribution for 'materiality'.")

    monkeypatch.setattr(dspy, "Predict", lambda signature: MalformedScore())
    judgment = asyncio.run(_judge(None, ScriptedLM([])).judge(_reader(), Budget.start(5)))
    assert judgment.status == "unavailable"
    assert judgment.error_code == "news_generation_output_schema_invalid"


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
            report_kind=ReportKindEvidence(
                value="new_action", probabilities=_kind_probabilities(new_action=1), confidence=0.7
            ),
            materiality=MaterialityEvidence(value=2.6, probabilities=(0.0, 0.1, 0.2, 0.7), confidence=0.7),
            interrupt=InterruptEvidence(probabilities=(0.4, 0.6), confidence=0.2),
            anchor=AnchorEvidence(probabilities={"m1": 0.2, "m2": 0.1, "none": 0.7}, confidence=0.7),
        )


def test_an_available_answer_is_reused_for_exactly_the_same_input() -> None:
    async def run() -> tuple[_CountingJudge, MemoryCache, ReaderJudgment]:
        judge, cache = _CountingJudge(), MemoryCache()
        first = (await cached_judgments(judge, cache, {"c": _reader()}, Budget.start(5)))["c"]
        again = (await cached_judgments(judge, cache, {"c": _reader()}, Budget.start(5)))["c"]
        assert again == first
        other = _update("NVIDIA ANNOUNCES $160 BILLION BUYBACK")
        both = {"c": _reader(), "d": ReaderInput.of(other.claims[0], other, SENT)}
        assert (await cached_judgments(judge, cache, both, Budget.start(5)))["c"] == first
        return judge, cache, first

    judge, cache, first = asyncio.run(run())
    # One read per set and one write per set of new answers; the sibling asked alone.
    assert judge.calls == 2 and len(cache.reads) == 3 and len(cache.writes) == 2
    stored = cache.values[cache_key(judge, _reader())]
    assert stored.status == "available" and ReaderJudgment.model_validate_json(str(stored.value)) == first


def test_evidence_shapes_and_judgment_status_are_exact() -> None:
    with pytest.raises(ValidationError, match="news_reader_anchor_options_invalid"):
        AnchorEvidence(probabilities={"m1": 0.5, "m3": 0.1, "none": 0.4}, confidence=0.5)
    with pytest.raises(ValidationError, match="news_reader_anchor_options_invalid"):
        AnchorEvidence(probabilities={"none": 1.0}, confidence=0.5)
    with pytest.raises(ValidationError):
        MaterialityEvidence(value=2.0, probabilities=(0.5, 0.5), confidence=0.5)
    with pytest.raises(ValidationError, match="news_reader_report_kind_options_invalid"):
        ReportKindEvidence(value="new_action", probabilities={"new_action": 1}, confidence=0.5)
    with pytest.raises(ValidationError, match="news_reader_materiality_distribution_invalid"):
        MaterialityEvidence(value=2.0, probabilities=(0.0, 0.0, 0.2, 0.2), confidence=0.5)
    with pytest.raises(ValidationError, match="news_reader_interrupt_distribution_invalid"):
        InterruptEvidence(probabilities=(0.2, 0.2), confidence=0.5)
    with pytest.raises(ValidationError):
        InterruptEvidence(probabilities=(-0.2, 1.2), confidence=0.5)
    with pytest.raises(ValidationError, match="news_reader_unavailable_judgment_has_answer"):
        ReaderJudgment(status="unavailable", error_code="x", backend="native")
    with pytest.raises(ValidationError, match="news_reader_available_judgment_incomplete"):
        ReaderJudgment(status="available", backend="native", identity="n")
    assert set(ReaderJudgment.model_json_schema()["properties"]) >= {
        "report_kind",
        "materiality",
        "interrupt",
        "anchor",
    }
    assert "importance" not in ReaderJudgment.model_json_schema()["properties"]
    anchor = AnchorEvidence(probabilities={"m1": 0.1, "m2": 0.75, "none": 0.15}, confidence=0.5)
    assert anchor_index(anchor, READER_CALIBRATIONS["native"]) == 1
    unsure = AnchorEvidence(probabilities={"m1": 0.1, "m2": 0.55, "none": 0.35}, confidence=0.5)
    assert anchor_index(unsure, READER_CALIBRATIONS["native"]) is None


def test_the_cache_key_names_the_judge_and_the_frozen_input_only() -> None:
    judge = _CountingJudge()
    assert cache_key(judge, _reader()) == cache_key(judge, _reader())
    assert cache_key(judge, _reader()) != cache_key(judge, _reader(SENT[:1]))
    assert digest(_reader()) == _reader().digest


def _link(current: str, previous: str, relation: str, at: int = 1) -> ClaimLink:
    return ClaimLink.model_validate(
        {"current_ref": current, "previous_ref": previous, "relation": relation, "asserted_at_ms": at}
    )


def _sent(intent: str, *refs: str, state: str = "sent", at: int = 10) -> LinkedReceipt:
    return LinkedReceipt.model_validate(
        {"intent_id": intent, "state": state, "claim_refs": refs, "settled_at_ms": None if state == "sending" else at}
    )


@pytest.mark.parametrize(
    ("links", "novelty", "intent"),
    [
        # The claim's own adoption compared it with a delivered claim.
        ([_link("c", "a", "equivalent")], "known", "ra"),
        ([_link("c", "a", "adds_information")], "increment", "ra"),
        ([_link("c", "a", "real_world_change")], "development", "ra"),
        ([_link("c", "a", "corrects")], "development", "ra"),
        # A delivered claim's later adoption said it adds to, or supersedes, this one: the reader holds more.
        ([_link("a", "c", "adds_information")], "known", "ra"),
        ([_link("a", "c", "real_world_change")], "known", "ra"),
        # A conflict names no order and leaves the claim to the questions.
        ([_link("c", "a", "conflicts")], "unlinked", None),
        # Two links pass through an equivalent claim.
        ([_link("c", "x", "equivalent"), _link("x", "a", "adds_information")], "increment", "ra"),
        ([_link("c", "x", "adds_information"), _link("x", "a", "equivalent")], "increment", "ra"),
        ([_link("c", "x", "adds_information"), _link("x", "a", "adds_information")], "unlinked", None),
        (
            [_link("c", "x", "equivalent"), _link("x", "y", "equivalent"), _link("y", "a", "equivalent")],
            "unlinked",
            None,
        ),
        # A later assertion about the same pair replaces the earlier one, whichever side made it.
        ([_link("c", "a", "adds_information", 1), _link("a", "c", "adds_information", 2)], "known", "ra"),
        ([_link("c", "a", "equivalent", 1), _link("c", "a", "conflicts", 2)], "unlinked", None),
    ],
)
def test_reader_novelty_reads_persisted_links_against_delivered_claims(
    links: list[ClaimLink], novelty: str, intent: str | None
) -> None:
    result = reader_novelty("c", links, [_sent("ra", "a"), _sent("rz", "z")])
    assert (result.novelty, result.intent_id) == (novelty, intent)
    assert result.linked_intents == (() if intent is None else (intent,))


def test_known_outranks_in_flight_which_outranks_development_and_increment() -> None:
    links = [_link("c", "a", "adds_information"), _link("c", "b", "real_world_change"), _link("c", "s", "equivalent")]
    assert reader_novelty("c", links[:2], [_sent("ra", "a"), _sent("rb", "b")]).novelty == "development"
    assert reader_novelty("c", links, [_sent("ra", "a"), _sent("rs", "s", state="sending")]).novelty == "in_flight"
    known = reader_novelty("c", links, [_sent("ra", "a"), _sent("rs", "s", state="ambiguous")])
    assert (known.novelty, known.intent_id, known.linked_intents) == ("known", "rs", ("rs", "ra"))
    # A claim a delivered receipt already carries is known without any link.
    assert reader_novelty("c", [], [_sent("rc", "c")]).novelty == "known"
    assert current_links([_link("c", "c", "equivalent")]) == ()


@pytest.mark.parametrize("stamp,expected", [(1790899199999, "2026-10-01"), (1790899200000, "2026-10-02")])
def test_reader_as_of_uses_the_fixed_first_visibility_utc_date(stamp: int, expected: str) -> None:
    update = _update()
    claim = update.claims[0].model_copy(update={"first_available_at_ms": stamp})
    reader = ReaderInput.of(claim, update, ())
    assert reader.as_of.isoformat() == expected
    assert reader.model_inputs()["as_of"] == expected
    later = update.model_copy(update={"adopted_at_ms": stamp + 5 * 86_400_000})
    assert ReaderInput.of(claim, later, ()).digest == reader.digest
