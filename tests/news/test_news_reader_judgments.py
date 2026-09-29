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
    ANCHOR_QUESTION,
    IMPORTANCE_LEVELS,
    IMPORTANCE_QUESTION,
    READER_CUTS,
    READER_INSTRUCTIONS,
    READER_QUOTE_CHARS_MAX,
    AnchorEvidence,
    ClaimLink,
    ImportanceEvidence,
    LinkedReceipt,
    ReaderCuts,
    ReaderInput,
    ReaderJudgment,
    ReaderNovelty,
    cache_key,
    cached_judgments,
    current_links,
    reader_decision,
    reader_novelty,
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
    assert reader.schema_version == "news_reader_input_v2"
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
                    "anchor_message": _choice({"m1": 0.85, "m2": 0.05, "none": 0.1}),
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
    anchor = request["questions"]["anchor_message"]
    assert anchor["type"] == "choice" and anchor["instructions"] == ANCHOR_QUESTION
    assert list(anchor["criteria"]) == ["m1", "m2", "none"]

    assert judgment.status == "available" and judgment.backend == "native"
    assert judgment.identity == native_identity and judgment.served_model == "jev-1.13-served"
    assert judgment.importance is not None and judgment.importance.value == pytest.approx(2.8)
    assert judgment.importance.probabilities == pytest.approx((0.0, 0.1, 0.1, 0.7, 0.1))
    assert judgment.anchor is not None and judgment.anchor.anchor(judgment.cuts) == 0
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
    assert judgment.anchor is None and judgment.importance is not None
    assert judgment.importance.value == pytest.approx(0.5)


def _generated_answer(request: Any) -> dict[str, Any]:
    return {
        "importance": {"probabilities": {"0": 0.1, "1": 0.2, "2": 0.4, "3": 0.2, "4": 0.1}, "confidence": 0.6},
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
    assert judgment.identity == judge.generated_identity and judgment.cuts == READER_CUTS["generated"]
    assert judgment.importance is not None and judgment.importance.value == pytest.approx(2.0)
    assert judgment.anchor is not None and judgment.anchor.anchor(judgment.cuts) is None
    request = generated.requests[0]
    prompt = "\n".join([str(request.system or ""), *(message.text for message in request.messages)])
    assert READER_INSTRUCTIONS in prompt
    for text in (IMPORTANCE_LEVELS[3], ANCHOR_QUESTION):
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
            first = (await cached_judgments(judge, cache, {"c": _reader()}, Budget.start(5)))["c"]
            second = (await cached_judgments(judge, cache, {"c": _reader()}, Budget.start(5)))["c"]
        finally:
            await connection.aclose()
        return first, second, cache

    first, second, cache = asyncio.run(run())
    assert first.status == second.status == "unavailable"
    assert first.error_code == "news_generation_lm_server_error"
    assert first.importance is None and first.anchor is None
    assert cache.values == {}


def test_generated_score_decoder_value_error_is_an_unavailable_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    class MalformedScore:
        async def acall(self, **kwargs: Any) -> Any:
            raise ValueError("Invalid Score distribution for 'importance'.")

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
            importance=ImportanceEvidence(value=2.6, probabilities=(0.0, 0.1, 0.3, 0.5, 0.1), confidence=0.7),
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
        ImportanceEvidence(value=2.0, probabilities=(0.5, 0.5), confidence=0.5)
    with pytest.raises(ValidationError, match="news_reader_unavailable_judgment_has_answer"):
        ReaderJudgment(status="unavailable", error_code="x", backend="native")
    with pytest.raises(ValidationError, match="news_reader_available_judgment_incomplete"):
        ReaderJudgment(status="available", backend="native", identity="n")
    anchor = AnchorEvidence(probabilities={"m1": 0.1, "m2": 0.75, "none": 0.15}, confidence=0.5)
    assert anchor.anchor(READER_CUTS["native"]) == 1
    unsure = AnchorEvidence(probabilities={"m1": 0.1, "m2": 0.55, "none": 0.35}, confidence=0.5)
    assert unsure.anchor(READER_CUTS["native"]) is None
    for cuts in READER_CUTS.values():
        assert 0 < cuts.push < cuts.key < len(IMPORTANCE_LEVELS) - 1 and 0 < cuts.anchor_none_below < 1


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


def _judgment(value: float, anchor: dict[str, float] | None = None) -> ReaderJudgment:
    return ReaderJudgment(
        status="available",
        backend="native",
        identity="native-test",
        importance=ImportanceEvidence(value=value, probabilities=(0.2, 0.2, 0.2, 0.2, 0.2), confidence=0.5),
        anchor=None if anchor is None else AnchorEvidence(probabilities=anchor, confidence=0.5),
    )


def test_reader_decision_rows_in_order() -> None:
    cuts = READER_CUTS["native"]
    corrects = ReaderNovelty(
        novelty="development", intent_id="ra", settled_at_ms=10, path=(_link("c", "a", "corrects"),)
    )
    change = corrects.model_copy(update={"path": (_link("c", "a", "real_world_change"),)})

    def decide(novelty: ReaderNovelty, value: float, **options: Any) -> tuple[str, str, str | None]:
        options.setdefault("first_available_at_ms", 20)
        result = reader_decision(
            novelty, _judgment(value, options.pop("anchor", None)), message_intents=("ra", "rb"), **options
        )
        return result.outcome, result.render, result.anchor_intent_id

    assert decide(ReaderNovelty(novelty="known", intent_id="ra"), 3.5) == ("known", "full", "ra")
    assert decide(ReaderNovelty(novelty="in_flight", intent_id="ra"), 3.5)[0] == "in_flight"
    # A correction of a delivered claim is repaired regardless of its score, but only when it came later.
    assert decide(corrects, 0.1) == ("correction", "correction", "ra")
    assert decide(corrects, 0.1, first_available_at_ms=5) == ("feed", "increment", "ra")
    # A real-world development is pushed on what it adds, as an increment on the earlier message.
    assert decide(change, cuts.push) == ("push", "increment", "ra")
    assert decide(change, cuts.push - 0.01)[0] == "feed"
    # What the reader already has the core fact of, by a link or by the anchor, needs the key cut; it is an
    # increment only on the message the anchor names, whichever message the link reached.
    anchored, unanchored = {"m1": 0.1, "m2": 0.8, "none": 0.1}, {"m1": 0.1, "m2": 0.1, "none": 0.8}
    increment = ReaderNovelty(novelty="increment", intent_id="ra", linked_intents=("ra",))
    assert decide(increment, cuts.key, anchor=anchored) == ("key", "increment", "rb")
    assert decide(increment, cuts.key, anchor=unanchored) == ("key", "full", None)
    assert decide(increment, cuts.key) == ("key", "full", None)
    assert decide(increment, cuts.key - 0.01, anchor=anchored) == ("feed", "increment", "rb")
    unlinked = ReaderNovelty(novelty="unlinked")
    assert decide(unlinked, cuts.key, anchor=anchored) == ("key", "increment", "rb")
    assert decide(unlinked, cuts.key - 0.01, anchor=anchored) == ("feed", "increment", "rb")
    assert decide(unlinked, cuts.push, anchor=unanchored) == ("push", "full", None)
    assert decide(unlinked, 1.0) == ("feed", "full", None)
    looser = ReaderCuts(push=0.5, key=3.9, anchor_none_below=0.9)
    assert decide(unlinked, 1.0, cuts=looser)[0] == "push"


# The first live receipt's repeats (#742 PR-4). P005: the same PSL cut 14 s after P004, a separate Event with
# no link, anchored to P004. P010: the UK Navy confirming the Hormuz ship fire, linked as adding to the
# IRGC-fire push 1.7 h earlier, with an anchor that disagreed and so no "补充" either.
P005 = (ReaderNovelty(novelty="unlinked"), {"m1": 0.83, "m2": 0.0, "none": 0.17}, "increment", "ra")
P010 = (
    ReaderNovelty(novelty="increment", intent_id="ra", linked_intents=("ra",)),
    {"m1": 0.35, "m2": 0.0, "none": 0.65},
    "full",
    None,
)


@pytest.mark.parametrize("case", [P005, P010], ids=["P005-anchored", "P010-linked"])
@pytest.mark.parametrize(("importance", "outcome"), [(2.5, "feed"), (2.59, "feed"), (2.79, "feed"), (2.8, "key")])
def test_a_known_core_fact_is_pushed_only_at_the_key_cut(
    case: tuple[Any, ...], importance: float, outcome: str
) -> None:
    novelty, anchor, render, intent = case
    result = reader_decision(
        novelty, _judgment(importance, anchor), first_available_at_ms=20, message_intents=("ra", "rb")
    )
    assert (result.outcome, result.render, result.anchor_intent_id) == (outcome, render, intent)
